"""Command-line composition; no implicit provider calls in read-only commands."""
import argparse
from decimal import Decimal
import json
from importlib.resources import files
import os
from pathlib import Path
import sys

from .corpus import Corpus
from .engine import Engine
from .multicandidate import MultiCandidate
from .evaluation import export_blind, run_evaluation
from .providers import ProviderError, ProviderProfile, create_provider
from .storage import Store


def emit(value):
    print(json.dumps(value, ensure_ascii=False, indent=2))


def load_env(path, names):
    if not path.exists():
        return
    for line in path.read_text(encoding="utf-8").splitlines():
        key, separator, value = line.partition("=")
        key = key.strip()
        if separator and key in names:
            os.environ.setdefault(key, value.strip().strip("\"'"))


def parser():
    p = argparse.ArgumentParser(prog="deeper", description="Local evidence-guided research")
    p.add_argument("--config", default="config.json")
    p.add_argument("--workspace")
    sub = p.add_subparsers(dest="command", required=True)
    init=sub.add_parser("init")
    interactive=init.add_mutually_exclusive_group()
    interactive.add_argument('--interactive',action='store_true',dest='interactive',help='Configure profile and hidden API key locally (requires a terminal)')
    interactive.add_argument('--no-interactive',action='store_false',dest='interactive',help='Create local configuration without prompts')
    init.set_defaults(interactive=None)
    imp = sub.add_parser("import")
    imp.add_argument("path"); imp.add_argument("--title"); imp.add_argument("--url")
    sub.add_parser("sources"); sub.add_parser("list"); sub.add_parser("budget")
    create = sub.add_parser("create")
    q = create.add_mutually_exclusive_group(required=True)
    q.add_argument("--question"); q.add_argument("--question-file")
    create.add_argument("--criterion", action="append", required=True)
    create.add_argument("--source", action="append", required=True)
    create.add_argument("--effort", choices=["low", "medium", "high"])
    create.add_argument('--mode', choices=['single','multi','single-budgeted'], default='single')
    create.add_argument('--task-budget', help='USD ceiling for this task and all its candidates; multi defaults to 0.30')
    create.add_argument("--profile")
    for name in ("run", "show", "cancel", "resume", "fork"):
        s = sub.add_parser(name); s.add_argument("task_id")
        if name == "resume":
            s.add_argument("--retry", action="store_true", help="Explicitly permit a new paid attempt after an uncertain/failed call")
        if name == "fork":
            s.add_argument("--effort", choices=["low", "medium", "high"])
            s.add_argument("--profile")
    ev = sub.add_parser("eval").add_subparsers(dest="eval_command", required=True)
    run = ev.add_parser("run")
    run.add_argument("--manifest", default="evals/manifest.json")
    run.add_argument("--set", choices=["development", "evaluation"], default="evaluation")
    run.add_argument("--efforts", default="low,medium,high")
    run.add_argument("--id"); run.add_argument("--limit", type=int); run.add_argument("--profile")
    export = ev.add_parser("export"); export.add_argument("run_file"); export.add_argument("output_dir")
    export.add_argument("--include-initial",action="store_true",help="Include changed initial drafts while keeping their stage hidden")
    return p


