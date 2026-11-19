"""Passive batch console using delete-sharing readers and current-run events."""
import argparse
import ctypes
import csv
import io
import json
import os
from pathlib import Path
import re
import subprocess
import time

from native_runtime import atomic_json, load_json, process_birth
from slots import active_slots


def metrics(previous=None):
    result = dict(cpu_percent=None, available_gib=None, memory_percent=None,
                  memory_used_gib=None, memory_total_gib=None)
    if os.name != "nt":
        return result, previous
    from ctypes import wintypes as w

    class Memory(ctypes.Structure):
        _fields_ = [("length", w.DWORD), ("load", w.DWORD)] + [
            (name, ctypes.c_ulonglong) for name in
            ("total", "available", "page_total", "page_available", "virtual_total", "virtual_available", "extended")]

    memory = Memory()
    memory.length = ctypes.sizeof(memory)
    kernel = ctypes.windll.kernel32
    if kernel.GlobalMemoryStatusEx(ctypes.byref(memory)):
        result["available_gib"] = memory.available / 2 ** 30
        result["memory_total_gib"] = memory.total / 2 ** 30
        result["memory_used_gib"] = (memory.total - memory.available) / 2 ** 30
        result["memory_percent"] = memory.load
    idle, system, user = w.FILETIME(), w.FILETIME(), w.FILETIME()
    if kernel.GetSystemTimes(ctypes.byref(idle), ctypes.byref(system), ctypes.byref(user)):
        number = lambda value: (value.dwHighDateTime << 32) | value.dwLowDateTime
        current = number(idle), number(system) + number(user)
        if previous and current[1] > previous[1]:
            result["cpu_percent"] = max(0, min(100, 100 * (1 - (current[0] - previous[0]) / (current[1] - previous[1]))))
        previous = current
    return result, previous


def gpu_metrics():
    """NVIDIA first, then Windows formatted engine counters; never fake zero."""
    flags = getattr(subprocess, "CREATE_NO_WINDOW", 0) if os.name == "nt" else 0
    try:
        result = subprocess.run(["nvidia-smi", "--query-gpu=index,name,utilization.gpu,memory.used,memory.total",
                                 "--format=csv,noheader,nounits"], capture_output=True, text=True,
                                timeout=2, creationflags=flags, check=True)
        rows = []
        for row in csv.reader(io.StringIO(result.stdout)):
            if len(row) != 5:
                continue
            def value(item):
                try:
                    return float(item.strip())
                except ValueError:
                    return None
            index, name, utilization, used, total = row
            rows.append(dict(id=index.strip(), name=name.strip(), utilization_percent=value(utilization),
                             memory_used_mib=value(used), memory_total_mib=value(total), source="nvidia-smi"))
        if rows:
            return rows
    except (OSError, subprocess.SubprocessError, UnicodeError):
        pass
    if os.name != "nt":
        return []
    try:
        script = ("$ErrorActionPreference='Stop'; [Console]::OutputEncoding=[Text.UTF8Encoding]::new(); "
                  "@(Get-CimInstance Win32_PerfFormattedData_GPUPerformanceCounters_GPUEngine | "
                  "Select-Object Name,UtilizationPercentage) | ConvertTo-Json -Compress")
        result = subprocess.run(["powershell.exe", "-NoProfile", "-NonInteractive", "-Command", script],
                                capture_output=True, encoding="utf-8-sig", timeout=2,
                                creationflags=flags, check=True)
        counters = json.loads(result.stdout)
        if isinstance(counters, dict):
            counters = [counters]
        adapters = {}
        for counter in counters or []:
            match = re.search(r"(luid_.*?_phys_\d+).*?engtype_(.+)", counter.get("Name", ""))
            if not match:
                continue
            adapter, engine = match.groups()
            engines = adapters.setdefault(adapter, {})
            engines[engine] = engines.get(engine, 0) + float(counter.get("UtilizationPercentage", 0))
        return [dict(id=key, name="GPU %d" % index, utilization_percent=min(100, max(values.values())),
                     memory_used_mib=None, memory_total_mib=None, source="windows-counters")
                for index, (key, values) in enumerate(sorted(adapters.items()))]
    except (OSError, subprocess.SubprocessError, ValueError, TypeError, AttributeError):
        return []


class ResourceSampler:
    def __init__(self):
        self.previous = None
        self.gpus = []
        self.gpu_next = 0

    def sample(self):
        resource, self.previous = metrics(self.previous)
        now = time.monotonic()
        if now >= self.gpu_next:
            self.gpus = gpu_metrics()
            self.gpu_next = now + (5 if self.gpus else 60)
        resource.update(gpus=self.gpus, gpu_status="available" if self.gpus else "unavailable")
        return resource


def collect_snapshot(state, root, sampler=None, progress_cache=None):
    """The console and JSON use the same fresh, delete-sharing progress reads."""
    root = Path(root).resolve()
    state = dict(state)
    rows = []
    for original in state.get("tasks", []):
        item = dict(original)
        attempt = item.get("attempt")
        if attempt:
            path = Path(attempt).resolve()
            if root in path.parents:
                progress_path = (path / item.get("progress_file", "progress.json")).resolve()
                if path in progress_path.parents:
                    try:
                        item["progress"] = load_json(progress_path)
                    except (OSError, ValueError):
                        if progress_cache is not None and attempt in progress_cache:
                            item["progress"] = progress_cache[attempt]
        if progress_cache is not None and attempt and isinstance(item.get("progress"), dict):
            progress_cache[attempt] = item["progress"]
        rows.append(item)
    state["tasks"] = rows
    if state.get("slot_root"):
        leases = active_slots(state["slot_root"])
        state["global_compute_slots"] = len(leases)
        state["compute_slots"] = sum(row.get("root") == str(root) for row in leases)
    return dict(state=state, resources=(sampler or ResourceSampler()).sample())


