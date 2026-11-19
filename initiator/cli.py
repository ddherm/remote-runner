"""Codex handoff: open a configuration session, wait for deployment, read status."""
import argparse
import json
import os
from pathlib import Path
import signal
import subprocess
import sys
import threading
import time
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen
import webbrowser

from .deployment import RUNNER_ROOT
from .server import make_server
from .service import Service

STATE = RUNNER_ROOT / "initiator" / "state"


def request_api(runtime, route, data=None):
    headers = {"X-Runner-Token": runtime["token"]}
    if data is not None:
        headers["Content-Type"] = "application/json"
    request = Request(runtime["url"] + route, data=json.dumps(data, ensure_ascii=False).encode("utf-8") if data is not None else None,
                      headers=headers)
    try:
        with urlopen(request, timeout=45) as response:
            return json.load(response)
    except HTTPError as exc:
        try:
            error = json.load(exc)["error"]
        except (ValueError, KeyError):
            error = str(exc)
        raise RuntimeError(error) from exc


def runtime_info(directory):
    path = Path(directory) / "runtime.json"
    data = json.loads(path.read_text(encoding="utf-8"))
    request_api(data, "/api/bootstrap")
    return data


def ensure_service(directory):
    directory = Path(directory).resolve()
    try:
        return runtime_info(directory)
    except (OSError, ValueError, RuntimeError):
        pass
    directory.mkdir(parents=True, exist_ok=True, mode=0o700)
    with (directory / "service.log").open("ab") as log:
        command = [sys.executable, "-B", str(RUNNER_ROOT / "tools" / "initiator.py"),
                   "--state-dir", str(directory), "serve"]
        subprocess.Popen(command, cwd=str(RUNNER_ROOT), stdin=subprocess.DEVNULL, stdout=log, stderr=log,
                         close_fds=True, start_new_session=(os.name != "nt"),
                         creationflags=(0x00000008 | 0x00000200) if os.name == "nt" else 0)
    deadline = time.monotonic() + 15
    while time.monotonic() < deadline:
        try:
            return runtime_info(directory)
        except (OSError, ValueError, RuntimeError):
            time.sleep(0.15)
    raise RuntimeError("本地后台未启动，请查看 " + str(directory / "service.log"))


def serve(directory, port):
    directory.mkdir(parents=True, exist_ok=True, mode=0o700)
    lock = (directory / "daemon.lock").open("a+b")
    try:
        if os.name == "nt":
            import msvcrt
            lock.seek(0)
            msvcrt.locking(lock.fileno(), msvcrt.LK_NBLCK, 1)
        else:
            import fcntl
            fcntl.flock(lock.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        return
    service = Service(directory)
    server, token = make_server(service, port=port)
    runtime = dict(pid=os.getpid(), url="http://127.0.0.1:%d" % server.server_port, token=token)
    info = directory / "runtime.json"
    temporary = directory / "runtime.json.tmp"
    temporary.write_text(json.dumps(runtime), encoding="utf-8")
    temporary.chmod(0o600)
    temporary.replace(info)
    def shutdown(*args):
        service.close()
        threading.Thread(target=server.shutdown, daemon=True).start()
    for name in (signal.SIGINT, signal.SIGTERM):
        signal.signal(name, shutdown)
    service.recover()
    try:
        server.serve_forever(poll_interval=0.25)
    finally:
        service.close()
        server.server_close()
        try:
            if json.loads(info.read_text())["pid"] == os.getpid():
                info.unlink()
        except (OSError, ValueError, KeyError):
            pass
        lock.close()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--state-dir", type=Path, default=STATE)
    sub = parser.add_subparsers(dest="action", required=True)
    daemon = sub.add_parser("serve", help="运行独立本地后台")
    daemon.add_argument("--port", type=int, default=0)
    opening = sub.add_parser("open", help="创建或打开实验配置界面")
    opening.add_argument("--request", type=Path)
    opening.add_argument("--session")
    opening.add_argument("--no-browser", action="store_true")
    status = sub.add_parser("status", help="读取当前实验状态")
    status.add_argument("--session", required=True)
    waiting = sub.add_parser("wait", help="有界等待部署或完成事件")
    waiting.add_argument("--session", required=True)
    waiting.add_argument("--until", choices=("deployed", "finished"), default="deployed")
    waiting.add_argument("--timeout", type=float, default=55)
    sub.add_parser("list", help="列出会话和已保存连接")
    args = parser.parse_args()
    if args.action == "serve":
        serve(args.state_dir.resolve(), args.port)
        return
    runtime = ensure_service(args.state_dir)
    if args.action == "open":
        if args.session and args.request:
            raise ValueError("--session 与 --request 不能同时指定")
        if args.session:
            session = request_api(runtime, "/api/sessions/" + args.session)
        else:
            body = json.loads(args.request.read_text(encoding="utf-8-sig")) if args.request else dict(
                title="新建远程实验", summary="等待 Codex 准备任务清单后，选择目标电脑并部署。")
            if args.request and body.get("source_root") and not Path(body["source_root"]).is_absolute():
                body["source_root"] = str((args.request.resolve().parent / body["source_root"]).resolve())
            session = request_api(runtime, "/api/sessions/create", dict(request=body))
        url = runtime["url"] + "/?session=" + session["id"] + "#token=" + runtime["token"]
        if not args.no_browser:
            webbrowser.open(url)
        result = dict(session_id=session["id"], status=session["status"], url=url)
    elif args.action == "list":
        result = request_api(runtime, "/api/bootstrap")
    elif args.action == "status":
        result = request_api(runtime, "/api/sessions/" + args.session)
    else:
        deadline = time.monotonic() + min(55, max(0, args.timeout))
        expected = {"completed", "complete", "failed", "interrupted"}
        if args.until == "deployed":
            expected |= {"running", "monitoring", "disconnected"}
        while True:
            result = request_api(runtime, "/api/sessions/" + args.session)
            if result["status"] in expected or time.monotonic() >= deadline:
                result["wait_timed_out"] = result["status"] not in expected
                break
            time.sleep(min(1, max(0, deadline - time.monotonic())))
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    try:
        main()
    except (ValueError, RuntimeError, OSError) as exc:
        print(json.dumps(dict(error=str(exc)), ensure_ascii=False), file=sys.stderr)
        raise SystemExit(1)