def main(argv=None):
    args = parser().parse_args(argv)
    try:
        config_path = Path(args.config).resolve()
        if args.command == 'init':
            fresh=not config_path.exists()
            interactive=args.interactive is True or (args.interactive is None and fresh and sys.stdin.isatty())
            if interactive:
                from .setup_wizard import configure
                try:confirmed=configure(config_path)
                except (EOFError,KeyboardInterrupt):confirmed=False
                if not confirmed:
                    print('已取消配置。',file=sys.stderr)
                    return 130
            elif fresh:
                config_path.parent.mkdir(parents=True,exist_ok=True)
                with config_path.open('x',encoding='utf-8') as output:
                    output.write(files('deeper_research').joinpath('defaults.json').read_text(encoding='utf-8'))
        if not config_path.exists():
            raise ValueError("Configuration missing; run deeper init first")
        config = json.loads(config_path.read_text(encoding="utf-8"))
        if config.get("ca_bundle"):
            ca_bundle = (config_path.parent/config["ca_bundle"]).resolve()
            if not ca_bundle.is_file():
                raise ValueError("Configured CA certificate bundle does not exist")
            os.environ["SSL_CERT_FILE"] = str(ca_bundle)
        profiles = {name: ProviderProfile.from_dict(data) for name,data in config["profiles"].items()}
        workspace = Path(args.workspace) if args.workspace else config_path.parent/config["workspace"]
        store, corpus = Store(workspace, Decimal(config["project_budget_usd"])), Corpus(workspace)
        if args.command == "init":
            emit({"config": str(config_path), "workspace": str(store.root), "budget": store.budget()})
        elif args.command == "import":
            emit({"source_id": corpus.ingest(Path(args.path), args.title, args.url)})
        elif args.command == "sources":
            emit(corpus.list_sources())
        elif args.command == "list":
            emit(store.tasks())
        elif args.command == "budget":
            emit(store.budget())
        elif args.command == "create":
            profile = profiles[args.profile or config["default_profile"]]
            known = {s["id"] for s in corpus.list_sources() if s["status"] != "corrupt"}
            if not set(args.source) <= known:
                raise ValueError("Unknown or corrupt source; import and inspect sources first")
            question = args.question if args.question is not None else Path(args.question_file).read_text(encoding="utf-8")
            spec={"question": question, "criteria": args.criterion, "source_ids": args.source,
                  "effort": args.effort or ('medium' if args.mode=='single' else 'high'),
                  'mode':args.mode, "profile": profile.public_dict()}
            if args.task_budget is not None:spec['budget_usd']=args.task_budget
            task_id = store.create_task(spec)
            emit({"task_id": task_id})
        elif args.command == "show":
            emit(store.task(args.task_id))
        elif args.command == "cancel":
            task = store.task(args.task_id)
            store.update(args.task_id, cancel_requested=1)
            if task["state"] != "running":
                store.update(args.task_id, state="cancelled", reason="user_cancel")
            emit({"task_id": args.task_id, "cancel_requested": True})
        elif args.command == "fork":
            old = store.task(args.task_id)["spec"]
            profile = profiles[args.profile].public_dict() if args.profile else old["profile"]
            new = {**old, "profile": profile, "effort": args.effort or old["effort"], "parent_task_id": args.task_id}
            for key in ('group_root','candidate_number'):
                new.pop(key,None)
            emit({"task_id": store.create_task(new), "parent_task_id": args.task_id})
        elif args.command in {"run", "resume"}:
            task = store.task(args.task_id)
            profile = ProviderProfile.from_dict(task["spec"]["profile"])
            load_env(config_path.parent/".env", {profile.api_key_env})
            controller=MultiCandidate if task['spec'].get('mode')=='multi' else Engine
            result = controller(store, corpus, create_provider(profile)).run(args.task_id,
                        retry_unknown=args.command == "resume" and args.retry)
            emit({k: result[k] for k in ("id", "state", "quality", "reason", "selected")})
            return 1 if result["state"] in {"failed", "interrupted"} else 0
        elif args.command == "eval" and args.eval_command == "run":
            profile = profiles[args.profile or config["default_profile"]]
            efforts = args.efforts.split(",")
            if not set(efforts) <= {"low", "medium", "high"}:
                raise ValueError("Unknown effort")
            load_env(config_path.parent/".env", {profile.api_key_env})
            provider = create_provider(profile)
            provider.validate_credentials()
            path = run_evaluation(store, corpus, provider, Path(args.manifest), config_path.parent,
                                  args.set, efforts, args.id, args.limit)
            emit({"run_file": str(path), "budget": store.budget()})
        elif args.command == "eval":
            emit(export_blind(store, args.run_file, args.output_dir,include_initial=args.include_initial))
        return 0
    except (OSError, ValueError, KeyError, RuntimeError, ProviderError) as exc:
        # Provider errors are sanitized by the adapter; never print request headers/credential values.
        print(f"Error: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