def format_view(state, root, resource, now=None, progress_cache=None):
    now = time.time() if now is None else now
    status = state.get("status", "waiting")
    age = now - state.get("updated_unix", now)
    title = root.name + " | " + status
    if status == "running" and (age > 30 or process_birth(state.get("controller_pid", 0)) != state.get("controller_birth")):
        title += " | 状态需核查"
    rows = [title, "计算槽位 %s/%s | 全机槽位 %s/%s | 状态 %d 秒前" % (
        state.get("compute_slots", 0), state.get("hard_concurrency_limit", 18),
        state.get("global_compute_slots", 0), state.get("hard_concurrency_limit", 18), max(0, age)),
        "CPU %s | 可用RAM %s GiB" % (resource.get("cpu_percent"), resource.get("available_gib"))]
    for item in state.get("tasks", []):
        metadata = item.get("metadata", {})
        progress = dict(item.get("progress", {}))
        attempt = item.get("attempt")
        if progress_cache is not None and attempt in progress_cache:
            progress = dict(progress_cache[attempt], **progress)
        if attempt:
            path = Path(attempt).resolve()
            if root.resolve() in path.parents:
                try:
                    progress = load_json(path / item.get("progress_file", "progress.json"))
                except (OSError, ValueError):
                    pass
        if progress_cache is not None and attempt:
            progress_cache[attempt] = progress
        rows.append("%s | %s | %s" % (item["id"], item.get("status"), progress.get("phase", metadata.get("phase", ""))))
        counters = []
        for label, keys, total in (
            ("回合", ("completed_episodes", "episodes_completed"), "episodes_total"),
            ("更新", ("optimizer_steps", "optimizer_updates"), "updates_total"),
            ("验证", ("validation_done", "validation_completed", "completed_validation_cases"), "validation_total"),
            ("测试", ("tests_completed",), "tests_total")):
            value = next((progress[key] for key in keys if key in progress), None)
            if value is not None:
                counters.append("%s %s/%s" % (label, value, metadata.get(total, progress.get(total, "?"))))
        if counters:
            rows.append("  " + " | ".join(counters))
        if attempt:
            rows.append("  attempt " + attempt)
        if item.get("detail"):
            rows.append("  error " + item["detail"])
    rows.append("更新时间 " + time.strftime("%Y-%m-%d %H:%M:%S"))
    return "\n".join(rows)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--root", required=True)
    parser.add_argument("--state", required=True)
    parser.add_argument("--interval", type=float, default=5)
    parser.add_argument("--once", action="store_true")
    parser.add_argument("--json", action="store_true")
    parser.add_argument("--instance", help="Dedicated monitor-owned startup record")
    args = parser.parse_args()
    root = Path(args.root).resolve()
    if os.name == "nt":
        ctypes.windll.kernel32.SetConsoleTitleW("GTR " + root.name + " 实验进度")
        kernel = ctypes.windll.kernel32
        from ctypes import wintypes as w
        kernel.GetStdHandle.argtypes = [w.DWORD]
        kernel.GetStdHandle.restype = w.HANDLE
        kernel.GetConsoleMode.argtypes = [w.HANDLE, ctypes.POINTER(w.DWORD)]
        kernel.SetConsoleMode.argtypes = [w.HANDLE, w.DWORD]
        handle = kernel.GetStdHandle(w.DWORD(-11))
        mode = w.DWORD()
        if kernel.GetConsoleMode(handle, ctypes.byref(mode)):
            kernel.SetConsoleMode(handle, mode.value | 4)
    if args.instance:
        proof = dict(pid=os.getpid(), process_birth=process_birth(os.getpid()), root=str(root), started_unix=time.time())
        if os.name == "nt":
            from ctypes import wintypes as w
            kernel = ctypes.windll.kernel32
            kernel.GetConsoleWindow.restype = w.HWND
            handle = kernel.GetConsoleWindow()
            user = ctypes.windll.user32
            user.IsWindowVisible.argtypes = [w.HWND]
            session = w.DWORD()
            kernel.ProcessIdToSessionId(os.getpid(), ctypes.byref(session))
            proof.update(console_handle=int(handle or 0), visible=bool(user.IsWindowVisible(handle)), session_id=session.value)
        instance = Path(args.instance)
        instance.parent.mkdir(parents=True, exist_ok=True)
        atomic_json(instance, proof)
    state, progress_cache, sampler = {}, {}, ResourceSampler()
    while True:
        try:
            state = load_json(args.state)
        except (OSError, ValueError):
            pass
        snapshot = collect_snapshot(state, root, sampler, progress_cache)
        state, resource = snapshot["state"], snapshot["resources"]
        if args.json:
            print(json.dumps(snapshot, ensure_ascii=False), flush=True)
        else:
            print(("" if args.once else "\x1b[2J\x1b[H") + format_view(state, root, resource, progress_cache=progress_cache), flush=True)
        if args.once:
            return
        time.sleep(max(1, args.interval))


if __name__ == "__main__":
    main()
