"""Capacity handover guards with injectable process census; no remote execution."""
import json
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

TOOLS = Path(__file__).resolve().parents[1] / "tools"
sys.path.insert(0, str(TOOLS))
from native_runtime import atomic_json, load_json
from slots import ComputeSlot, capacity_lock
from machine_policy import PolicyBlocked, change_policy, is_dispatcher


def census(commands=(), complete=True):
    return dict(schema=1, current_user="SYS\\PC", complete=complete,
                processes=[dict(pid=100 + index, owner="SYS\\PC", name="python.exe", commandline=command)
                           for index, command in enumerate(commands)], uncertain=[] if complete else [dict(pid=50)])


class MachinePolicyTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix="gtr-machine-policy-")
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name).resolve() / "machine"

    def seed(self):
        self.root.mkdir()
        atomic_json(self.root / "capacity.json", dict(capacity=18))

    def apply(self, scanner=None, lookup=None):
        with patch("machine_policy.WINDOWS", True):
            return change_policy(self.root, 36, execute=True,
                                 process_scanner=scanner or (lambda: census()), birth_lookup=lookup or (lambda pid: None))

    def test_plan_is_read_only_and_non_windows_execute_refused(self):
        plan = change_policy(self.root, 36)
        self.assertEqual(plan["status"], "plan")
        self.assertIsNone(plan["previous_capacity"])
        self.assertFalse(self.root.exists())
        with patch("machine_policy.WINDOWS", False):
            with self.assertRaisesRegex(PolicyBlocked, "native Windows"):
                change_policy(self.root, 36, execute=True)
        self.assertFalse(self.root.exists())

    def test_dispatchers_and_waiting_workers_block_but_observers_do_not(self):
        blocked = [r'python.exe "E:\long root\runner.py" --execute --manifest m.json',
                   r'python.exe E:\runner.py --worker spec.json',
                   r'pythonw.exe E:\controller_entry.py --root batch',
                   r'python.exe E:\workload_entry.py spec.json',
                   r'powershell.exe -File "E:\launch_windows.ps1" -BatchRoot b']
        harmless = [r'python.exe E:\monitor.py --once --json',
                    r'python.exe E:\observer.py --root batch',
                    r'powershell.exe -File E:\launch_windows.ps1 -BatchRoot batch -ShowMonitor',
                    r'powershell.exe -File E:\launch_windows.ps1 -BatchRoot batch -MonitorOnly',
                    r'python.exe E:\runner.py --verify --manifest m.json',
                    r'python.exe E:\machine_policy.py --capacity 36 --execute']
        self.assertTrue(all(is_dispatcher(command) for command in blocked))
        self.assertFalse(any(is_dispatcher(command) for command in harmless))
        self.seed()
        original = (self.root / "capacity.json").read_bytes()
        for command in blocked:
            with self.subTest(command=command):
                with self.assertRaisesRegex(PolicyBlocked, "still present"):
                    self.apply(scanner=lambda: census([command]))
                self.assertEqual((self.root / "capacity.json").read_bytes(), original)
        receipt = self.apply(scanner=lambda: census(harmless))
        self.assertEqual(receipt["status"], "applied")

    def test_incomplete_process_census_and_permission_failures_refuse_change(self):
        self.seed()
        original = (self.root / "capacity.json").read_bytes()
        with self.assertRaisesRegex(PolicyBlocked, "incomplete"):
            self.apply(scanner=lambda: census(complete=False))
        with self.assertRaisesRegex(PolicyBlocked, "could not be verified"):
            self.apply(scanner=lambda: (_ for _ in ()).throw(PermissionError("denied")))
        self.assertEqual((self.root / "capacity.json").read_bytes(), original)

    def test_unknown_and_live_owner_records_are_preserved(self):
        self.seed()
        owner_path = self.root / "slot-00.json"
        atomic_json(owner_path, dict(pid=100))
        unknown = owner_path.read_bytes()
        with self.assertRaisesRegex(PolicyBlocked, "Incomplete"):
            self.apply()
        self.assertEqual(owner_path.read_bytes(), unknown)
        atomic_json(owner_path, dict(pid=100, process_birth="12345", slot=0, token="old", acquired_unix=1.0))
        live = owner_path.read_bytes()
        with self.assertRaisesRegex(PolicyBlocked, "still alive"):
            self.apply(lookup=lambda pid: "12345")
        self.assertEqual(owner_path.read_bytes(), live)
        with self.assertRaisesRegex(PolicyBlocked, "Cannot verify"):
            self.apply(lookup=lambda pid: (_ for _ in ()).throw(PermissionError("owner denied")))
        self.assertEqual(load_json(self.root / "capacity.json")["capacity"], 18)

    def test_stale_owners_preserved_and_handover_receipt_published(self):
        self.seed()
        owner_path = self.root / "slot-00.json"
        atomic_json(owner_path, dict(pid=100, process_birth="12345", slot=0, token="old", acquired_unix=1.0))
        original = owner_path.read_bytes()
        receipt = self.apply()
        policy = load_json(self.root / "capacity.json")
        self.assertEqual(policy["capacity"], 36)
        self.assertEqual(policy["handover_receipt"], receipt["receipt"])
        self.assertEqual(load_json(Path(receipt["receipt"]))["status"], "applied")
        self.assertEqual(owner_path.read_bytes(), original)
        self.assertEqual(receipt["preserved_owners"][0]["status"], "exited")

    def test_new_dispatcher_during_review_prevents_commit(self):
        self.seed()
        reports = iter([census(), census([r'python.exe E:\runner.py --execute'])])
        with self.assertRaisesRegex(PolicyBlocked, "still present"):
            self.apply(scanner=lambda: next(reports))
        self.assertEqual(load_json(self.root / "capacity.json")["capacity"], 18)
        self.assertFalse((self.root / "policy_handover").exists())

    def test_policy_lock_serializes_review_and_lease_admission(self):
        self.seed()
        with capacity_lock(self.root, timeout=0):
            with self.assertRaisesRegex(TimeoutError, "another operation"):
                self.apply()
        lease = ComputeSlot(self.root, {}, self.root.parent / "resource.json", capacity=18)
        self.apply()
        with self.assertRaisesRegex(ValueError, "different capacity"):
            lease.acquire(timeout=0.1)
        self.assertFalse(list(self.root.glob("slot-*.json")))

    def test_owned_lock_without_record_prevents_change(self):
        self.seed()
        lease = ComputeSlot(self.root, {}, self.root.parent / "resource.json", capacity=18)
        lease.acquire()
        try:
            (self.root / "slot-00.json").unlink()
            with self.assertRaisesRegex(PolicyBlocked, "slot lock"):
                self.apply()
        finally:
            lease.release()
        self.assertEqual(load_json(self.root / "capacity.json")["capacity"], 18)


if __name__ == "__main__":
    unittest.main(verbosity=2)
