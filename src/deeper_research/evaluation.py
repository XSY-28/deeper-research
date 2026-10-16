"""Fixed-corpus evaluation and blinded exports; answer keys never enter solving."""
import hashlib
import json
from pathlib import Path
import random
import sys
import uuid
import shutil

from .engine import Engine
from .contracts import CODE_VERSION
from .corpus import Corpus


def load_manifest(path, project_root):
    data = json.loads(Path(path).read_text(encoding="utf-8"))
    if data.get("version") != 1:
        raise ValueError("Unsupported evaluation manifest version")
    for source in data["sources"].values():
        file = Path(project_root) / source["path"]
        if hashlib.sha256(file.read_bytes()).hexdigest() != source["sha256"]:
            raise ValueError(f"Evaluation source changed: {source['path']}")
    for group in ("development", "evaluation"):
        for case in data[group]:
            if not case["sources"] or not set(case["sources"]) <= data["sources"].keys():
                raise ValueError("Invalid evaluation source scope")
    return data


def run_evaluation(store, corpus, provider, manifest_path, project_root, group="evaluation",
                   efforts=("low", "medium", "high"), case_id=None, limit=None):
    manifest = load_manifest(manifest_path, project_root)
    cases = [c for c in manifest[group] if case_id is None or c["id"] == case_id]
    if not cases:
        raise ValueError("No matching evaluation cases")
    if limit:
        cases = cases[:limit]
    imported = {}
    for key in {k for case in cases for k in case["sources"]}:
        source = manifest["sources"][key]
        imported[key] = corpus.ingest(Path(project_root)/source["path"], title=source["title"], url=source["url"])
    directory = store.root/"evaluations"/uuid.uuid4().hex
    directory.mkdir(parents=True)
    report = {"manifest_sha256": hashlib.sha256(Path(manifest_path).read_bytes()).hexdigest(),
              "implementation_sha256": CODE_VERSION,
              "python": sys.version.split()[0],
              "group": group, "profile": provider.profile.public_dict(), "runs": []}
    path = directory/"runs.json"
    for case in cases:
        for effort in efforts:
            task_id = store.create_task({"question": case["question"], "criteria": case["criteria"],
                "source_ids": [imported[k] for k in case["sources"]], "effort": effort,
                "profile": provider.profile.public_dict(), "evaluation_case": case["id"]})
            record = {"case_id": case["id"], "effort": effort, "task_id": task_id, "state": "ready"}
            report["runs"].append(record)
            path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
            task = Engine(store, corpus, provider).run(task_id)
            record.update({k: task[k] for k in ("state", "quality", "reason")})
            path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
            print(json.dumps(record, ensure_ascii=False), flush=True)
            calls = store.calls(task_id)
            invalid_answer = (task["state"] == "failed" and calls
                              and calls[-1]["error"] == "response failed local contract validation")
            if (task["state"] == "interrupted" or (task["state"] == "failed" and not invalid_answer)
                    or store.budget()["remaining_usd"] == "0"):
                return path
    return path


def export_blind(store, run_path, output_dir, include_initial=False):
    report = json.loads(Path(run_path).read_text(encoding="utf-8"))
    records = [{**r,"version":"selected"} for r in report["runs"]]
    if include_initial:
        for record in list(records):
            task = store.task(record["task_id"])
            initial = task["checkpoint"].get("initial")
            if initial and initial != task["selected"]:
                records.append({**record,"version":"initial"})
    random.SystemRandom().shuffle(records)
    root = Path(output_dir)
    answers = root/"answers"
    answers.mkdir(parents=True, exist_ok=False)
    mapping = []
    source_blocks = {}
    cases = {}
    corpus = Corpus(store.root)
    source_metadata = {s["id"]:s for s in corpus.list_sources()}
    catalog = {}
    for index, record in enumerate(records, 1):
        task = store.task(record["task_id"])
        alias = f"answer-{index:03}"
        selected = task["checkpoint"].get("initial") if record["version"] == "initial" else task["selected"]
        candidate = store.read_artifact(selected) if selected else {"answer": "No answer produced", "claims": [], "open_issues": []}
        text = f"# {task['spec']['question']}\n\n{candidate['answer']}\n\n"
        text += "引用与待核查主张：\n\n" + json.dumps(candidate["claims"], ensure_ascii=False, indent=2)
        (answers/f"{alias}.md").write_text(text, encoding="utf-8")
        mapping.append({"alias": alias, **record})
        case = cases.setdefault(record["case_id"], {
            "case_id": record["case_id"], "question": task["spec"]["question"],
            "criteria": task["spec"]["criteria"],
            "source_ids": sorted(set(task["spec"]["source_ids"])), "answers": []})
        if (case["question"] != task["spec"]["question"] or case["criteria"] != task["spec"]["criteria"]
                or case["source_ids"] != sorted(set(task["spec"]["source_ids"]))):
            raise ValueError("Blind comparison requires identical question, criteria and source versions within a case")
        case["answers"].append(alias)
        for source_id in case["source_ids"]:
            if source_id in catalog:
                continue
            metadata = source_metadata.get(source_id)
            if metadata is None or metadata["status"] == "corrupt":
                raise ValueError("Blind comparison source is missing or corrupt")
            original = store.root/metadata["snapshot_path"]
            relative = Path("originals")/source_id/original.name
            target = root/relative
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(original,target)
            if hashlib.sha256(target.read_bytes()).hexdigest() != metadata["sha256"]:
                raise ValueError("Blind source copy hash mismatch")
            catalog[source_id] = {k:metadata[k] for k in
                ("title","url","sha256","parser_version","status","warnings")}
            catalog[source_id]["snapshot_path"] = str(relative)
        references = {i for c in candidate["claims"] for i in c["evidence_ids"]}
        references.update(candidate.get("body_evidence_ids", []))
        for block_id in references:
            source_blocks[block_id] = corpus.get_block(block_id, task["spec"]["source_ids"])
    key = root/"mapping.json"
    key.write_text(json.dumps(mapping, ensure_ascii=False, indent=2), encoding="utf-8")
    sources = root/"sources.json"
    sources.write_text(json.dumps(source_blocks, ensure_ascii=False, indent=2), encoding="utf-8")
    criteria = root/"criteria.json"
    criteria.write_text(json.dumps(list(cases.values()), ensure_ascii=False, indent=2), encoding="utf-8")
    source_catalog = root/"source-catalog.json"
    source_catalog.write_text(json.dumps(catalog,ensure_ascii=False,indent=2),encoding="utf-8")
    return {"answers": str(answers), "mapping": str(key), "sources": str(sources),
            "criteria": str(criteria), "source_catalog": str(source_catalog), "count": len(records)}
