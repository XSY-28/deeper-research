#!/usr/bin/env python3
"""Validate/export an explicit public file set. No GitHub, Git or model calls."""
import argparse
import hashlib
import json
from pathlib import Path,PurePosixPath
import re
import sys

PRIVATE_PARTS={'workspace','.work','.venv','.git','evals','__pycache__','build','dist'}
PRIVATE_NAMES={'config.json','notes.md','task_plan.md'}
TEXT_SUFFIXES={'.py','.md','.json','.toml','.txt','.in','.yaml','.yml'}
PATTERNS={
    'token':re.compile(r'\b(?:sk|ghp|gho|github_pat|xox[baprs])[-_][A-Za-z0-9_-]{20,}'),
    'private-key':re.compile(r'-----BEGIN (?:[A-Z0-9 ]+ )?PRIVATE KEY-----'),
    'personal-path':re.compile(r'/(?:Users|home)/[^/\s\"\'<>]+'),
}
KEY_NAME=r'(?:api[_-]?key|access[_-]?token|secret[_-]?key|[A-Z][A-Z0-9_]*_API_KEY)'
ASSIGNMENTS=(
    re.compile(r'''(?i)["']'''+KEY_NAME+r'''["']\s*:\s*["']([^"'\r\n]+)["']'''),
    re.compile(r'(?im)^[ \t]*'+KEY_NAME+r'\s*=\s*([^\r\n]+)'),
)


class ReleaseError(ValueError):
    def __init__(self,kind: str,file: str='manifest'):
        # File names can themselves contain credentials; never echo a match.
        safe='<redacted>' if any(p.search(file) for p in PATTERNS.values()) else file
        self.kind,self.file=kind,safe
        super().__init__(kind)


def collect(root: Path) -> dict[str,bytes]:
    manifest=root/'release-files.json'
    if manifest.is_symlink():raise ReleaseError('symlink','release-files.json')
    try:names=json.loads(manifest.read_text(encoding='utf-8'))['files']
    except (OSError,ValueError,KeyError,TypeError):raise ReleaseError('invalid-manifest') from None
    if not isinstance(names,list) or not names or any(not isinstance(n,str) for n in names) or len(names)!=len(set(names)):
        raise ReleaseError('invalid-manifest')
    payload={}
    for name in names:
        path=PurePosixPath(name)
        if not name or path.is_absolute() or '..' in path.parts or '\\' in name or path.as_posix()!=name:
            raise ReleaseError('invalid-manifest')
        if (any(p.casefold() in PRIVATE_PARTS or p.casefold().endswith('.egg-info') for p in path.parts)
                or path.name.casefold() in PRIVATE_NAMES or path.name.casefold().startswith('continue_handoff')
                or (path.name.casefold().startswith('.env') and name!='.env.example')
                or path.suffix.lower() in {'.db','.sqlite','.sqlite3','.pem','.key','.pdf','.log'}):
            raise ReleaseError('private-path',name)
        source=root/name
        if any(p.is_symlink() for p in [source,*list(source.parents)[:len(path.parts)-1]]):
            raise ReleaseError('symlink',name)
        if not source.is_file():raise ReleaseError('missing-file',name)
        if path.suffix not in TEXT_SUFFIXES and name not in {'.gitignore','.env.example','LICENSE'}:
            raise ReleaseError('unsupported-file',name)
        data=source.read_bytes()
        try:text=data.decode('utf-8')
        except UnicodeError:raise ReleaseError('non-text-file',name) from None
        for kind,pattern in PATTERNS.items():
            if pattern.search(text):raise ReleaseError(kind,name)
        for pattern in ASSIGNMENTS:
            for match in pattern.finditer(text):
                value=match[1].strip().strip("\"'").casefold()
                if value not in {'secret','true','false','none'} and not value.startswith(('dummy','test','replace','your','example')):
                    raise ReleaseError('credential-literal',name)
        payload[name]=data
    return payload


def prepare(root: Path,output: Path | None=None) -> dict:
    payload=collect(root)
    if output is not None:
        if output.exists() or output.is_symlink():raise ReleaseError('destination-exists')
        output=output.resolve()
        if output.is_relative_to(root) and not output.is_relative_to(root/'.work'):
            raise ReleaseError('unsafe-destination')
        output.mkdir(parents=True)
        for name,data in payload.items():
            target=output/name;target.parent.mkdir(parents=True,exist_ok=True);target.write_bytes(data)
    return {'ok':True,'files':{name:hashlib.sha256(data).hexdigest() for name,data in payload.items()},
            'count':len(payload),'exported':output is not None,'network_or_publish':False,
            'boundary':'Explicit file selection and heuristic redacted scanning; no private .env/workspace traversal or publication.'}


def main() -> int:
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--root',type=Path,default=Path(__file__).resolve().parents[1])
    group=parser.add_mutually_exclusive_group(required=True)
    group.add_argument('--check',action='store_true');group.add_argument('--output',type=Path)
    args=parser.parse_args()
    try:result=prepare(args.root.resolve(),args.output)
    except ReleaseError as error:
        print(json.dumps({'ok':False,'kind':error.kind,'file':error.file}),file=sys.stderr);return 1
    except OSError:
        print(json.dumps({'ok':False,'kind':'filesystem-error'}),file=sys.stderr);return 1
    print(json.dumps(result,ensure_ascii=False,indent=2));return 0


if __name__=='__main__':raise SystemExit(main())
