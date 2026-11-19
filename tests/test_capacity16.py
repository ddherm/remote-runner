"""Capacity and transport regressions without scientific computation."""
import json
from pathlib import Path
from subprocess import CompletedProcess
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'tools'))
from slots import MAX_CAPACITY, ComputeSlot
from native_runtime import atomic_json
from bundle_sync import capture_checked, fallback_ssh_options


class CapacityTests(unittest.TestCase):
    def test_sixteen_leases_and_seventeenth_waits(self):
        with tempfile.TemporaryDirectory(dir=Path(__file__).parent) as directory:
            root = Path(directory)
            held = []
            try:
                for index in range(16):
                    lease = ComputeSlot(root / 'slots', {'case': index}, root / ('s%d.json' % index), capacity=16)
                    lease.acquire(timeout=1)
                    held.append(lease)
                self.assertEqual(len({lease.index for lease in held}), 16)
                extra = ComputeSlot(root / 'slots', {}, root / 'extra.json', capacity=16)
                with self.assertRaises(TimeoutError):
                    extra.acquire(timeout=.05)
                held.pop().release()
                extra.acquire(timeout=1)
                extra.release()
                with self.assertRaises(ValueError):
                    ComputeSlot(root / 'slots', {}, root / 'bad.json', capacity=MAX_CAPACITY + 1)
            finally:
                for lease in held:
                    lease.release()

    def test_existing_twelve_policy_requires_handover(self):
        with tempfile.TemporaryDirectory(dir=Path(__file__).parent) as directory:
            root = Path(directory)
            ComputeSlot(root / 'slots', {}, root / 'old.json', capacity=12)
            with self.assertRaisesRegex(ValueError, 'different capacity'):
                ComputeSlot(root / 'slots', {}, root / 'new.json')

    def test_configured_fallback_reuses_alias_and_host_identity(self):
        command = ['ssh', '-o', 'BatchMode=yes', 'windows-runner', 'hostname']
        options = fallback_ssh_options('windows-runner', '192.0.2.10', '192.0.2.20')
        results = [CompletedProcess(command, 255, '', 'connection timeout'),
                   CompletedProcess(command, 0, 'test-computer', '')]
        with patch('bundle_sync.subprocess.run', side_effect=results) as run:
            self.assertEqual(capture_checked(command, 'probe', options).stdout, 'test-computer')
            fallback = run.call_args_list[1].args[0]
            self.assertIn('Hostname=192.0.2.10', fallback)
            self.assertIn('HostKeyAlias=192.0.2.20', fallback)
            self.assertIn('windows-runner', fallback)

    def test_unconfigured_fallback_keeps_connect_error(self):
        command = ['ssh', 'windows-runner', 'hostname']
        with patch('bundle_sync.subprocess.run', return_value=CompletedProcess(command, 255, '', 'offline')) as run:
            with self.assertRaisesRegex(RuntimeError, 'offline'):
                capture_checked(command, 'probe', fallback_ssh_options('windows-runner', None, None))
            run.assert_called_once()
