"""Interactive local configuration. No provider construction or network access."""
import getpass
import json
import os
from pathlib import Path
import sys
import tempfile
import warnings
from importlib.resources import files

from .providers import ProviderProfile


def _ask(prompt: str) -> str:
    print(prompt,end='',file=sys.stderr,flush=True)
    return input().strip()


def _confirm(prompt: str, default: bool = False) -> bool:
    while True:
        answer=_ask(prompt+(' [Y/n] ' if default else ' [y/N] ')).casefold()
        if not answer:return default
        if answer in {'y','yes','是'}:return True
        if answer in {'n','no','否'}:return False
        print('请输入 y 或 n。',file=sys.stderr)


def _read_local(path: Path) -> bytes | None:
    if path.is_symlink():raise ValueError('配置向导不读写符号链接密钥文件，请手动配置或使用环境变量。')
    if not path.exists():return None
    if not path.is_file():raise ValueError('配置目标必须是普通文件。')
    return path.read_bytes()


def _atomic_write(path: Path, data: bytes, previous: bytes | None) -> None:
    if _read_local(path)!=previous:raise ValueError('配置文件已变化，未覆盖；请重新运行向导。')
    path.parent.mkdir(parents=True,exist_ok=True)
    # .env.* is already ignored. A killed process must not leave a normally
    # trackable secret temporary file. mkstemp creates it with mode0600.
    fd,name=tempfile.mkstemp(prefix=path.name+'.',suffix='.tmp',dir=path.parent)
    try:
        with os.fdopen(fd,'wb') as output:
            output.write(data);output.flush();os.fsync(output.fileno())
        if _read_local(path)!=previous:raise ValueError('配置文件已变化，未覆盖；请重新运行向导。')
        os.replace(name,path)
    finally:
        Path(name).unlink(missing_ok=True)


def _secret(name: str) -> str | None:
    while True:
        try:
            with warnings.catch_warnings():
                warnings.simplefilter('error',getpass.GetPassWarning)
                value=getpass.getpass(f'{name}（隐藏输入，回车跳过）：',stream=sys.stderr)
        except getpass.GetPassWarning:
            raise ValueError('终端无法安全隐藏输入，未读取密钥；请使用环境变量或手动配置。') from None
        if not value:return None
        # Match the existing header-safe credential contract and ensure the
        # existing dotenv loader will round-trip the value without alteration.
        if value.isascii() and all(32<ord(c)<127 for c in value) and value==value.strip("\"'"):
            return value
        print('密钥格式不适合保存，请仅粘贴密钥本体；不要包含空白或包裹引号。',file=sys.stderr)


def _env_text(original: str, name: str, value: str) -> str:
    newline='\r\n' if '\r\n' in original else '\n';lines=[];replaced=False
    for line in original.splitlines(keepends=True):
        key,separator,_=line.partition('=')
        if separator and key.strip()==name:
            if not replaced:lines.append(name+'='+value+newline);replaced=True
        else:lines.append(line)
    if not replaced:
        if lines and not lines[-1].endswith(('\n','\r')):lines.append(newline)
        lines.append(name+'='+value+newline)
    return ''.join(lines)


def configure(config_path: Path) -> bool:
    """Gather first, confirm, then save. True means setup confirmed locally."""
    if not sys.stdin.isatty():raise ValueError('交互配置需要终端；脚本请用 init --no-interactive。')
    env_path=config_path.parent/'.env'
    if config_path==env_path:raise ValueError('JSON配置文件不能命名为.env。')
    previous_config=_read_local(config_path);previous_env=_read_local(env_path)
    config=json.loads(previous_config) if previous_config is not None else json.loads(files('deeper_research').joinpath('defaults.json').read_text(encoding='utf-8'))
    profiles={name:ProviderProfile.from_dict(value) for name,value in config['profiles'].items()}
    names=list(profiles);default=config['default_profile']
    if default not in profiles:raise ValueError('默认profile不存在，请先检查JSON配置。')
    try:original_env=previous_env.decode('utf-8') if previous_env is not None else ''
    except UnicodeError:raise ValueError('.env不是UTF-8文本，未修改，请手动检查。') from None
    print('Deeper Research 首次配置：只保存本地设置，不联网、不验证账号有效性、不产生模型费用。',file=sys.stderr)
    for i,name in enumerate(names,1):
        p=profiles[name];print(f'  {i}. {name} — {p.protocol} / {p.model}',file=sys.stderr)
    while True:
        choice=_ask(f'选择profile [{names.index(default)+1}]：')
        if not choice:selected=default;break
        if choice in profiles:selected=choice;break
        if choice.isdecimal() and 1<=int(choice)<=len(names):selected=names[int(choice)-1];break
        print('请输入列表中的编号或profile名称。',file=sys.stderr)
    profile=profiles[selected];key_name=profile.api_key_env
    exists_in_file=any(separator and key.strip()==key_name and value.strip().strip("\"'")
                       for key,separator,value in (line.partition('=') for line in original_env.splitlines()))
    exists_in_environment=bool(os.environ.get(key_name))
    if exists_in_environment:
        print(f'当前环境变量 {key_name} 已设置，运行时优先于.env；向导不会修改系统环境变量。',file=sys.stderr)
    replace=not (exists_in_file or exists_in_environment) or _confirm('已有密钥值。要在本地.env写入新值吗？')
    secret=_secret(key_name) if replace else None
    print(f'配置：{config_path}\n默认profile：{selected}\n服务地址：{profile.base_url}',file=sys.stderr)
    print(f'密钥：写入 {env_path}（权限0600）' if secret else '密钥：保留已有值或跳过；没有设置时可稍后配置。',file=sys.stderr)
    if not _confirm('保存这些本地设置？',default=True):return False
    updated={**config,'default_profile':selected}
    try:
        # Save the key first: a key-write failure must not switch the default
        # profile or make first init look finished on a normal retry.
        if secret is not None:
            _atomic_write(env_path,_env_text(original_env,key_name,secret).encode('utf-8'),previous_env)
        if previous_config is None or selected!=default:
            _atomic_write(config_path,(json.dumps(updated,ensure_ascii=False,indent=2)+'\n').encode('utf-8'),previous_config)
    except (OSError,ValueError,KeyboardInterrupt):
        raise ValueError('本地保存未完成，部分文件可能已保存；请使用相同--config重新运行 init --interactive。未发出API请求。') from None
    print('本地配置已保存。未发出API请求；密钥的账号权限及有效性尚未联网验证。',file=sys.stderr)
    return True
