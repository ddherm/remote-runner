"""Read-only Windows observer shared by the initiator and legacy console."""
import argparse
import codecs
import contextlib
import ctypes
import json
import os
from pathlib import Path
import re
import sys
import time

from native_runtime import load_json, process_birth
from monitor import ResourceSampler, collect_snapshot
from slots import active_slots

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from initiator.progress import normalize_metrics, normalize_phases, overall_progress, ProgressEstimator


def optional(path, default=None):
    try:
        return load_json(path)
    except (OSError, ValueError, TypeError):
        return default


def within(root, path):
    root, path = Path(root).resolve(), Path(path).resolve()
    return path == root or root in path.parents


@contextlib.contextmanager
def shared_stream(path):
    """Seekable reader; Windows writers remain able to atomically replace files."""
    path = Path(path)
    if os.name != "nt":
        with path.open("rb") as stream:
            yield stream
        return
    import msvcrt
    from ctypes import wintypes as w
    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel.CreateFileW.argtypes = [w.LPCWSTR, w.DWORD, w.DWORD, ctypes.c_void_p,
                                   w.DWORD, w.DWORD, w.HANDLE]
    kernel.CreateFileW.restype = w.HANDLE
    kernel.CloseHandle.argtypes = [w.HANDLE]
    handle = kernel.CreateFileW(str(path.resolve()), 0x80000000, 1 | 2 | 4, None, 3, 0x80, None)
    if handle == ctypes.c_void_p(-1).value:
        raise ctypes.WinError(ctypes.get_last_error())
    try:
        descriptor = msvcrt.open_osfhandle(handle, os.O_RDONLY | os.O_BINARY)
    except BaseException:
        kernel.CloseHandle(handle)
        raise
    with os.fdopen(descriptor, "rb") as stream:
        yield stream


def read_log(path, offset=0, max_bytes=65536):
    """Byte cursor with UTF-8 boundary preservation and truncation detection."""
    offset = max(0, int(offset))
    max_bytes = min(1024 * 1024, max(4, int(max_bytes)))
    try:
        with shared_stream(path) as stream:
            size = os.fstat(stream.fileno()).st_size
            reset = offset > size
            start = 0 if reset else offset
            stream.seek(start)
            chunk = stream.read(max_bytes)
        decoder = codecs.getincrementaldecoder("utf-8")("replace")
        content = decoder.decode(chunk, final=False)
        trailing = len(decoder.getstate()[0])
        return dict(text=content, offset=start + len(chunk) - trailing, size=size,
                    reset=reset, has_more=start + len(chunk) < size)
    except FileNotFoundError:
        return dict(text="", offset=offset, size=0, reset=False, has_more=False, missing=True)


def browse_directories(path=None):
    """Enumerate target-machine folders, never the initiator's file picker."""
    if not path and os.name == "nt":
        mask = ctypes.windll.kernel32.GetLogicalDrives()
        roots = [chr(65 + index) + ":\\" for index in range(26) if mask & (1 << index)]
        return dict(path=None, parent=None, directories=[dict(name=p, path=p) for p in roots])
    directory = Path(path or os.path.abspath(os.sep)).resolve()
    rows = []
    with os.scandir(directory) as entries:
        for entry in entries:
            try:
                if entry.is_dir(follow_symlinks=False):
                    rows.append(dict(name=entry.name, path=str(directory / entry.name)))
            except OSError:
                continue
    return dict(path=str(directory), parent=None if directory.parent == directory else str(directory.parent),
                directories=sorted(rows, key=lambda row: row["name"].casefold()))


