"""Small integration API for scientific workers and their validation stages."""
import argparse
import concurrent.futures
import contextlib
import copy
import hashlib
import importlib.util
import os
from pathlib import Path
import time
import uuid

from native_runtime import atomic_json, load_json
from slots import current_slot

_runner = None


def worker_identity(scientific_fields, worker_runtime=None):
    """Use the configured compute identity, including from pythonw callers.

    Controllers pass common['worker_environment'] explicitly. Workloads inherit
    the exact environment in their immutable task spec.
    """
    if worker_runtime is None:
        spec = os.environ.get("GTR_RUNTIME_SPEC")
        if not spec:
            raise ValueError("A controller must pass the configured worker environment")
        worker_runtime = load_json(spec)["worker_environment"]
    return dict(science=copy.deepcopy(scientific_fields), worker=copy.deepcopy(worker_runtime))


def run_cases(manifest, root=None, python=None):
    """Run an ordered validation batch while the parent learner releases its slot.

    Each child owns a separate attempt, RNG, environment and compute lease.
    Scientific code constructs the fixed validation manifest and consumes these
    ordered output paths using its registered selection rule.
    """
    global _runner
    if _runner is None:
        module_spec = importlib.util.spec_from_file_location("_gtr_public_runner", Path(__file__).with_name("runner.py"))
        _runner = importlib.util.module_from_spec(module_spec)
        module_spec.loader.exec_module(_runner)
    runner = _runner
    args = argparse.Namespace(manifest=str(manifest), root=root, python=python, retry_failed=False)
    manifest_path, _, common, tasks, parallel, budget = runner.prepare(args)
    common["run_id"] = "children-" + uuid.uuid4().hex
    state_path = Path(common["results"]) / "campaign_state.json"
    state_path.parent.mkdir(parents=True, exist_ok=True)
    started = time.monotonic()
    parent_path = os.environ.get("GTR_RUNTIME_SPEC")
    relation_path = None
    relation = None
    if parent_path:
        parent = load_json(parent_path)
        relation_id = hashlib.sha256((str(manifest_path) + "\0" + common["root"]).encode("utf-8")).hexdigest()[:24]
        relation_path = Path(parent["attempt"]) / "children" / (relation_id + ".json")
        relation_path.parent.mkdir(parents=True, exist_ok=True)
        relation = dict(schema=1, relation_id=relation_id,
                        parent=dict(task_id=parent.get("task_id", parent.get("slot_identity", {}).get("task_id")),
                                    attempt=parent["attempt"], run_id=parent.get("run_id")),
                        child=dict(manifest=str(manifest_path), root=common["root"],
                                   state_path=str(state_path), results=common["results"],
                                   task_ids=[task["id"] for task in tasks], run_id=common["run_id"]),
                        status="running", started_unix=time.time(), updated_unix=time.time())
        atomic_json(relation_path, relation)
    slot = current_slot()
    pause = slot.suspended() if slot is not None else contextlib.nullcontext()
    completed = {}
    status = "failed"
    try:
        with pause:
            deadline = time.monotonic() + budget
            runner.publish_state(state_path, common, tasks, completed, started, common["run_id"])
            with concurrent.futures.ThreadPoolExecutor(max_workers=parallel) as pool:
                futures = [pool.submit(runner.task_run, task, common, deadline) for task in tasks]
                for future in concurrent.futures.as_completed(futures):
                    outcome = future.result()
                    completed[outcome["id"]] = outcome
                    runner.publish_state(state_path, common, tasks, completed, started, common["run_id"])
            outcomes = [future.result() for future in futures]
            failures = [row for row in outcomes if row["status"] not in ("complete", "verified_resume")]
            if failures:
                raise RuntimeError("Validation cases failed; attempts retained: " + repr(failures))
        status = "complete"
        return outcomes
    finally:
        try:
            runner.publish_state(state_path, common, tasks, completed, started, common["run_id"], status)
        finally:
            if relation is not None:
                relation.update(status=status, updated_unix=time.time())
                atomic_json(relation_path, relation)
