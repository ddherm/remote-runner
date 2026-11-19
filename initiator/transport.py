"""Bounded SSH transport. All remote parameters travel as structured payloads."""
import json
import ntpath
import os
from pathlib import Path
import re
import subprocess
import sys

TOOLS = Path(__file__).resolve().parents[1] / "tools"
if str(TOOLS) not in sys.path:
    sys.path.insert(0, str(TOOLS))
from bundle_sync import fallback_ssh_options, python_ps, ssh_ps

DEFAULTS = dict(host="", address="", python=r"C:\Python\python.exe",
                runtime_root=r"C:\RemoteRunner\runtime",
                slot_root=r"C:\RemoteRunner\slots",
                remote_root=r"C:\RemoteRunner\workspaces", global_limit=18, max_supported=36)


def windows_path(value):
    if not isinstance(value, str) or not value or any(c in value for c in '\x00\r\n"<>|?*'):
        raise ValueError("请输入有效的 Windows 绝对路径")
    value = ntpath.normpath(value.replace("/", "\\"))
    drive, tail = ntpath.splitdrive(value)
    if not drive or not tail.startswith("\\") or tail == "\\":
        raise ValueError("请选择盘符下的文件夹，不要使用盘符根目录")
    return value


def profile(data):
    address = str(data.get("address") or data.get("host") or "").strip()
    if not re.fullmatch(r"[A-Za-z0-9_.@:\[\]-]+", address) or address.startswith("-"):
        raise ValueError("目标地址只能是 IP 或 SSH 别名")
    result = {key: data.get(key, DEFAULTS.get(key, "")) for key in
              ("python", "runtime_root", "slot_root", "user", "port", "identity_file")}
    result.update(address=address, label=str(data.get("label") or address).strip()[:100])
    for key in ("python", "runtime_root", "slot_root"):
        result[key] = windows_path(result[key])
    if result["port"]:
        result["port"] = int(result["port"])
        if not 1 <= result["port"] <= 65535:
            raise ValueError("SSH 端口必须在 1–65535 之间")
    if result["user"] and not re.fullmatch(r"[A-Za-z0-9_.\\-]+", result["user"]):
        raise ValueError("SSH 用户名格式无效")
    if result["identity_file"]:
        key = Path(result["identity_file"]).expanduser().resolve()
        if not key.is_file():
            raise ValueError("SSH 密钥文件不存在")
        result["identity_file"] = str(key)
    return result


class SSH:
    def __init__(self, host):
        self.host = host

    def fallback_options(self):
        if self.host["address"] != os.getenv("REMOTE_RUNNER_SSH_PRIMARY_ALIAS"):
            return ()
        hostname = os.getenv("REMOTE_RUNNER_SSH_FALLBACK_HOSTNAME")
        host_key_alias = os.getenv("REMOTE_RUNNER_SSH_HOST_KEY_ALIAS")
        return fallback_ssh_options(self.host["address"], hostname, host_key_alias)

    def can_fallback(self):
        return bool(self.fallback_options())

    def options(self, scp=False, fallback=False):
        args = ["-o", "BatchMode=yes", "-o", "ConnectTimeout=10", "-o", "StrictHostKeyChecking=yes",
                "-o", "ServerAliveInterval=10", "-o", "ServerAliveCountMax=3"]
        if self.host.get("user"):
            args += ["-o", "User=" + self.host["user"]]
        if self.host.get("port"):
            args += ["-P" if scp else "-p", str(self.host["port"])]
        if self.host.get("identity_file"):
            args += ["-i", self.host["identity_file"]]
        if fallback:
            args = [*self.fallback_options(), *args]
        return args

    def command(self, code, argv=(), fallback=False):
        wrapped = ssh_ps(self.host["address"], python_ps(self.host["python"], code, argv))
        return ["ssh", "-T"] + self.options(fallback=fallback) + wrapped[-2:]

    def run(self, code, argv=(), timeout=45):
        for fallback in (False, True):
            command = self.command(code, argv, fallback)
            result = subprocess.run(command, capture_output=True, encoding="utf-8", errors="replace", timeout=timeout)
            if result.returncode == 255 and not fallback and self.can_fallback():
                continue
            if result.returncode:
                raise RuntimeError("SSH 执行失败：" + (result.stderr or result.stdout)[-2000:])
            try:
                return json.loads(result.stdout.strip().splitlines()[-1])
            except (ValueError, IndexError) as exc:
                raise RuntimeError("远端没有返回有效状态：" + result.stdout[-1000:]) from exc
        raise RuntimeError("无法连接目标电脑")

    def upload(self, local, remote, timeout=180):
        for fallback in (False, True):
            command = ["scp"] + self.options(scp=True, fallback=fallback) + [str(local), self.host["address"] + ":" + remote.replace("\\", "/")]
            result = subprocess.run(command, capture_output=True, encoding="utf-8", errors="replace", timeout=timeout)
            if result.returncode == 255 and not fallback and self.can_fallback():
                continue
            if result.returncode:
                raise RuntimeError("上传失败：" + (result.stderr or result.stdout)[-2000:])
            return


