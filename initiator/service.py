"""Independent local service, durable sessions and bounded remote observers."""
import copy
import json
import ntpath
import os
from pathlib import Path
import queue
import secrets
import subprocess
import threading
import time
import uuid

from .deployment import (RECONCILE, START, build_runtime, prepare_package, upload_package)
from .store import Store
from .transport import BROWSE, DEFAULTS, PROBE, READ_LOG, SSH, profile, windows_path


DRAFT_STATES = ("draft", "ready", "configured")
FINISHED_STATES = ("complete", "completed", "failed")


def parallel_limit(value):
    try:
        parallel = int(value)
    except (TypeError, ValueError, OverflowError) as exc:
        raise ValueError("并行上限必须在 1–36 之间") from exc
    if isinstance(value, bool) or (isinstance(value, float) and value != parallel) or not 1 <= parallel <= 36:
        raise ValueError("并行上限必须在 1–36 之间")
    return parallel


class Service:
    def __init__(self, directory, ssh_factory=SSH):
        self.store = Store(directory)
        self.ssh_factory = ssh_factory
        self.lock = threading.RLock()
        self.host_locks = {}
        self.working = set()
        self.observers = {}
        self.observer_stops = {}
        self.observer_processes = {}
        self.closed = threading.Event()

    def recover(self):
        for session in self.store.list("session"):
            observed_session = session["status"] in ("running", "monitoring", "disconnected", "completed", "complete")
            observed_session = observed_session or (session["status"] == "failed" and session.get("snapshot"))
            if observed_session and session.get("runtime_release"):
                self.start_observer(session["id"])
            elif session["status"] in ("deploying", "preparing", "uploading", "verifying", "launching"):
                self.store.event(session["id"], "interrupted", "后台已恢复；请重连核对远端状态，不会自动重复启动。")

    def create(self, request):
        if not isinstance(request, dict):
            raise ValueError("需要 Codex 生成的请求对象")
        if request.get("mode", "deploy") not in ("deploy", "monitor"):
            raise ValueError("不支持的请求模式")
        if request.get("mode") != "monitor" and (request.get("manifest_template") or request.get("source_root")):
            source = Path(request.get("source_root", "")).resolve()
            if not source.is_dir():
                raise ValueError("任务源目录不存在")
            request = dict(request, source_root=str(source))
        summary = request.get("summary", "等待 Codex 准备任务清单后，在此选择目标电脑并部署。")
        if not isinstance(summary, (str, dict, list)):
            raise ValueError("实验摘要格式无效")
        return self.store.put("session", dict(id=uuid.uuid4().hex[:16], deployment_id=uuid.uuid4().hex[:16],
            status="draft", request=copy.deepcopy(request), parallel=None, host_id=None, archived_unix=None,
            remote_root=request.get("remote_root", DEFAULTS["remote_root"]), snapshot=None,
            error=None, events=[], created_unix=time.time()))

    def session(self, identifier):
        with self.lock:
            return self._session_view(self.store.get("session", identifier))

    def _session_view(self, session):
        session.setdefault("host_id", None)
        session.setdefault("archived_unix", None)
        session["archived"] = session["archived_unix"] is not None
        session["can_delete"] = self._can_delete(session)
        snapshot = session.get("snapshot")
        if snapshot:
            observed = session.get("last_observed_unix", snapshot.get("updated_unix", 0))
            snapshot["stale"] = bool(snapshot.get("stale")) or time.time() - float(observed) > 20 or session["status"] == "disconnected"
            if snapshot["stale"]:
                snapshot["eta_seconds"] = None
        return session

    def bootstrap(self, identifier=None, host_id=None, archived=False):
        with self.lock:
            sessions = [s for s in self.store.list("session")
                        if (host_id is None or s.get("host_id") == (host_id or None))
                        and (s.get("archived_unix") is not None) == archived]
            selected = self.session(identifier) if identifier else (self._session_view(sessions[0]) if sessions else None)
            summaries = [dict(id=s["id"], status=s["status"], request=s.get("request"), created_unix=s.get("created_unix"),
                              host_id=s.get("host_id"), archived_unix=s.get("archived_unix"), archived=archived,
                              can_delete=self._can_delete(s)) for s in sessions]
            return dict(hosts=self.store.list("host"), sessions=summaries, defaults=DEFAULTS, session=selected)

    def assign(self, identifier, data):
        with self.lock:
            session = self.store.get("session", identifier)
            if session.get("archived_unix") is not None or session["status"] not in DRAFT_STATES:
                raise ValueError("只能为未归档且尚未部署的工作区选择电脑")
            host = self.store.get("host", data.get("host_id"))
            changes = {"host_id": host["id"]}
            if data.get("remote_root") is not None:
                changes["remote_root"] = windows_path(data["remote_root"])
            if data.get("parallel") is not None:
                changes["parallel"] = parallel_limit(data["parallel"])
            updated = self.store.update("session", identifier, **changes)
            return self._session_view(updated)

    def archive(self, identifier, archived=True):
        with self.lock:
            session = self.store.get("session", identifier)
            if (session.get("archived_unix") is not None) != archived:
                session = self.store.update("session", identifier, archived_unix=time.time() if archived else None)
            return self._session_view(session)

    def _can_delete(self, session):
        if session["id"] in self.working:
            return False
        status = session["status"]
        if status in DRAFT_STATES:
            return True
        snapshot = session.get("snapshot") or {}
        if status in ("complete", "completed"):
            return not snapshot.get("active_workers")
        if status in ("failed", "disconnected") and snapshot.get("status") in FINISHED_STATES:
            return not snapshot.get("active_workers")
        if status in ("failed", "interrupted"):
            # Missing flags in old records are conclusive only before any remote state path existed.
            if session.get("request", {}).get("mode") == "monitor" and session.get("state_file"):
                return False
            return not session.get("launch_attempted", bool(session.get("state_file")))
        return False

    def delete(self, identifier):
        with self.lock:
            session = self.store.get("session", identifier)
            if not self._can_delete(session):
                raise ValueError("工作区仍在运行或远端状态未确认，请先归档，或在任务结束后删除")
            self.store.delete("session", identifier)
            stop = self.observer_stops.get(identifier)
            if stop is not None:
                stop.set()
            process = self.observer_processes.get(identifier)
        if process is not None:
            self._terminate_observer(process)
        return dict(id=identifier, deleted=True)

    def profile_lock(self, host):
        key = tuple(host.get(name) for name in ("address", "user", "port", "identity_file"))
        with self.lock:
            return self.host_locks.setdefault(key, threading.RLock())

    def probe(self, data):
        host = profile(data)
        with self.profile_lock(host):
            return self._probe(host, data)

    def _probe(self, host, data):
        previous = next((x for x in self.store.list("host") if all(x.get(k) == host.get(k) for k in
                         ("address", "user", "port", "identity_file"))), None)
        try:
            observation = self.ssh_factory(host).run(PROBE, [host["runtime_root"], host["slot_root"]])
        except Exception as exc:
            if previous:
                self.store.update("host", previous["id"], status="offline", error=str(exc), checked_unix=time.time())
            raise
        host.update(observation, id=previous["id"] if previous else uuid.uuid4().hex[:16],
                    status="online", checked_unix=time.time(), error=None)
        # A connection check never silently erases a user-assigned display name.
        if previous:
            host["label"] = self.store.get("host", previous["id"])["label"]
        return self.store.put("host", host)

    def rename(self, identifier, label):
        label = str(label).strip()
        if not label or len(label) > 100:
            raise ValueError("名称需要 1–100 个字符")
        return self.store.update("host", identifier, label=label)

    def capacity(self, identifier, capacity, execute=False):
        if isinstance(capacity, bool) or not isinstance(capacity, int) or not 1 <= capacity <= 36:
            raise ValueError("全机并行上限必须在 1–36 之间")
        host = self.store.get("host", identifier)
        with self.profile_lock(host):
            return self._capacity(host, capacity, execute)

    def _capacity(self, host, capacity, execute):
        identifier = host["id"]
        ssh = self.ssh_factory(host)
        directory = self.store.directory / "policy_packages" / identifier
        directory.mkdir(parents=True, exist_ok=True)
        runtime = build_runtime(directory)
        release = ntpath.join(host["runtime_root"], "releases", runtime["release_id"])
        upload_package(ssh, runtime, release, host["runtime_root"])
        code = "import json,sys;sys.path.insert(0,sys.argv[1]);from machine_policy import change_policy;v=change_policy(sys.argv[2],int(sys.argv[3]),sys.argv[4]=='true');print(json.dumps(v,ensure_ascii=False))"
        policy = ssh.run(code, [ntpath.join(release, "tools"), host["slot_root"], str(capacity), "true" if execute else "false"], timeout=100)
        if execute and policy.get("status") in ("applied", "unchanged"):
            host = self.store.update("host", identifier, global_limit=capacity)
        return dict(host=host, policy=policy)

    def browse(self, identifier, path):
        host = self.store.get("host", identifier)
        if path:
            # Browsing a drive root is valid; deployment into it is not.
            if not ntpath.isabs(path) or any(c in path for c in "\x00\r\n"):
                raise ValueError("需要远端 Windows 绝对路径")
        return self.ssh_factory(host).run(BROWSE, [path])

    def deploy(self, identifier, data):
        with self.lock:
            session = self.store.get("session", identifier)
            if session.get("archived_unix") is not None:
                raise ValueError("请先恢复已归档的工作区再部署")
            host_id = data.get("host_id") or session.get("host_id")
            if session.get("host_id") and session["host_id"] != host_id:
                raise ValueError("工作区属于另一台电脑，请先更新工作区的电脑")
            if identifier in self.working or session["status"] in ("deploying", "running", "monitoring", "completed", "complete"):
                return self._session_view(session)
            if session["status"] not in DRAFT_STATES:
                raise ValueError("该会话已有部署记录，请重连或让 Codex 核对，不能直接重复部署")
            request = session["request"]
            if request.get("mode") != "monitor" and not request.get("source_root"):
                raise ValueError("请先由 Codex 准备本次实验任务清单")
            host = self.store.get("host", host_id)
            parallel = parallel_limit(data.get("parallel", session.get("parallel")) or host["global_limit"])
            remote_root = windows_path(data.get("remote_root") or session.get("remote_root") or request.get("remote_root") or DEFAULTS["remote_root"])
            self.store.event(identifier, "deploying", "正在检查目标环境", host_id=host["id"],
                             remote_root=remote_root, parallel=parallel, error=None, launch_attempted=False)
            self.working.add(identifier)
            thread = threading.Thread(target=self._deploy, args=(identifier,), daemon=True)
            thread.start()
            return self.session(identifier)

    def _deploy(self, identifier):
        try:
            session = self.store.get("session", identifier)
            host = self.store.get("host", session["host_id"])
            ssh = self.ssh_factory(host)
            info = ssh.run(PROBE, [host["runtime_root"], host["slot_root"]])
            host = self.store.update("host", host["id"], **info, status="online", checked_unix=time.time())
            directory = self.store.directory / "packages" / identifier
            directory.mkdir(parents=True, exist_ok=True)
            runtime = build_runtime(directory)
            runtime_release = ntpath.join(host["runtime_root"], "releases", runtime["release_id"])
            self.store.event(identifier, "deploying", "正在同步独立运行器版本", runtime_release=runtime_release)
            upload_package(ssh, runtime, runtime_release, host["runtime_root"])
            request = session["request"]
            if request.get("mode") == "monitor":
                execution_root = windows_path(request.get("execution_root") or session["remote_root"])
                manifest = ntpath.join(execution_root, request.get("manifest", "manifest.json"))
                state_file = request.get("state_file") or ntpath.join(execution_root, "control", "campaign_state.json")
                self.store.update("session", identifier, execution_root=execution_root, manifest=manifest,
                                  state_file=state_file, slot_root=host["slot_root"], runtime_release=runtime_release)
                self.store.event(identifier, "running", "已接入现有任务，只读监控")
                self.start_observer(identifier)
                return
            options = dict(remote_root=session["remote_root"], deployment_id=session["deployment_id"],
                           parallel=session["parallel"], global_limit=host["global_limit"],
                           python=host["python"], slot_root=host["slot_root"])
            self.store.event(identifier, "deploying", "正在绑定路径并生成输入 SHA")
            package = prepare_package(request, options, directory, runtime_release)
            self.store.update("session", identifier, package=package, execution_root=package["execution_root"],
                              manifest=package["manifest"], state_file=package["state_file"], slot_root=host["slot_root"],
                              runtime_release=runtime_release, global_limit=host["global_limit"],
                              effective_parallel=package["effective_parallel"])
            self.store.event(identifier, "deploying", "正在上传并核验任务包")
            upload_package(ssh, package, package["execution_root"], session["remote_root"])
            self.store.event(identifier, "deploying", "输入已冻结，正在启动 Windows 控制器和监督窗口", launch_attempted=True)
            try:
                launched = ssh.run(START, [package["execution_root"], package["manifest"], ntpath.join(runtime_release, "tools"),
                                         host["python"], host["principal"], session["deployment_id"]], timeout=175)
            except (RuntimeError, subprocess.TimeoutExpired, OSError) as original:
                try:
                    launched = ssh.run(RECONCILE, [package["execution_root"], package["state_file"], ntpath.join(runtime_release, "tools")])
                except Exception as reconcile_error:
                    raise RuntimeError("启动未确认：%s\n状态核对：%s" % (original, reconcile_error)) from original
            self.store.event(identifier, "running", "部署完成，正在同步运行进度", launch_receipt=launched, error=None)
            self.start_observer(identifier)
        except Exception as exc:
            self.store.event(identifier, "failed", "部署未完成；已保留当前阶段和回执", error=str(exc))
        finally:
            with self.lock:
                self.working.discard(identifier)

    def reconnect(self, identifier):
        with self.lock:
            session = self.store.get("session", identifier)
            if identifier in self.working:
                raise ValueError("工作区正在部署或重连，请稍后再试")
            if not session.get("state_file") or not session.get("runtime_release"):
                raise ValueError("尚未建立远端监控路径，请让 Codex 核对部署记录")
            self.working.add(identifier)
        try:
            if session["status"] in ("failed", "interrupted") and session["request"].get("mode") != "monitor":
                host = self.store.get("host", session["host_id"])
                receipt = self.ssh_factory(host).run(RECONCILE,
                    [session["execution_root"], session["state_file"], ntpath.join(session["runtime_release"], "tools")])
                self.store.update("session", identifier, launch_receipt=receipt)
            self.store.event(identifier, "running", "正在重新连接监控；不会重新启动任务", error=None)
            self.start_observer(identifier)
            return self.session(identifier)
        finally:
            with self.lock:
                self.working.discard(identifier)

    def start_observer(self, identifier):
        with self.lock:
            existing = self.observers.get(identifier)
            if self.closed.is_set() or (existing and existing.is_alive()):
                # The observer loop retries automatically; avoid a competing writer.
                return
            stop = threading.Event()
            thread = threading.Thread(target=self._observe, args=(identifier, stop), daemon=True)
            self.observers[identifier] = thread
            self.observer_stops[identifier] = stop
            thread.start()

    def _observe(self, identifier, stop):
        delay = 2
        while not stop.is_set():
            process = None
            host = None
            try:
                session = self.store.get("session", identifier)
                host = self.store.get("host", session["host_id"])
                ssh = self.ssh_factory(host)
                observer = ntpath.join(session["runtime_release"], "tools", "observer.py")
                code = "import runpy,sys;script=sys.argv[1];sys.argv=sys.argv[1:];sys.path.insert(0,str(__import__('pathlib').Path(script).parent));runpy.run_path(script,run_name='__main__')"
                argv = [observer, "--root", session["execution_root"], "--state", session["state_file"],
                        "--manifest", session["manifest"], "--slot-root", session["slot_root"], "--interval", "5"]
                command = ssh.command(code, argv, fallback=bool(host.get("prefer_fallback")))
                with self.lock:
                    if stop.is_set():
                        break
                    process = subprocess.Popen(command, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                               encoding="utf-8", errors="replace", bufsize=1)
                    self.observer_processes[identifier] = process
                errors = []
                def drain(pipe, tail):
                    for line in pipe:
                        tail.append(line)
                        del tail[:-20]
                threading.Thread(target=drain, args=(process.stderr, errors), daemon=True).start()
                lines = queue.Queue(maxsize=10)
                def read_lines(pipe, output):
                    try:
                        for value in pipe:
                            while not stop.is_set():
                                try:
                                    output.put(value, timeout=1)
                                    break
                                except queue.Full:
                                    pass
                    finally:
                        try:
                            output.put(None, timeout=1)
                        except queue.Full:
                            pass
                threading.Thread(target=read_lines, args=(process.stdout, lines), daemon=True).start()
                received = False
                last_frame = time.monotonic()
                while not stop.is_set():
                    try:
                        line = lines.get(timeout=1)
                    except queue.Empty:
                        if time.monotonic() - last_frame > 35:
                            raise RuntimeError("超过 35 秒未收到远端状态，正在重新连接监控")
                        continue
                    if line is None:
                        break
                    try:
                        snapshot = json.loads(line)
                    except ValueError:
                        continue
                    if not isinstance(snapshot, dict) or "updated_unix" not in snapshot:
                        continue
                    received = True
                    last_frame = time.monotonic()
                    status = snapshot.get("status", "running")
                    state = "completed" if status in ("complete", "completed") else ("failed" if status == "failed" else "running")
                    with self.lock:
                        if stop.is_set():
                            break
                        self.store.update("session", identifier, snapshot=snapshot, status=state,
                                          error=None, last_observed_unix=time.time())
                        self.store.update("host", host["id"], status="online", checked_unix=time.time())
                    delay = 2
                    # Keep machine telemetry alive after the workload finishes;
                    # task completion and the observer's lifetime are separate.
                if stop.is_set():
                    break
                if not received and ssh.can_fallback() and not host.get("prefer_fallback"):
                    self.store.update("host", host["id"], prefer_fallback=True)
                raise RuntimeError("监控连接已中断。" + "".join(errors)[-1000:])
            except Exception as exc:
                with self.lock:
                    if not stop.is_set():
                        self.store.update("session", identifier, status="disconnected", error=str(exc))
                        if host is not None:
                            self.store.update("host", host["id"], status="offline", checked_unix=time.time())
            finally:
                with self.lock:
                    if self.observer_processes.get(identifier) is process:
                        self.observer_processes.pop(identifier, None)
                if process is not None and process.poll() is None:
                    self._terminate_observer(process)
                    try:
                        process.wait(timeout=3)
                    except subprocess.TimeoutExpired:
                        process.kill()
            stop.wait(delay)
            delay = min(30, delay * 2)
        with self.lock:
            self.observers.pop(identifier, None)
            self.observer_stops.pop(identifier, None)

    def logs(self, identifier, task_id, stream, cursor):
        if stream not in ("stdout", "stderr"):
            raise ValueError("日志类型无效")
        session = self.store.get("session", identifier)
        rows = (session.get("snapshot") or {}).get("tasks", [])
        row = next((task for task in rows if task.get("id") == task_id), None)
        if row is None or not row.get(stream):
            return dict(text="", cursor=0, reset=bool(cursor))
        host = self.store.get("host", session["host_id"])
        code = "import json,pathlib,sys;sys.path.insert(0,sys.argv[4]);from observer import read_log;p=pathlib.Path(sys.argv[1]).resolve();p.relative_to(pathlib.Path(sys.argv[2]).resolve());v=read_log(p,int(sys.argv[3]));v['cursor']=v.pop('offset');print(json.dumps(v,ensure_ascii=False))"
        return self.ssh_factory(host).run(code, [row[stream], session["execution_root"], str(max(0, int(cursor))),
                                                ntpath.join(session["runtime_release"], "tools")])

    def close(self):
        self.closed.set()
        with self.lock:
            for stop in self.observer_stops.values():
                stop.set()
            processes = list(self.observer_processes.values())
        for process in processes:
            self._terminate_observer(process)

    @staticmethod
    def _terminate_observer(process):
        try:
            if process.poll() is None:
                process.terminate()
        except OSError:
            pass
