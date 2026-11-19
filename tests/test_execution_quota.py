"""Batch and machine admission helpers; no scientific imports or computation."""
import argparse
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from unittest.mock import patch

TOOLS = Path(__file__).resolve().parents[1] / "tools"
sys.path.insert(0, str(TOOLS))
from native_runtime import atomic_json, child_environment, load_json
from runner import prepare
from slots import ComputeLease, ComputeSlot, active_slots

WORKER = '''import os,time
from pathlib import Path
from native_runtime import atomic_json,load_json
spec=load_json(os.environ['GTR_RUNTIME_SPEC'])
started=time.time()
time.sleep(0.2)
atomic_json(Path(spec['attempt'])/'result.json',dict(start=started,end=time.time(),quota=spec.get('execution_quota')))
'''

PARENT = '''import os
from pathlib import Path
from native_runtime import atomic_json,load_json
from runner_api import run_cases
spec=load_json(os.environ['GTR_RUNTIME_SPEC'])
root=Path(spec['root']);attempt=Path(spec['attempt'])
manifest=load_json(root/'child-template.json')
manifest['results']=(attempt/'child-results').relative_to(root).as_posix()
path=attempt/'validation.json';atomic_json(path,manifest)
rows=run_cases(path,root=str(root),python=spec['worker_python'])
atomic_json(attempt/'result.json',dict(ids=[row['id'] for row in rows],quota=spec['execution_quota']))
'''


class ExecutionQuotaTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix="gtr-quota-")
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name).resolve()

    def quota(self, name="batch", capacity=1):
        return dict(id=name, root=str(self.root / "quotas" / name), capacity=capacity)

    def test_thirty_six_slots_and_thirty_seventh_waits(self):
        held = []
        try:
            for index in range(36):
                lease = ComputeSlot(self.root / "machine", {}, self.root / ("s%d.json" % index), 36)
                lease.acquire(timeout=1)
                held.append(lease)
            self.assertEqual(len(active_slots(self.root / "machine")), 36)
            extra = ComputeSlot(self.root / "machine", {}, self.root / "extra.json", 36)
            with self.assertRaises(TimeoutError):
                extra.acquire(timeout=0.04)
            held.pop().release()
            extra.acquire(timeout=1)
            extra.release()
        finally:
            for lease in held:
                lease.release()

    def test_waiting_batch_does_not_reserve_machine_capacity(self):
        first = ComputeLease(self.root / "machine", {}, self.root / "first.json", 2, self.quota())
        first.acquire()
        self.addCleanup(first.release)
        second = ComputeLease(self.root / "machine", {}, self.root / "second.json", 2, self.quota())
        errors = []
        def wait():
            try:
                second.acquire(timeout=0.25)
            except TimeoutError as error:
                errors.append(error)
        thread = threading.Thread(target=wait)
        thread.start()
        try:
            deadline = time.monotonic() + 2
            while not (self.root / "second.json").exists() and time.monotonic() < deadline:
                time.sleep(0.005)
            state = load_json(self.root / "second.json")
            self.assertEqual(state["status"], "waiting_batch_slot")
            self.assertEqual(state["deployment_id"], "batch")
            self.assertEqual(len(active_slots(self.root / "machine")), 1)
        finally:
            thread.join(timeout=3)
            second.release()
        self.assertEqual(len(errors), 1)

    def test_waiting_machine_timeout_refunds_batch_permit(self):
        occupied = ComputeSlot(self.root / "machine", {}, self.root / "occupied.json", 1)
        occupied.acquire()
        self.addCleanup(occupied.release)
        contender = ComputeLease(self.root / "machine", {}, self.root / "waiting.json", 1, self.quota())
        with self.assertRaises(TimeoutError):
            contender.acquire(timeout=0.05)
        self.assertEqual(active_slots(self.root / "quotas/batch"), [])
        self.assertEqual(len(active_slots(self.root / "machine")), 1)

    def test_crashed_process_releases_both_os_locks(self):
        source = """import json,sys,time
from pathlib import Path
from slots import ComputeLease
from native_runtime import atomic_json
root=Path(sys.argv[1])
quota=dict(id='batch',root=str(root/'quotas/batch'),capacity=1)
lease=ComputeLease(root/'machine',{},root/'crash.json',1,quota)
lease.acquire()
atomic_json(root/'ready.json',dict(ready=True))
time.sleep(30)
"""
        environment = child_environment()
        process = subprocess.Popen([sys.executable, "-X", "utf8", "-B", "-c", source, str(self.root)], env=environment)
        try:
            deadline = time.monotonic() + 10
            while not (self.root / "ready.json").exists() and time.monotonic() < deadline:
                if process.poll() is not None:
                    self.fail("Lease helper exited before publishing readiness")
                time.sleep(0.01)
            self.assertTrue((self.root / "ready.json").exists())
            process.kill()
            process.wait(timeout=5)
            with ComputeLease(self.root / "machine", {}, self.root / "replacement.json", 1, self.quota()):
                self.assertEqual(len(active_slots(self.root / "machine")), 1)
                self.assertEqual(len(active_slots(self.root / "quotas/batch")), 1)
        finally:
            if process.poll() is None:
                process.kill()
                process.wait(timeout=5)

    def test_parent_suspension_releases_and_reacquires_both_permits(self):
        first = ComputeLease(self.root / "machine", {}, self.root / "parent.json", 1, self.quota())
        with first:
            with first.suspended():
                self.assertEqual(active_slots(self.root / "machine"), [])
                self.assertEqual(active_slots(self.root / "quotas/batch"), [])
                with ComputeLease(self.root / "machine", {}, self.root / "child.json", 1, self.quota()):
                    self.assertEqual(len(active_slots(self.root / "machine")), 1)
            self.assertIsNotNone(first.stream)
            self.assertEqual(len(active_slots(self.root / "quotas/batch")), 1)
        self.assertEqual(active_slots(self.root / "machine"), [])

    def prepare_manifest(self, value):
        path = self.root / "manifest.json"
        atomic_json(path, dict(results="control", budget_seconds=60, tasks=[], **value))
        args = argparse.Namespace(manifest=str(path), root=str(self.root), python=sys.executable)
        with patch("runner.worker_environment", return_value={"fixture": True}):
            return prepare(args)

    def test_quota_inherits_and_conflicts_cannot_escape(self):
        parent = self.root / "parent.json"
        quota = self.quota(capacity=30)
        atomic_json(parent, dict(execution_quota=quota, slot_root=str(self.root / "machine"), slot_capacity=36))
        with patch.dict(os.environ, {"GTR_RUNTIME_SPEC": str(parent)}):
            prepared = self.prepare_manifest({})
            self.assertEqual(prepared[2]["execution_quota"], quota)
            self.assertEqual(prepared[2]["slot_capacity"], 36)
            self.assertEqual(prepared[4], 30)
            with self.assertRaisesRegex(ValueError, "conflicts"):
                self.prepare_manifest(dict(execution_quota=self.quota(capacity=36)))
            with self.assertRaisesRegex(ValueError, "machine capacity"):
                self.prepare_manifest(dict(slot_capacity=18))
        with patch.dict(os.environ, {"GTR_RUNTIME_SPEC": ""}):
            self.assertEqual(self.prepare_manifest({})[4], 18)
            self.assertEqual(self.prepare_manifest(dict(slot_capacity=36))[4], 36)

    def test_invalid_quota_and_reserved_environment_rejected(self):
        with patch.dict(os.environ, {"GTR_RUNTIME_SPEC": ""}):
            for quota in (self.quota(capacity=0), self.quota(capacity=37),
                          dict(self.quota(), root="relative"), dict(self.quota(), id="../escape")):
                with self.assertRaises(ValueError):
                    self.prepare_manifest(dict(execution_quota=quota))
            with self.assertRaisesRegex(ValueError, "override"):
                self.prepare_manifest(dict(env={"GTR_RUNTIME_SPEC": "forged.json"}))
            with self.assertRaisesRegex(ValueError, "different directories"):
                self.prepare_manifest(dict(slot_root=self.quota()["root"], execution_quota=self.quota()))

    def write_fixture(self):
        (self.root / "worker.py").write_text(WORKER, encoding="utf-8")
        (self.root / "parent.py").write_text(PARENT, encoding="utf-8")

    def task(self, name, workspace, parent=False):
        return dict(id=name, workspace=workspace, argv=["{python}", "{root}/" + ("parent.py" if parent else "worker.py")],
                    timeout_seconds=40, outputs=["result.json"], metadata=dict(phase="validation"))

    def inputs(self, names):
        return [dict(path=name, sha256=hashlib.sha256((self.root / name).read_bytes()).hexdigest()) for name in names]

    def invoke(self, manifest):
        environment = child_environment()
        environment.pop("GTR_RUNTIME_SPEC", None)
        return subprocess.Popen([sys.executable, "-X", "utf8", "-B", str(TOOLS / "runner.py"),
                                  "--root", str(self.root), "--manifest", str(manifest), "--execute"],
                                 env=environment, stdout=subprocess.PIPE, stderr=subprocess.PIPE)

    def test_nested_run_cases_keeps_order_registers_children_and_inherits_quota(self):
        self.write_fixture()
        template = dict(results="placeholder", budget_seconds=60, inputs=self.inputs(["worker.py"]),
                        tasks=[self.task("validation-%d" % index, "validation") for index in range(3)])
        atomic_json(self.root / "child-template.json", template)
        manifest = dict(results="control", budget_seconds=60, slot_root=str(self.root / "machine"), slot_capacity=2,
                        execution_quota=self.quota(), inputs=self.inputs(["worker.py", "parent.py", "child-template.json"]),
                        tasks=[self.task("parent", "training", parent=True)])
        path = self.root / "manifest.json"
        atomic_json(path, manifest)
        process = self.invoke(path)
        out, error = process.communicate(timeout=60)
        self.assertEqual(process.returncode, 0, (out + error).decode("utf-8", errors="replace"))
        result = load_json(next(self.root.glob("training/runs/parent/a*/result.json")))
        self.assertEqual(result["ids"], ["validation-0", "validation-1", "validation-2"])
        relation = load_json(next(self.root.glob("training/runs/parent/a*/children/*.json")))
        self.assertEqual(relation["status"], "complete")
        self.assertEqual(relation["parent"]["task_id"], "parent")
        self.assertTrue(relation["parent"]["run_id"].startswith("run-"))
        self.assertTrue(Path(relation["child"]["manifest"]).is_absolute())
        self.assertEqual(load_json(Path(relation["child"]["state_path"]))["status"], "complete")
        children = [load_json(path) for path in self.root.glob("validation/runs/*/a*/result.json")]
        self.assertEqual(len(children), 3)
        self.assertTrue(all(row["quota"] == self.quota() for row in children))
        ordered = sorted(children, key=lambda row: row["start"])
        self.assertTrue(all(left["end"] <= right["start"] for left, right in zip(ordered, ordered[1:])))

    def test_two_batches_obey_own_limits_and_shared_machine_limit(self):
        self.write_fixture()
        processes = []
        for name in ("first", "second"):
            manifest = dict(results=name + "-control", budget_seconds=60,
                            slot_root=str(self.root / "machine"), slot_capacity=3,
                            execution_quota=self.quota(name, capacity=2), inputs=self.inputs(["worker.py"]),
                            tasks=[self.task("case%d" % index, name) for index in range(5)])
            path = self.root / (name + ".json")
            atomic_json(path, manifest)
            processes.append(self.invoke(path))
        try:
            for process in processes:
                output, error = process.communicate(timeout=60)
                self.assertEqual(process.returncode, 0, (output + error).decode("utf-8", errors="replace"))
        finally:
            for process in processes:
                if process.poll() is None:
                    process.kill()
                    process.communicate()
        records = [load_json(path) for path in self.root.glob("*/runs/*/a*/result.json")]
        self.assertEqual(len(records), 10)
        events = sorted([(row["start"], 1, row["quota"]["id"]) for row in records] +
                        [(row["end"], -1, row["quota"]["id"]) for row in records])
        counts = dict(first=0, second=0)
        peak = 0
        for _, change, name in events:
            counts[name] += change
            self.assertLessEqual(counts[name], 2)
            self.assertLessEqual(sum(counts.values()), 3)
            peak = max(peak, sum(counts.values()))
        self.assertGreaterEqual(peak, 2)
        self.assertEqual(active_slots(self.root / "machine"), [])


if __name__ == "__main__":
    unittest.main(verbosity=2)
