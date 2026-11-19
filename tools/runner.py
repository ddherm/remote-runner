#!/usr/bin/env python3
"""Python 3.9+, stdlib-only, fail-closed task runner. See RUNNER.md."""
import argparse
import concurrent.futures
import ctypes
import hashlib
import json
import math
import os
from pathlib import Path
import platform
import re
import signal
import subprocess
import sys
import threading
import time
import uuid

from native_runtime import (atomic_json, child_environment, install, load_json,
                            process_birth, shared_read, worker_environment)
from slots import DEFAULT_CAPACITY, MAX_CAPACITY, active_slots

install()

def sha(path):
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def encoded(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")


def load(path):
    return load_json(path)


def inside(root, relative):
    if not isinstance(relative, str) or not relative or ":" in relative or relative.startswith(("/", "\\")):
        raise ValueError("Expected nonempty relative path: %r" % relative)
    relative = relative.replace("\\", "/")
    path = (root / relative).resolve()
    try:
        path.relative_to(root.resolve())
    except ValueError:
        raise ValueError("Path escapes configured root: " + relative)
    return path


def verify_inputs(root, inputs):
    checked = []
    for item in inputs:
        path = inside(root, item["path"])
        try:
            actual = sha(path)
        except FileNotFoundError as exc:
            raise ValueError("Input missing: %s (resolved to %s)" % (item["path"], path)) from exc
        if actual != item.get("sha256"):
            raise ValueError("Input SHA mismatch: " + item["path"])
        checked.append({"path": item["path"], "sha256": actual})
    return sorted(checked, key=lambda x: x["path"])


def win_job_for_self():
    """Contain this worker and future descendants; handle is non-inheritable.

    Assignment happens BEFORE workload creation; worker exit/crash closes its
    handle and Windows kills all remaining descendants. Fail if unavailable.
    """
    from ctypes import wintypes as w
    class BASIC(ctypes.Structure):
        _fields_ = [("PerProcessUserTimeLimit", ctypes.c_longlong), ("PerJobUserTimeLimit", ctypes.c_longlong),
                    ("LimitFlags", w.DWORD), ("MinimumWorkingSetSize", ctypes.c_size_t),
                    ("MaximumWorkingSetSize", ctypes.c_size_t), ("ActiveProcessLimit", w.DWORD),
                    ("Affinity", ctypes.c_size_t), ("PriorityClass", w.DWORD), ("SchedulingClass", w.DWORD)]
    class IO(ctypes.Structure):
        _fields_ = [(name, ctypes.c_ulonglong) for name in ("ReadOperationCount", "WriteOperationCount", "OtherOperationCount", "ReadTransferCount", "WriteTransferCount", "OtherTransferCount")]
    class EXTENDED(ctypes.Structure):
        _fields_ = [("BasicLimitInformation", BASIC), ("IoInfo", IO), ("ProcessMemoryLimit", ctypes.c_size_t),
                    ("JobMemoryLimit", ctypes.c_size_t), ("PeakProcessMemoryUsed", ctypes.c_size_t), ("PeakJobMemoryUsed", ctypes.c_size_t)]
    k = ctypes.WinDLL("kernel32", use_last_error=True)
    k.CreateJobObjectW.argtypes = [ctypes.c_void_p, w.LPCWSTR]
    k.CreateJobObjectW.restype = w.HANDLE
    k.SetInformationJobObject.argtypes = [w.HANDLE, ctypes.c_int, ctypes.c_void_p, w.DWORD]
    k.SetInformationJobObject.restype = w.BOOL
    k.GetCurrentProcess.restype = w.HANDLE
    k.AssignProcessToJobObject.argtypes = [w.HANDLE, w.HANDLE]
    k.AssignProcessToJobObject.restype = w.BOOL
    job = k.CreateJobObjectW(None, None)
    if not job:
        raise ctypes.WinError(ctypes.get_last_error())
    limits = EXTENDED()
    limits.BasicLimitInformation.LimitFlags = 0x2000  # KILL_ON_JOB_CLOSE
    if not k.SetInformationJobObject(job, 9, ctypes.byref(limits), ctypes.sizeof(limits)):
        raise ctypes.WinError(ctypes.get_last_error())
    if not k.AssignProcessToJobObject(job, k.GetCurrentProcess()):
        raise ctypes.WinError(ctypes.get_last_error())
    # Intentionally retain until OS closes it at worker exit. Never inherit it.
    return job


def stop_worker(proc):
    if os.name == "nt":
        proc.kill()  # Job Object closes and kills descendants.
    else:
        try:
            os.killpg(proc.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
    proc.wait(timeout=30)


def worker(spec_path):
    spec = load(spec_path)
    attempt = Path(spec["attempt"])
    try:
        if os.name == "nt":
            job_handle = win_job_for_self()
        verify_inputs(Path(spec["root"]), spec["inputs"])
        env = child_environment(spec["env"], attempt)
        with (attempt / "stdout.log").open("wb") as out, (attempt / "stderr.log").open("wb") as err:
            command = [spec["worker_python"], "-X", "utf8", "-B",
                       str(Path(__file__).with_name("workload_entry.py")), str(Path(spec_path).resolve())]
            child = subprocess.Popen(command, cwd=str(attempt), env=env, stdout=out, stderr=err, close_fds=True)
            atomic_json(attempt / "process.json", {"worker_pid": os.getpid(), "worker_birth": process_birth(os.getpid()),
                        "child_pid": child.pid, "child_birth": process_birth(child.pid), "started_unix": time.time()})
            try:
                rc = child.wait(timeout=float(spec["timeout_seconds"]))
            except subprocess.TimeoutExpired:
                child.kill()
                child.wait(timeout=30)
                atomic_json(attempt / "worker_exit.json", {"exit_code": child.returncode, "timeout": True, "finished_unix": time.time()})
                if os.name != "nt":
                    os.killpg(os.getpgrp(), signal.SIGKILL)
                return 1  # Windows closes Job Object on exit, including descendants.
        atomic_json(attempt / "worker_exit.json", {"exit_code": rc, "finished_unix": time.time()})
        return 0 if rc == 0 else 1
    except BaseException as exc:
        atomic_json(attempt / "worker_error.json", {"error": repr(exc), "finished_unix": time.time()})
        return 1


def output_hashes(attempt, names):
    if not names:
        raise ValueError("Every task must declare at least one output")
    return {name: sha(inside(attempt, name)) for name in names}


def task_directory(task, common):
    if task.get("workspace"):
        return inside(Path(common["root"]), task["workspace"]) / "runs" / task["id"]
    return Path(common["results"]) / task["id"]


def verify_json_checks(attempt, checks):
    for check in checks:
        value = load(inside(attempt, check["path"]))
        for key, expected in check["equals"].items():
            actual = value
            for part in key.split("."):
                actual = actual[part]
            if actual != expected:
                raise ValueError("Output identity/status mismatch: " + check["path"] + ":" + key)


def task_identity(task, common, inputs):
    tools = Path(__file__).resolve().parent
    runtime_hashes = {p.name: sha(p) for p in sorted(tools.glob("*.py"))}
    identity = {"schema": 2, "task": task, "inputs": inputs, "env": common["env"],
            "runtime_sha256": runtime_hashes, "worker_environment": common["worker_environment"],
            "python": common["python"]}
    if common.get("execution_quota") is not None:
        identity["execution_quota"] = common["execution_quota"]
    return identity


def task_run(task, common, deadline):
    root, results = Path(common["root"]), Path(common["results"])
    name = task["id"]
    task_dir = task_directory(task, common)
    task_dir.mkdir(parents=True, exist_ok=True)
    inputs = verify_inputs(root, common["inputs"] + task.get("inputs", []))
    identity = task_identity(task, common, inputs)
    fingerprint = hashlib.sha256(encoded(identity)).hexdigest()
    lock = task_dir / "active.lock"
    try:
        lock.mkdir()
    except FileExistsError:
        return {"id": name, "status": "blocked_active_lock", "detail": str(lock)}
    token = uuid.uuid4().hex
    atomic_json(lock / "owner.json", {"token": token, "runner_pid": os.getpid(), "runner_birth": process_birth(os.getpid()),
                                    "hostname": platform.node(), "created_unix": time.time()})
    safe_unlock = True
    try:
        receipt_path = task_dir / "complete.json"
        if receipt_path.exists():
            checked = verify_completed(task, common)
            checked["status"] = "verified_resume"
            return checked
        if any(task_dir.glob("a[0-9][0-9][0-9][0-9]_*")) and not common.get("retry_failed", False):
            raise RuntimeError("Unfinished attempts retained; explicit reviewed retry/resume required")
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            return {"id": name, "status": "budget_exhausted"}
        timeout = min(float(task["timeout_seconds"]), remaining)
        attempt_name = "a%04d_%s" % (1 + len(list(task_dir.glob("a[0-9][0-9][0-9][0-9]_*"))), uuid.uuid4().hex[:6])
        attempt = task_dir / attempt_name
        attempt.mkdir()
        values = {"root": str(root), "attempt": str(attempt), "python": common["python"]}
        argv = [s.format_map(values) for s in task["argv"]]
        spec = {"argv": argv, "root": str(root), "attempt": str(attempt), "env": common["env"],
                "inputs": inputs, "timeout_seconds": timeout, "worker_python": common["python"],
                "worker_environment": common["worker_environment"], "slot_root": common["slot_root"],
                "slot_capacity": common["slot_capacity"],
                "execution_quota": common.get("execution_quota"),
                "task_id": name, "run_id": common.get("run_id"),
                "slot_identity": {"root": str(root), "task_id": name, "attempt": str(attempt),
                                  "phase": task.get("metadata", {}).get("phase", "compute")}}
        atomic_json(attempt / "spec.json", spec)
        atomic_json(attempt / "provenance.json", {"identity": identity, "fingerprint": fingerprint,
                    "metadata": task.get("metadata", {}), "python": common["python"], "runner_python": sys.version,
                    "platform": platform.platform(), "hostname": platform.node(), "timeout_seconds": timeout,
                    "coordinator_executable": sys.executable,
                    "manifest_sha256": common["manifest_sha256"],
                    "lock_token": token, "started_unix": time.time()})
        safe_unlock = False
        started = time.monotonic()
        with (attempt / "supervisor.log").open("wb") as log:
            proc = subprocess.Popen([common["python"], "-X", "utf8", "-B", str(Path(__file__).resolve()), "--worker", str(attempt / "spec.json")],
                                    cwd=str(attempt), stdout=log, stderr=subprocess.STDOUT, close_fds=True,
                                    env=child_environment(common["env"], attempt),
                                    start_new_session=(os.name != "nt"))
            atomic_json(lock / "process.json", {"worker_pid": proc.pid, "worker_birth": process_birth(proc.pid),
                                                "attempt": attempt_name, "token": token})
            timed_out = False
            try:
                proc.wait(timeout=timeout)
            except subprocess.TimeoutExpired:
                timed_out = True
                stop_worker(proc)
            finally:
                if proc.poll() is None:
                    stop_worker(proc)
                elif os.name != "nt":
                    # Remove any descendants that outlived the direct workload.
                    try:
                        os.killpg(proc.pid, signal.SIGKILL)
                    except ProcessLookupError:
                        pass
        safe_unlock = True
        if (attempt / "worker_exit.json").exists():
            timed_out = timed_out or bool(load(attempt / "worker_exit.json").get("timeout"))
        state = {"id": name, "status": "timeout" if timed_out else "failed", "attempt": str(attempt),
                 "worker_returncode": proc.returncode, "wall_seconds": time.monotonic() - started, "finished_unix": time.time()}
        if not timed_out and proc.returncode == 0:
            outputs = output_hashes(attempt, task["outputs"])
            verify_json_checks(attempt, task.get("json_checks", []))
            verify_inputs(root, inputs)  # detect accidental hot edits
            if sha(common["manifest_path"]) != common["manifest_sha256"]:
                raise ValueError("Manifest changed during an active task")
            if task_identity(task, common, inputs) != identity:
                raise ValueError("Runtime changed while a task was computing")
            state["status"] = "complete"
            atomic_json(task_dir / "complete.json", {"schema": 2, "fingerprint": fingerprint, "identity": identity, "attempt": attempt_name,
                        "outputs": outputs, "metadata": task.get("metadata", {}), "finished_unix": time.time()})
        atomic_json(attempt / "runner_result.json", state)
        return state
    except Exception as exc:
        error = {"id": name, "status": "error", "detail": repr(exc), "finished_unix": time.time()}
        if "attempt" in locals():
            atomic_json(attempt / "runner_result.json", error)
        return error
    finally:
        if safe_unlock:
            for item in lock.iterdir():
                item.unlink()
            lock.rmdir()


def normalize_quota(value):
    if not isinstance(value, dict) or set(value) != {"id", "root", "capacity"}:
        raise ValueError("Execution quota requires id, root and capacity")
    name = value["id"]
    if not isinstance(name, str) or not re.fullmatch(r"[A-Za-z0-9_.-]{1,128}", name) or name in (".", ".."):
        raise ValueError("Execution quota id must be a safe deployment id")
    capacity = value["capacity"]
    if isinstance(capacity, bool) or not isinstance(capacity, int) or not 1 <= capacity <= MAX_CAPACITY:
        raise ValueError("Execution quota capacity must be 1..%d" % MAX_CAPACITY)
    if not isinstance(value["root"], str) or not Path(value["root"]).is_absolute():
        raise ValueError("Execution quota root must be an absolute target-machine path")
    return dict(id=name, root=str(Path(value["root"]).resolve()), capacity=capacity)


def inherited_quota(manifest, parent=None):
    if parent is None:
        parent_path = os.environ.get("GTR_RUNTIME_SPEC")
        parent = load(parent_path) if parent_path else {}
    inherited = parent.get("execution_quota")
    if inherited is not None:
        inherited = normalize_quota(inherited)
    explicit = manifest.get("execution_quota")
    if explicit is not None:
        explicit = normalize_quota(explicit)
    if inherited is not None and explicit is not None:
        comparable = lambda quota: (quota["id"], os.path.normcase(quota["root"]), quota["capacity"])
        if comparable(inherited) != comparable(explicit):
            raise ValueError("Child execution quota conflicts with inherited deployment quota")
    return inherited if inherited is not None else explicit


def prepare(args):
    manifest_path = Path(args.manifest).resolve()
    manifest_bytes = shared_read(manifest_path)
    manifest = json.loads(manifest_bytes.decode("utf-8-sig"))
    root = Path(args.root).resolve() if args.root else manifest_path.parent
    results = inside(root, manifest["results"])
    tasks = manifest["tasks"]
    names = [t["id"] for t in tasks]
    if len({n.casefold() for n in names}) != len(names) or any(not re.fullmatch(r"[A-Za-z0-9_.-]+", n) or n in (".", "..") for n in names):
        raise ValueError("Task ids must be unique safe directory names")
    parallel = int(manifest.get("max_parallel", DEFAULT_CAPACITY))
    budget = float(manifest["budget_seconds"])
    if not 1 <= parallel <= MAX_CAPACITY or not math.isfinite(budget) or budget <= 0:
        raise ValueError("Concurrency must be 1..%d and budget must be finite and positive" % MAX_CAPACITY)
    python = Path(args.python or manifest.get("python") or sys.executable).resolve()
    if python.name.lower() == "pythonw.exe":
        python = python.with_name("python.exe")
    parent_path = os.environ.get("GTR_RUNTIME_SPEC")
    parent = load(parent_path) if parent_path else {}
    slot_root = Path(manifest.get("slot_root", parent.get("slot_root", root.parent / "_compute_slots"))).resolve()
    capacity = int(manifest.get("slot_capacity", parent.get("slot_capacity", DEFAULT_CAPACITY)))
    if not 1 <= capacity <= MAX_CAPACITY:
        raise ValueError("Shared capacity must be 1..%d" % MAX_CAPACITY)
    if parent.get("slot_root") is not None and os.path.normcase(str(slot_root)) != os.path.normcase(str(Path(parent["slot_root"]).resolve())):
        raise ValueError("Child machine slot root conflicts with inherited machine policy")
    if parent.get("slot_capacity") is not None and capacity != int(parent["slot_capacity"]):
        raise ValueError("Child machine capacity conflicts with inherited machine policy")
    quota = inherited_quota(manifest, parent)
    if quota is not None and os.path.normcase(quota["root"]) == os.path.normcase(str(slot_root)):
        raise ValueError("Execution quota and machine slots require different directories")
    environment = manifest.get("env", {})
    if any(key.casefold() == "gtr_runtime_spec" for key in environment):
        raise ValueError("Manifest environment cannot override GTR_RUNTIME_SPEC")
    if "max_parallel" not in manifest:
        parallel = min(capacity, quota["capacity"] if quota else capacity)
    elif quota is not None:
        parallel = min(parallel, capacity, quota["capacity"])
    common = {"root": str(root), "results": str(results), "python": str(Path(args.python or sys.executable).resolve()),
              "inputs": manifest.get("inputs", []), "env": environment,
              "slot_root": str(slot_root), "slot_capacity": capacity,
              "execution_quota": quota,
              "manifest_path": str(manifest_path), "manifest_sha256": hashlib.sha256(manifest_bytes).hexdigest(),
              "retry_failed": getattr(args, "retry_failed", False)}
    common["python"] = str(python)
    common["worker_environment"] = worker_environment(python, common["env"])
    directories = []
    for t in tasks:
        if not isinstance(t["argv"], list) or not t["argv"] or not t.get("outputs") or not math.isfinite(float(t["timeout_seconds"])) or float(t["timeout_seconds"]) <= 0:
            raise ValueError("Task requires argv, outputs and positive timeout")
        reserved = {"spec.json", "provenance.json", "process.json", "resource.json", "worker_exit.json", "worker_error.json", "runner_result.json", "stdout.log", "stderr.log", "supervisor.log"}
        if any(name.replace("\\", "/").casefold() in reserved for name in t["outputs"]):
            raise ValueError("Task output collides with runner metadata")
        directory = task_directory(t, common).resolve()
        for previous in directories:
            left, right = str(previous).casefold(), str(directory).casefold()
            if left == right or left.startswith(right + os.sep) or right.startswith(left + os.sep):
                raise ValueError("Writable task directories overlap")
        directories.append(directory)
        for name in t["outputs"]:
            inside(directory, name)
        checked_inputs = verify_inputs(root, common["inputs"] + t.get("inputs", []))
        command = [value.format_map(dict(root=str(root), attempt=str(directory / "planned-attempt"),
                                        python=common["python"])) for value in t["argv"]]
        if Path(command[0]).resolve() != python:
            raise ValueError("Task must use the configured worker Python")
        script_args = command[1:]
        while script_args and script_args[0] in ("-B", "-u", "-X"):
            option = script_args.pop(0)
            if option == "-X" and (not script_args or script_args.pop(0) != "utf8"):
                raise ValueError("Only -X utf8 is supported")
        if not script_args or script_args[0].startswith("-"):
            raise ValueError("Workload must be a declared Python script")
        source_scripts = {inside(root, row["path"]) for row in checked_inputs}
        if Path(script_args[0]).resolve() not in source_scripts:
            raise ValueError("Worker entry script must be declared in input SHA list")
        for row in common["inputs"] + t.get("inputs", []):
            source = inside(root, row["path"])
            if source == directory or directory in source.parents:
                raise ValueError("Writable task directory overlaps an input")
        for check in t.get("json_checks", []):
            if check["path"] not in t["outputs"]:
                raise ValueError("JSON checks must refer to a declared hashed output")
    return manifest_path, manifest, common, tasks, parallel, budget


def verify_completed(task, common):
    directory = task_directory(task, common)
    inputs = verify_inputs(Path(common["root"]), common["inputs"] + task.get("inputs", []))
    identity = task_identity(task, common, inputs)
    receipt = load(directory / "complete.json")
    expected = hashlib.sha256(encoded(identity)).hexdigest()
    if receipt.get("fingerprint") != expected or receipt.get("identity") != identity:
        raise ValueError("Completed task identity mismatch: " + task["id"])
    attempt = inside(directory, receipt["attempt"])
    provenance = load(attempt / "provenance.json")
    if provenance["fingerprint"] != expected or provenance["identity"] != identity:
        raise ValueError("Attempt provenance mismatch: " + task["id"])
    exit_record = load(attempt / "worker_exit.json")
    if exit_record.get("exit_code") != 0 or exit_record.get("timeout"):
        raise ValueError("Completed task has unsuccessful process exit")
    if output_hashes(attempt, task["outputs"]) != receipt["outputs"]:
        raise ValueError("Completed output hash mismatch: " + task["id"])
    verify_json_checks(attempt, task.get("json_checks", []))
    return {"id": task["id"], "status": "verified", "attempt": str(attempt)}


def recover_locks(common, tasks):
    """Archive locks only after recorded controller and descendants have exited."""
    locks = [Path(common["results"]) / "controller.active.lock"]
    locks += [task_directory(task, common) / "active.lock" for task in tasks]
    reviewed = []
    for lock in locks:
        if not lock.exists():
            continue
        owner_path = lock / "owner.json"
        before = sha(owner_path)
        owner = load(owner_path)
        host = owner.get("hostname", owner.get("host"))
        if host != platform.node():
            raise RuntimeError("Lock belongs to a different host: " + str(lock))
        records = [owner]
        process = lock / "process.json"
        if process.exists():
            records.append(load(process))
        if lock.name == "active.lock":
            attempts = list(lock.parent.glob("a[0-9][0-9][0-9][0-9]_*"))
            if attempts and not process.exists():
                raise RuntimeError("Lock has an unrecorded worker; inspect its process ownership: " + str(lock))
            for attempt in attempts:
                if (attempt / "process.json").exists():
                    records.append(load(attempt / "process.json"))
        for record in records:
            for pid_key, birth_key in (("pid", "birth"), ("runner_pid", "runner_birth"),
                                       ("worker_pid", "worker_birth"), ("child_pid", "child_birth")):
                if pid_key not in record:
                    continue
                pid = int(record[pid_key])
                if pid <= 0:
                    raise ValueError("Invalid process owner")
                birth = process_birth(pid)
                if birth is not None and (record.get(birth_key) is None or birth == record[birth_key]):
                    raise RuntimeError("Recorded process is still alive: %d" % pid)
        reviewed.append((lock, before))
    archived = []
    for lock, before in reviewed:
        if sha(lock / "owner.json") != before:
            raise RuntimeError("Lock ownership changed during review")
        destination = lock.with_name(lock.name + ".abandoned-" + uuid.uuid4().hex[:8])
        atomic_json(lock / "recovery_review.json", dict(owner_sha256=before,
                    reviewed_unix=time.time(), reviewer_pid=os.getpid(), host=platform.node(), archive_path=str(destination)))
        lock.rename(destination)
        archived.append(str(destination))
    return archived


def publish_state(path, common, tasks, outcomes, started, run_id, status="running"):
    rows = []
    for task in tasks:
        directory = task_directory(task, common)
        row = dict(id=task["id"], metadata=task.get("metadata", {}), status="pending",
                   progress_file=task.get("progress_file", "progress.json"))
        attempts = sorted(directory.glob("a[0-9][0-9][0-9][0-9]_*"))
        if attempts:
            attempt = attempts[-1]
            row["attempt"] = str(attempt)
            row["status"] = "running" if (directory / "active.lock").exists() else "unfinished"
            progress = inside(attempt, task.get("progress_file", "progress.json"))
            for name, target in (("progress", progress), ("resource", attempt / "resource.json")):
                try:
                    row[name] = load(target)
                except (OSError, ValueError):
                    pass
        outcome = outcomes.get(task["id"])
        if outcome is not None:
            row.update(outcome)
        rows.append(row)
    leases = active_slots(common["slot_root"])
    quota = common.get("execution_quota")
    atomic_json(path, dict(schema=2, status=status, run_id=run_id, controller_pid=os.getpid(),
                controller_birth=process_birth(os.getpid()), updated_unix=time.time(),
                elapsed_seconds=time.monotonic() - started, hard_concurrency_limit=common["slot_capacity"],
                slot_root=common["slot_root"], global_compute_slots=len(leases),
                execution_quota=quota, deployment_id=quota["id"] if quota else None,
                batch_compute_slots=sum(r.get("deployment_id") == quota["id"] for r in leases) if quota else None,
                compute_slots=sum(r.get("root") == common["root"] for r in leases), tasks=rows,
                current_errors=[o for o in outcomes.values() if o["status"] not in ("complete", "verified_resume")]))


def run(args):
    manifest_path, manifest, common, tasks, parallel, budget = prepare(args)
    results = Path(common["results"])
    plan = {"tasks": len(tasks), "task_ids": [t["id"] for t in tasks], "max_parallel": parallel, "budget_seconds": budget,
            "results": str(results), "python": common["python"], "execute": args.execute,
            "execution_quota": common.get("execution_quota"), "slot_capacity": common["slot_capacity"],
            "maximum_supported_capacity": MAX_CAPACITY}
    print(json.dumps(plan, ensure_ascii=False), flush=True)
    if args.recover_locks:
        print(json.dumps({"archived_locks": recover_locks(common, tasks)}, ensure_ascii=False), flush=True)
    if args.verify:
        report = [verify_completed(t, common) for t in tasks]
        print(json.dumps({"status": "verified", "tasks": report}, ensure_ascii=False), flush=True)
        return 0
    if not args.execute:
        return 0
    results.mkdir(parents=True, exist_ok=True)
    controller_lock = results / "controller.active.lock"
    try:
        controller_lock.mkdir()
    except FileExistsError:
        raise RuntimeError("Controller lock exists; inspect recorded process ownership before recovery")
    atomic_json(controller_lock / "owner.json", dict(pid=os.getpid(), birth=process_birth(os.getpid()),
                host=platform.node(), manifest=str(manifest_path), started_unix=time.time()))
    run_id = "run-" + uuid.uuid4().hex
    common["run_id"] = run_id
    atomic_json(results / (run_id + "-manifest.json"), manifest)
    deadline = time.monotonic() + budget
    outcomes = {}
    started = time.monotonic()
    done = threading.Event()
    guard = threading.Lock()
    heartbeat_errors = []
    state_path = results / "campaign_state.json"

    def heartbeat():
        while not done.is_set():
            try:
                with guard:
                    publish_state(state_path, common, tasks, outcomes, started, run_id)
            except Exception as error:
                heartbeat_errors.append(repr(error))
            done.wait(1)

    pump = threading.Thread(target=heartbeat, daemon=True)
    pump.start()
    try:
        with concurrent.futures.ThreadPoolExecutor(max_workers=parallel) as pool:
            futures = [pool.submit(task_run, t, common, deadline) for t in tasks]
            for future in concurrent.futures.as_completed(futures):
                row = future.result()
                with guard:
                    outcomes[row["id"]] = row
                print(json.dumps(row, ensure_ascii=False), flush=True)
        status = "complete" if all(o["status"] in ("complete", "verified_resume") for o in outcomes.values()) else "failed"
        done.set()
        pump.join(timeout=10)
        publish_state(state_path, common, tasks, outcomes, started, run_id, status)
        atomic_json(results / (run_id + "-summary.json"), {"manifest_sha256": common["manifest_sha256"],
                    "plan": plan, "outcomes": list(outcomes.values()), "heartbeat_errors": heartbeat_errors})
        return 0 if status == "complete" else 1
    finally:
        done.set()
        pump.join(timeout=10)
        for item in controller_lock.iterdir():
            item.unlink()
        controller_lock.rmdir()


def main():
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8", errors="backslashreplace")
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--manifest")
    parser.add_argument("--root")
    parser.add_argument("--python")
    parser.add_argument("--execute", action="store_true", help="Without this option only validate and print plan")
    parser.add_argument("--verify", action="store_true", help="Verify all completed task receipts without computing")
    parser.add_argument("--retry-failed", action="store_true", help="Permit a reviewed retry with a fresh attempt")
    parser.add_argument("--recover-locks", action="store_true", help="Review process ownership and archive exited-owner locks")
    parser.add_argument("--worker", help=argparse.SUPPRESS)
    args = parser.parse_args()
    if args.worker:
        return worker(args.worker)
    if not args.manifest:
        parser.error("--manifest is required")
    try:
        return run(args)
    except Exception as exc:
        print(json.dumps({"status": "preflight_error", "error": repr(exc)}, ensure_ascii=False), file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