PROBE = r'''
import ctypes,json,os,pathlib,platform,sys
if os.name!='nt':raise RuntimeError('目标必须是原生 Windows')
if sys.version_info<(3,9):raise RuntimeError('需要 Python 3.9 或更高版本')
runtime,slots=map(pathlib.Path,sys.argv[1:3]);policy=slots/'capacity.json'
limit=json.loads(policy.read_text(encoding='utf-8-sig'))['capacity'] if policy.exists() else 18
if not 1<=int(limit)<=36:raise RuntimeError('全机并行上限超出当前版本支持范围')
length=ctypes.c_ulong(512);account=ctypes.create_unicode_buffer(512)
if not ctypes.windll.secur32.GetUserNameExW(2,account,ctypes.byref(length)):raise ctypes.WinError()
print(json.dumps(dict(global_limit=int(limit),python=sys.executable,platform=platform.platform(),
 principal=account.value,
 runtime_available=(runtime/'deployments/current.json').exists()),ensure_ascii=False))
'''

BROWSE = r'''
import json,os,pathlib,string,sys
drives=[x+':\\' for x in string.ascii_uppercase if os.path.isdir(x+':\\')]
p=pathlib.Path(sys.argv[1]) if sys.argv[1] else None
if p and not p.is_absolute():raise ValueError('需要绝对路径')
rows=[];exists=bool(p.is_dir()) if p else True
if exists:
 for item in p.iterdir():
  try:
   if item.is_dir():rows.append(dict(name=item.name,path=str(item)))
  except OSError:pass
print(json.dumps(dict(path=str(p) if p else '',parent=str(p.parent) if p and p.parent!=p else '',
 drives=drives,directories=sorted(rows,key=lambda x:x['name'].casefold()),exists=exists),ensure_ascii=False))
'''

READ_LOG = r'''
import codecs,json,pathlib,sys
p=pathlib.Path(sys.argv[1]).resolve();root=pathlib.Path(sys.argv[2]).resolve();p.relative_to(root)
if p.name not in ('stdout.log','stderr.log','supervisor.log'):raise ValueError('不支持的日志')
cursor=max(0,int(sys.argv[3]));reset=False
if not p.is_file():print(json.dumps(dict(text='',cursor=0,reset=cursor>0)));sys.exit(0)
size=p.stat().st_size
if cursor>size:cursor=0;reset=True
with p.open('rb') as f:
 f.seek(cursor);data=f.read(65536)
decoder=codecs.getincrementaldecoder('utf-8')('replace');text=decoder.decode(data,final=False)
pending=decoder.getstate()[0]
print(json.dumps(dict(text=text,cursor=cursor+len(data)-len(pending),reset=reset),ensure_ascii=False))
'''
