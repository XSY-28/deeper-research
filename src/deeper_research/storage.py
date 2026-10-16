"""SQLite truth, immutable artifacts, task locks and global paid-call ledger."""
from contextlib import contextmanager
from datetime import datetime, timezone
from decimal import Decimal
import fcntl
import hashlib
import json
import os
from pathlib import Path
import re
import sqlite3
import uuid
import time

from .contracts import LIMITS, PROMPT_VERSION, CODE_VERSION
from .acceptance import validate_plan


class BudgetExceeded(RuntimeError):
    pass


def now():
    return datetime.now(timezone.utc).isoformat()


def dump(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True)


def valid_id(value):
    if not isinstance(value, str) or not re.fullmatch(r"[a-f0-9]{32}", value):
        raise ValueError("Invalid task or call ID")
    return value


class Store:
    def __init__(self, root: Path, project_budget: Decimal = Decimal("10")):
        self.root = Path(root).resolve()
        self.root.mkdir(parents=True, exist_ok=True)
        if not project_budget.is_finite() or project_budget < 0:
            raise ValueError("Budget must be finite and non-negative")
        self.db_path = self.root / "state.sqlite"
        with self.db() as db:
            db.executescript("""
                CREATE TABLE IF NOT EXISTS settings(key TEXT PRIMARY KEY, value TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS tasks(
                    id TEXT PRIMARY KEY, spec TEXT NOT NULL, state TEXT NOT NULL,
                    phase TEXT NOT NULL, reason TEXT, quality TEXT NOT NULL DEFAULT 'none',
                    selected TEXT, checkpoint TEXT NOT NULL DEFAULT '{}',
                    rounds INTEGER NOT NULL DEFAULT 0, reads INTEGER NOT NULL DEFAULT 0,
                    active_seconds REAL NOT NULL DEFAULT 0, cancel_requested INTEGER NOT NULL DEFAULT 0,
                    created TEXT NOT NULL, updated TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS calls(
                    id TEXT PRIMARY KEY, task_id TEXT NOT NULL, role TEXT NOT NULL, status TEXT NOT NULL,
                    request TEXT NOT NULL, response TEXT, input_estimate INTEGER NOT NULL,
                    max_output INTEGER NOT NULL, tokens INTEGER NOT NULL,
                    input_price TEXT NOT NULL, output_price TEXT NOT NULL,
                    accounted_usd TEXT NOT NULL, estimated INTEGER NOT NULL DEFAULT 1,
                    error TEXT, created TEXT NOT NULL, updated TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS events(
                    id INTEGER PRIMARY KEY, task_id TEXT NOT NULL, kind TEXT NOT NULL,
                    detail TEXT NOT NULL, created TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS artifacts(
                    path TEXT PRIMARY KEY, task_id TEXT NOT NULL, kind TEXT NOT NULL, sha256 TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS task_groups(
                    task_id TEXT PRIMARY KEY, root_id TEXT NOT NULL, slot INTEGER NOT NULL,
                    UNIQUE(root_id, slot));
            """)
            db.execute("INSERT OR IGNORE INTO settings VALUES ('budget_usd', ?)", (str(project_budget),))

    @contextmanager
    def db(self):
        db = sqlite3.connect(self.db_path, timeout=10)
        db.row_factory = sqlite3.Row
        db.execute("PRAGMA journal_mode=WAL")
        try:
            yield db
            db.commit()
        except BaseException:
            db.rollback()
            raise
        finally:
            db.close()

    @contextmanager
    def execution_lock(self):
        with (self.root / ".run.lock").open("a+") as handle:
            try:
                fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError as exc:
                raise RuntimeError("Another task is running in this workspace") from exc
            try:
                yield
            finally:
                fcntl.flock(handle, fcntl.LOCK_UN)

    def _prepare_spec(self, spec: dict) -> dict:
        spec = json.loads(dump(spec))
        if not isinstance(spec.get("question"), str) or not spec["question"].strip():
            raise ValueError("A non-empty question is required")
        if not isinstance(spec.get("criteria"), list) or not spec["criteria"] or any(
                not isinstance(c, str) or not c.strip() for c in spec["criteria"]):
            raise ValueError("Non-empty acceptance criteria are required")
        if not isinstance(spec.get("source_ids"), list) or not spec["source_ids"]:
            raise ValueError("At least one frozen source is required")
        if spec.get("effort") not in LIMITS:
            raise ValueError("effort must be low, medium or high")
        mode = spec.get('mode', 'single')
        if mode not in {'single', 'multi', 'single-budgeted'}:
            raise ValueError('Unknown research mode')
        if mode != 'single' and spec['effort'] != 'high':
            raise ValueError('Budgeted modes require high effort for full loops')
        spec["limits"] = dict(LIMITS[spec["effort"]])
        if mode != 'single':
            spec['limits'] = {'calls':60, 'tokens':640000, 'seconds':3600,
                              'rounds':18, 'directions':2, 'reads':96}
            spec.setdefault('budget_usd', '0.30')
        if 'budget_usd' in spec:
            try:
                value = Decimal(str(spec['budget_usd']))
                if not value.is_finite() or value <= 0:
                    raise ValueError('Task budget must be positive and finite')
            except ArithmeticError as exc:
                raise ValueError('Invalid task USD budget') from exc
            spec['budget_usd'] = str(value)
        if 'requirements' in spec:
            spec['requirements'] = validate_plan(spec['requirements'], spec['criteria'], spec['question'])
        spec["prompt_version"] = PROMPT_VERSION
        spec["code_version"] = CODE_VERSION
        return spec

    def create_task(self, spec: dict) -> str:
        if {'group_root', 'candidate_number'} & spec.keys():
            raise ValueError('Candidate membership is owned by the coordinator')
        spec = self._prepare_spec(spec)
        task_id = uuid.uuid4().hex
        with self.db() as db:
            db.execute("INSERT INTO tasks(id,spec,state,phase,created,updated) VALUES (?,?,?,?,?,?)",
                       (task_id, dump(spec), "ready", "retrieval", now(), now()))
        return task_id

    def create_child(self, root_id: str, slot: int, strategy: str,
                     challenge: dict | None = None) -> str:
        """Create a frozen child and its membership in one transaction."""
        root = self.task(root_id)
        if root['spec'].get('mode') != 'multi' or type(slot) is not int or not 1 <= slot <= 5:
            raise ValueError('Only a multi root owns candidate slots 1-5')
        if root['state'] in {'finished', 'cancelled'}:
            raise ValueError('Cannot add candidates to a closed search')
        if strategy not in {'direct', 'alternative', 'counterexample', 'challenge'}:
            raise ValueError('Invalid generation strategy')
        spec = {k:root['spec'][k] for k in ('question','criteria','source_ids','profile')}
        spec.update(effort='high', group_root=root_id, candidate_number=slot,
                    generation_strategy=strategy,
                    budget_usd=str(Decimal(root['spec']['budget_usd'])*Decimal('0.8')/3))
        if challenge:
            spec['challenge'] = challenge
        plan = root['spec'].get('requirements')
        if not plan and root['checkpoint'].get('requirements'):
            plan = self.read_artifact(root['checkpoint']['requirements'])
        if not plan:
            raise ValueError('Compile shared requirements before creating candidates')
        # IDs and anchors are assigned to the identical frozen root criteria.
        requirements = validate_plan(plan, spec['criteria'], spec['question'], assign_anchors=True)
        spec = self._prepare_spec(spec)
        spec['requirements'] = requirements
        task_id = uuid.uuid4().hex
        with self.db() as db:
            existing = db.execute('SELECT task_id FROM task_groups WHERE root_id=? AND slot=?',
                                  (root_id,slot)).fetchone()
            if existing:
                return existing[0]
            db.execute('INSERT INTO tasks(id,spec,state,phase,created,updated) VALUES (?,?,?,?,?,?)',
                       (task_id,dump(spec),'ready','retrieval',now(),now()))
            db.execute('INSERT INTO task_groups VALUES (?,?,?)',(task_id,root_id,slot))
        return task_id

    def children(self, root_id: str) -> list[dict]:
        with self.db() as db:
            return [dict(r) for r in db.execute(
                'SELECT task_id,slot FROM task_groups WHERE root_id=? ORDER BY slot',(valid_id(root_id),))]

    def _group_rows(self, db, root_id: str):
        return db.execute("SELECT * FROM calls WHERE status!='released' AND (task_id=? OR task_id IN "
                          '(SELECT task_id FROM task_groups WHERE root_id=?))', (root_id,root_id)).fetchall()

    def group_usage(self, root_id: str) -> dict:
        with self.db() as db:
            rows = self._group_rows(db, valid_id(root_id))
        return {'calls':len(rows), 'tokens':sum(r['tokens'] for r in rows),
                'accounted_usd':str(sum((Decimal(r['accounted_usd']) for r in rows),Decimal(0))),
                'estimated_entries':sum(bool(r['estimated']) for r in rows)}

    def group_seconds_remaining(self, task_id: str) -> float | None:
        with self.db() as db:
            row=db.execute('SELECT t.* FROM tasks t JOIN task_groups g ON t.id=g.root_id WHERE g.task_id=?',
                           (valid_id(task_id),)).fetchone()
        if row is None:
            return None
        since=json.loads(row['checkpoint']).get('search',{}).get('segment_started')
        elapsed=row['active_seconds']+(max(0,time.time()-since) if since else 0)
        return max(0,json.loads(row['spec'])['limits']['seconds']-elapsed)

    def task(self, task_id: str) -> dict:
        valid_id(task_id)
        with self.db() as db:
            row = db.execute("SELECT * FROM tasks WHERE id=?", (task_id,)).fetchone()
        if row is None:
            raise ValueError("Task not found")
        value = dict(row)
        value["spec"] = json.loads(value["spec"])
        value["checkpoint"] = json.loads(value["checkpoint"])
        return value

    def tasks(self):
        with self.db() as db:
            return [dict(r) for r in db.execute(
                "SELECT id,state,phase,reason,quality,created FROM tasks ORDER BY created DESC")]

    def update(self, task_id, **fields):
        valid_id(task_id)
        allowed = {"state", "phase", "reason", "quality", "selected", "checkpoint", "rounds",
                   "reads", "active_seconds", "cancel_requested"}
        if not fields or not fields.keys() <= allowed:
            raise ValueError("Task contract is immutable")
        if "checkpoint" in fields:
            fields["checkpoint"] = dump(fields["checkpoint"])
        fields["updated"] = now()
        with self.db() as db:
            db.execute("UPDATE tasks SET " + ",".join(f"{k}=?" for k in fields) + " WHERE id=?",
                       (*fields.values(), task_id))

    def event(self, task_id, kind, detail):
        with self.db() as db:
            db.execute("INSERT INTO events(task_id,kind,detail,created) VALUES (?,?,?,?)",
                       (valid_id(task_id), kind, dump(detail), now()))

    def calls(self, task_id):
        with self.db() as db:
            return [self._call_dict(r) for r in db.execute(
                "SELECT * FROM calls WHERE task_id=? ORDER BY created,id", (valid_id(task_id),))]

    @staticmethod
    def _call_dict(row):
        value = dict(row)
        for key in ("request", "response"):
            value[key] = json.loads(value[key]) if value[key] else None
        return value

    def call(self, call_id):
        with self.db() as db:
            row = db.execute("SELECT * FROM calls WHERE id=?", (valid_id(call_id),)).fetchone()
        if row is None:
            raise ValueError("Call not found")
        return self._call_dict(row)

    def _budget(self, db):
        limit = Decimal(db.execute("SELECT value FROM settings WHERE key='budget_usd'").fetchone()[0])
        rows = db.execute("SELECT accounted_usd,estimated FROM calls WHERE status != 'released'").fetchall()
        total = sum((Decimal(r[0]) for r in rows), Decimal(0))
        return {"limit_usd": str(limit), "accounted_usd": str(total),
                "remaining_usd": str(max(Decimal(0), limit-total)),
                "estimated_entries": sum(bool(r[1]) for r in rows)}

    def budget(self):
        with self.db() as db:
            return self._budget(db)

    def reserve_call(self, task_id, role, messages, input_estimate, max_output, input_price, output_price):
        task = self.task(task_id)
        limits = task["spec"]["limits"]
        if input_estimate < 0 or max_output <= 0:
            raise ValueError("Invalid request size")
        quote = (input_price * input_estimate + output_price * max_output) / Decimal(1000000)
        call_id = uuid.uuid4().hex
        with self.db() as db:
            db.execute("BEGIN IMMEDIATE")
            rows = db.execute("SELECT tokens,accounted_usd FROM calls WHERE task_id=? AND status!='released'", (task_id,)).fetchall()
            if len(rows) >= limits["calls"]:
                raise BudgetExceeded("call_limit")
            if sum(r[0] for r in rows) + input_estimate + max_output > limits["tokens"]:
                raise BudgetExceeded("token_target")
            own_usd = sum((Decimal(r['accounted_usd']) for r in rows),Decimal(0))
            if 'budget_usd' in task['spec'] and own_usd + quote > Decimal(task['spec']['budget_usd']):
                raise BudgetExceeded('task_usd_limit')
            membership = db.execute('SELECT root_id FROM task_groups WHERE task_id=?',(task_id,)).fetchone()
            root_id = membership[0] if membership else (task_id if task['spec'].get('mode')=='multi' else None)
            if root_id:
                root = db.execute('SELECT * FROM tasks WHERE id=?',(root_id,)).fetchone()
                root_spec = json.loads(root['spec'])
                if root['cancel_requested']:
                    raise InterruptedError()
                if root['state'] != 'running':
                    raise BudgetExceeded('parent_closed')
                all_rows = self._group_rows(db,root_id)
                total_usd = sum((Decimal(r['accounted_usd']) for r in all_rows),Decimal(0))
                if len(all_rows) >= root_spec['limits']['calls']:
                    raise BudgetExceeded('group_call_limit')
                if sum(r['tokens'] for r in all_rows)+input_estimate+max_output > root_spec['limits']['tokens']:
                    raise BudgetExceeded('group_token_limit')
                if total_usd+quote > Decimal(root_spec['budget_usd']):
                    raise BudgetExceeded('group_usd_limit')
                child_usd = sum((Decimal(r['accounted_usd']) for r in all_rows if r['task_id']!=root_id),Decimal(0))
                if membership and child_usd+quote > Decimal(root_spec['budget_usd'])*Decimal('0.8'):
                    raise BudgetExceeded('candidate_pool_limit')
                checkpoint = json.loads(root['checkpoint'])
                since = checkpoint.get('search',{}).get('segment_started')
                elapsed = root['active_seconds'] + (max(0,time.time()-since) if since else 0)
                if elapsed >= root_spec['limits']['seconds']:
                    raise BudgetExceeded('group_time_limit')
            budget = self._budget(db)
            if Decimal(budget["accounted_usd"]) + quote > Decimal(budget["limit_usd"]):
                raise BudgetExceeded("project_usd_limit")
            db.execute("""INSERT INTO calls(id,task_id,role,status,request,input_estimate,max_output,
                tokens,input_price,output_price,accounted_usd,created,updated)
                VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (call_id, task_id, role, "reserved", dump(messages), input_estimate, max_output,
                 input_estimate+max_output, str(input_price), str(output_price), str(quote), now(), now()))
        return call_id

    def mark_dispatched(self, call_id):
        with self.db() as db:
            db.execute("UPDATE calls SET status='dispatched',updated=? WHERE id=? AND status='reserved'",
                       (now(), valid_id(call_id)))

    def complete_call(self, call_id, result):
        call = self.call(call_id)
        inp, out = result.get("input_tokens"), result.get("output_tokens")
        exact = (isinstance(inp, int) and not isinstance(inp, bool) and inp >= 0
                 and isinstance(out, int) and not isinstance(out, bool) and out >= 0)
        cost = ((Decimal(inp)*Decimal(call["input_price"])+Decimal(out)*Decimal(call["output_price"]))
                / Decimal(1000000)) if exact else Decimal(call["accounted_usd"])
        tokens = result.get("total_tokens") if exact else None
        if not isinstance(tokens, int) or tokens < 0:
            tokens = inp+out if exact else call["tokens"]
        with self.db() as db:
            db.execute("UPDATE calls SET status='succeeded',response=?,tokens=?,accounted_usd=?,estimated=?,updated=? WHERE id=?",
                       (dump(result), tokens, str(cost), int(not exact), now(), call_id))

    def fail_call(self, call_id, error, unknown=False):
        with self.db() as db:
            db.execute("UPDATE calls SET status=?,error=?,updated=? WHERE id=?",
                       ("outcome_unknown" if unknown else "failed", error, now(), valid_id(call_id)))

    def recover(self, task_id):
        with self.db() as db:
            db.execute("UPDATE calls SET status='outcome_unknown',error='interrupted before result persisted' WHERE task_id=? AND status='dispatched'", (valid_id(task_id),))
            db.execute("UPDATE calls SET status='released',accounted_usd='0' WHERE task_id=? AND status='reserved'", (task_id,))

    def save_artifact(self, task_id, kind, value):
        valid_id(task_id)
        if kind not in {"candidate", "review", "verification", "packet", "requirements", "comparison", "comparison_request"}:
            raise ValueError("Unsupported artifact kind")
        data = dump(value).encode("utf-8")
        digest = hashlib.sha256(data).hexdigest()
        relative = f"tasks/{task_id}/artifacts/{kind}-{digest}.json"
        path = self.root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        if path.exists():
            if path.read_bytes() != data:
                raise ValueError("Immutable artifact was modified")
        else:
            temp = path.with_suffix(f".{uuid.uuid4().hex}.tmp")
            with temp.open("xb") as f:
                f.write(data)
                f.flush()
                os.fsync(f.fileno())
            os.replace(temp, path)
        with self.db() as db:
            db.execute("INSERT OR IGNORE INTO artifacts VALUES (?,?,?,?)", (relative, task_id, kind, digest))
        return relative

    def read_artifact(self, relative):
        with self.db() as db:
            row = db.execute("SELECT sha256 FROM artifacts WHERE path=?", (relative,)).fetchone()
        path = (self.root / relative).resolve()
        if row is None or not path.is_relative_to(self.root):
            raise ValueError("Unknown artifact")
        data = path.read_bytes()
        if hashlib.sha256(data).hexdigest() != row[0]:
            raise ValueError("Artifact hash mismatch")
        return json.loads(data)
