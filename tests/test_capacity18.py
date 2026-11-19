"""Eighteen-slot policy regressions; no scientific imports or computation."""
import argparse
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'tools'))
from native_runtime import atomic_json
from runner import prepare
from slots import MAX_CAPACITY, ComputeSlot


class Capacity18Tests(unittest.TestCase):
    def test_default_eighteen_leases_and_nineteenth_waits(self):
        with tempfile.TemporaryDirectory(dir=Path(__file__).parent) as directory:
            root = Path(directory)
            held = []
            try:
                for index in range(18):
                    lease = ComputeSlot(root / 'slots', {'case': index}, root / ('s%d.json' % index))
                    lease.acquire(timeout=1)
                    held.append(lease)
                self.assertEqual(len({lease.index for lease in held}), 18)
                extra = ComputeSlot(root / 'slots', {}, root / 'extra.json')
                with self.assertRaises(TimeoutError):
                    extra.acquire(timeout=.05)
                released_index = held[-1].index
                held.pop().release()
                extra.acquire(timeout=1)
                self.assertEqual(extra.index, released_index)
                extra.release()
                for unsupported in (0, MAX_CAPACITY + 1):
                    with self.assertRaises(ValueError):
                        ComputeSlot(root / 'slots', {}, root / 'bad.json', capacity=unsupported)
            finally:
                for lease in held:
                    lease.release()

    def test_existing_sixteen_policy_requires_handover(self):
        with tempfile.TemporaryDirectory(dir=Path(__file__).parent) as directory:
            root = Path(directory)
            ComputeSlot(root / 'slots', {}, root / 'old.json', capacity=16)
            with self.assertRaisesRegex(ValueError, 'different capacity'):
                ComputeSlot(root / 'slots', {}, root / 'new.json')

    def test_manifest_defaults_and_bounds(self):
        with tempfile.TemporaryDirectory(dir=Path(__file__).parent) as directory:
            root = Path(directory).resolve()
            manifest_path = root / 'manifest.json'
            manifest = dict(results='control', budget_seconds=60, tasks=[])
            args = argparse.Namespace(manifest=str(manifest_path), root=str(root), python=sys.executable)
            atomic_json(manifest_path, manifest)
            with patch('runner.worker_environment', return_value={'fixture': True}):
                prepared = prepare(args)
                self.assertEqual(prepared[2]['slot_capacity'], 18)
                self.assertEqual(prepared[4], 18)
                for field in ('max_parallel', 'slot_capacity'):
                    for unsupported in (0, MAX_CAPACITY + 1):
                        atomic_json(manifest_path, dict(manifest, **{field: unsupported}))
                        with self.assertRaises(ValueError):
                            prepare(args)
                for supported in (12, 16, 18, 36):
                    atomic_json(manifest_path, dict(manifest, max_parallel=supported, slot_capacity=supported))
                    checked = prepare(args)
                    self.assertEqual(checked[2]['slot_capacity'], supported)
                    self.assertEqual(checked[4], supported)