class Observer:
    def __init__(self, root, state=None, manifest=None, slot_root=None, sampler=None):
        self.root = Path(root).resolve()
        self.state_path = (self.root / (state or "control/campaign_state.json")).resolve()
        self.manifest_path = (self.root / (manifest or "manifest.json")).resolve()
        self.slot_root = (self.root / slot_root).resolve() if slot_root else None
        self.sampler = sampler or ResourceSampler()
        self.progress_cache = {}
        self.estimator = ProgressEstimator()
        self.last_state = {}

    def _read_progress(self, path, fallback):
        value = optional(path)
        if isinstance(value, dict):
            self.progress_cache[str(path)] = value
            return value
        return self.progress_cache.get(str(path), fallback or {})

    def _child_manifests(self, attempt, progress_path):
        candidates = []
        for relation_file in sorted((attempt / "children").glob("*.json")):
            relation = optional(relation_file, {})
            child = relation.get("child", {})
            if child.get("manifest"):
                child_root = Path(child.get("root") or self.root)
                manifest = Path(child["manifest"])
                if not manifest.is_absolute():
                    manifest = child_root / manifest
                candidates.append(manifest)
        # Frozen releases have no relation registration. Their manifests are
        # already fixed under the progress directory (no broad result walk).
        folder = progress_path.parent
        candidates.extend(folder.glob("*/manifest.json"))
        candidates.extend(folder.glob("v/*/manifest.json"))
        return [path.resolve() for path in candidates if within(self.root, path)]

    def _manifest_tasks(self, manifest_path, rows, parent_id=None, state=None, depth=0, seen=None):
        if depth > 6:
            return
        seen = seen if seen is not None else set()
        manifest_path = Path(manifest_path).resolve()
        if str(manifest_path) in seen or not within(self.root, manifest_path):
            return
        seen.add(str(manifest_path))
        manifest = optional(manifest_path, {})
        if state is None:
            child_state = self.root / manifest.get("results", "control") / "campaign_state.json"
            state = optional(child_state, {}) if within(self.root, child_state) else {}
        live = {row.get("id"): row for row in (state or {}).get("tasks", []) if isinstance(row, dict)}
        tasks = manifest.get("tasks", [])
        if not tasks and live:
            tasks = list(live.values())
        work_plan = manifest.get("work_plan", {}) if parent_id is None else {}
        declared_tasks = work_plan.get("tasks", {}) if isinstance(work_plan, dict) else {}
        for task in tasks:
            if not isinstance(task, dict) or not task.get("id"):
                continue
            original_id = str(task["id"])
            task_id = original_id if parent_id is None else parent_id + "/" + original_id
            item = dict(task, **live.get(original_id, {}))
            metadata = dict(item.get("metadata", {}))
            plan = declared_tasks.get(original_id, {})
            if not plan and parent_id is None and len(tasks) == 1 and isinstance(work_plan, dict):
                plan = work_plan
            if isinstance(plan, dict):
                for key, target in (("training", "episodes_total"), ("validation", "validation_total"),
                                    ("testing", "tests_total")):
                    if key in plan:
                        metadata[target] = plan[key]
                if isinstance(plan.get("metrics"), list):
                    metadata["metrics"] = plan["metrics"]
            if task.get("workspace"):
                directory = self.root / task["workspace"] / "runs" / original_id
            else:
                directory = self.root / manifest.get("results", "control") / original_id
            if not within(self.root, directory):
                continue
            receipt = optional(directory / "complete.json", {})
            attempt = Path(item["attempt"]) if item.get("attempt") else None
            if attempt is None and receipt.get("attempt"):
                attempt = directory / receipt["attempt"]
            if attempt is None:
                attempts = sorted(directory.glob("a[0-9][0-9][0-9][0-9]_*"))
                attempt = attempts[-1] if attempts else None
            if attempt is not None and not within(self.root, attempt):
                attempt = None
            raw = item.get("progress", {})
            progress_path = None
            if attempt is not None:
                progress_path = (attempt / item.get("progress_file", "progress.json")).resolve()
                if not within(attempt, progress_path):
                    progress_path = attempt / "progress.json"
                raw = self._read_progress(progress_path, raw)
            status = item.get("status", "pending")
            if receipt:
                status = "complete"
            elif attempt is not None:
                outcome = optional(attempt / "runner_result.json", {})
                resource = optional(attempt / "resource.json", {})
                status = outcome.get("status", raw.get("status", status))
                if status in ("pending", "unfinished") and (directory / "active.lock").exists():
                    status = "running"
                if status == "running" and resource.get("status") in (
                        "waiting_slot", "waiting_batch_slot", "waiting_machine_slot"):
                    status = resource["status"]
            phase = str(raw.get("phase", metadata.get("phase", "")))
            row = dict(id=task_id, native_task_id=original_id, label=item.get("label", original_id),
                       parent_id=parent_id, status=status, phase=phase.splitlines()[0] if phase else "",
                       metrics=normalize_metrics(raw, metadata), phases=normalize_phases(raw, metadata),
                       attempt=str(attempt) if attempt else None,
                       stdout=str(attempt / "stdout.log") if attempt else None,
                       stderr=str(attempt / "stderr.log") if attempt else None,
                       metadata=metadata, updated_unix=raw.get("updated_unix"),
                       error=raw.get("error", item.get("detail")),
                       _raw=raw, _progress_path=progress_path, _verified_receipt=bool(receipt))
            rows.append(row)
            if attempt is not None:
                for child in self._child_manifests(attempt, progress_path):
                    self._manifest_tasks(child, rows, task_id, depth=depth + 1, seen=seen)

    @staticmethod
    def _reconcile(rows):
        children = {}
        for row in rows:
            children.setdefault(row.get("parent_id"), []).append(row)
        for row in reversed(rows):
            raw = row["_raw"]
            descendants = children.get(row["id"], [])
            # Live validation batches are present in a fixed manifest before
            # the old parent publishes its next validation_done count.
            if row["phase"] == "validation" and raw.get("validation_episode") is not None:
                episode = int(raw["validation_episode"])
                current_batch = [child for child in descendants
                                 if re.search(r"[/\\]e0*%d[/\\]" % episode,
                                              str(child.get("attempt") or "") + os.sep)]
                done = sum(child["status"] in ("complete", "verified_resume") for child in current_batch)
                for metric in row["metrics"]:
                    if metric["id"] == "validation":
                        prior = max(0, metric["current"] - int(raw.get("validation_environment_done", 0)))
                        metric["current"] = max(metric["current"], prior + done)
            # Cases do not expose an internal fraction; their completion is
            # one honest plan item, while running cases remain indeterminate.
            if not row["metrics"] and row["phase"] in ("validation", "testing"):
                row["metrics"] = [dict(id=row["phase"], label="验证" if row["phase"] == "validation" else "测试",
                                       current=int(bool(row.get("_verified_receipt"))), total=1,
                                       unit="场", contributes_to_overall=True,
                                       indeterminate=not row.get("_verified_receipt") and row["status"] not in ("failed", "error"))]
            if (not descendants and row.get("_verified_receipt")
                    and row["phase"] in ("validation", "testing")):
                for metric in row["metrics"]:
                    if (metric["id"] == row["phase"] and metric.get("total") == 1
                            and metric.get("current") in (None, 0)):
                        metric["current"] = 1
                        metric["indeterminate"] = False
            own = {metric["id"]: metric for metric in row["metrics"]}
            sums = {}
            for child in descendants:
                for metric in child["metrics"]:
                    if metric.get("current") is not None:
                        sums[metric["id"]] = sums.get(metric["id"], 0) + metric["current"]
            for key, value in sums.items():
                if key in own:
                    own[key]["current"] = max(own[key].get("current") or 0, value)

    def snapshot(self):
        now = time.time()
        state = optional(self.state_path)
        if isinstance(state, dict):
            self.last_state = state
        else:
            state = self.last_state
        source_available = bool(state)
        snapshot = collect_snapshot(state, self.root, self.sampler)
        state = snapshot["state"]
        manifest = optional(self.manifest_path, {})
        rows = []
        self._manifest_tasks(self.manifest_path, rows, state=state)
        self._reconcile(rows)
        slot_root = self.slot_root or (Path(state["slot_root"]) if state.get("slot_root") else None)
        if slot_root is None and manifest.get("slot_root"):
            slot_root = Path(manifest["slot_root"])
        leases = active_slots(slot_root) if slot_root else []
        policy = optional(slot_root / "capacity.json", {}) if slot_root else {}
        limit = policy.get("capacity", state.get("hard_concurrency_limit", manifest.get("slot_capacity", 18)))
        by_attempt = {os.path.normcase(row["attempt"]): row for row in rows if row.get("attempt")}
        workers = []
        machine_workers = []
        active_metrics = {}
        by_id = {row["id"]: row for row in rows}
        for lease in leases:
            row = by_attempt.get(os.path.normcase(lease.get("attempt", "")))
            worker = dict(lease)
            worker["task_id"] = row["id"] if row else lease.get("task_id")
            worker["label"] = row["label"] if row else lease.get("task_id", "其他批次任务")
            worker["phase"] = row["phase"] if row else lease.get("phase", "compute")
            machine_workers.append(worker)
            lease_root = os.path.normcase(str(Path(lease["root"]).resolve())) if lease.get("root") else ""
            if row is None and lease_root != os.path.normcase(str(self.root)):
                continue
            workers.append(worker)
            phase = worker["phase"]
            metric_id = "training" if phase in ("training", "collecting", "optimizing", "initialization") else phase
            while row:
                active_metrics.setdefault(row["id"], set()).add(metric_id)
                row = by_id.get(row.get("parent_id"))
        overall = overall_progress(rows, manifest.get("progress_weights"))
        eta = self.estimator.update(rows, now, active_metrics)
        status = state.get("status", "waiting")
        stale = bool(state and now - state.get("updated_unix", now) > 30 and status == "running")
        if status == "running" and state.get("controller_pid") and state.get("controller_birth"):
            stale = stale or process_birth(state["controller_pid"]) != state["controller_birth"]
        for row in rows:
            row.pop("_raw", None)
            row.pop("_progress_path", None)
            row.pop("_verified_receipt", None)
        warnings = [] if source_available else ["尚未读取到控制器状态，请检查启动记录和日志"]
        return dict(schema=1, updated_unix=now, source_updated_unix=state.get("updated_unix"), warnings=warnings,
                    status=status, stale=stale, tasks=rows, active_workers=workers, machine_workers=machine_workers,
                    resources=snapshot["resources"], global_used=len(leases), global_limit=limit,
                    execution_quota=state.get("execution_quota", manifest.get("execution_quota")),
                    batch_compute_slots=state.get("batch_compute_slots"), deployment_id=state.get("deployment_id"),
                    overall=overall, eta_seconds=eta,
                    eta_basis="近期进展速度" if eta is not None else "估算中",
                    root=str(self.root), state_path=str(self.state_path), manifest=str(self.manifest_path))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", required=True)
    parser.add_argument("--state")
    parser.add_argument("--manifest")
    parser.add_argument("--slot-root")
    parser.add_argument("--interval", type=float, default=5)
    parser.add_argument("--once", action="store_true")
    parser.add_argument("--browse", nargs="?", const="", help="List remote directories once")
    parser.add_argument("--log", help="Read a registered log path once")
    parser.add_argument("--offset", type=int, default=0)
    parser.add_argument("--max-bytes", type=int, default=65536)
    args = parser.parse_args()
    if args.browse is not None:
        print(json.dumps(browse_directories(args.browse), ensure_ascii=False), flush=True)
        return
    observer = Observer(args.root, args.state, args.manifest, args.slot_root)
    if args.log:
        snapshot = observer.snapshot()
        allowed = {str(Path(row[key]).resolve()) for row in snapshot["tasks"]
                   for key in ("stdout", "stderr") if row.get(key)}
        if str(Path(args.log).resolve()) not in allowed:
            raise ValueError("Log is not registered by an observed task")
        print(json.dumps(read_log(args.log, args.offset, args.max_bytes), ensure_ascii=False), flush=True)
        return
    while True:
        started = time.monotonic()
        print(json.dumps(observer.snapshot(), ensure_ascii=False, allow_nan=False), flush=True)
        if args.once:
            return
        time.sleep(max(0.1, args.interval - (time.monotonic() - started)))


if __name__ == "__main__":
    main()
