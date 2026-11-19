"""Regression fixtures for recorded D/E failures. No ML imports or simulations."""
import base64
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import threading
import time
import unittest
import uuid

TOOLS = Path(__file__).resolve().parents[1] / "tools"
sys.path.insert(0, str(TOOLS))
from native_runtime import atomic_json, child_environment, install, load_json, shared_read
from monitor import format_view

RUNNER = TOOLS / "runner.py"
HELPER = '''import json, os, pathlib, subprocess, sys, time
from runner_api import worker_identity
mode = sys.argv[1]
if mode == 'fail':
    raise RuntimeError('intentional fixture failure')
if mode == 'tree':
    subprocess.Popen([sys.executable, '-c', "import time,pathlib;time.sleep(4);pathlib.Path(%r).write_text('escaped')" % sys.argv[2]])
    pathlib.Path('spawned.json').write_text('{}')
    time.sleep(30)
start = time.time()
time.sleep(float(sys.argv[2]))
result = dict(start=start, end=time.time(), cwd=os.getcwd(), temp=os.environ['TEMP'],
              identity=worker_identity(dict(seed=7)), status='bad' if mode == 'bad' else 'complete')
if os.name == 'nt':
    import ctypes
    result['console_handle'] = int(ctypes.windll.kernel32.GetConsoleWindow())
pathlib.Path('result.json').write_text(json.dumps(result),encoding='utf-8')
'''


class RuntimeTests(unittest.TestCase):
    def setUp(self):
        self.root = Path(__file__).resolve().parent / "test_artifacts" / (self._testMethodName[:24] + "-" + uuid.uuid4().hex[:8])
        self.root.mkdir(parents=True)
        (self.root / "worker.py").write_text(HELPER, encoding="utf-8")
        self.manifest = self.root / "manifest.json"

    def configure(self, modes=("ok",), capacity=12, duration="0.1", timeout=15):
        tasks = []
        for index, mode in enumerate(modes):
            tasks.append(dict(id="case%d" % index, workspace="H-20261104", argv=["{python}", "{root}/worker.py", mode, duration],
                timeout_seconds=timeout, outputs=["result.json"], json_checks=[dict(path="result.json", equals={"status": "complete"})],
                metadata=dict(group="H", phase="validation", training_seed=7, environment_seed=1000 + index)))
        value = dict(results="control", max_parallel=12, budget_seconds=60, slot_root=str(self.root / "slots"),
                     slot_capacity=capacity, inputs=[dict(path="worker.py", sha256=hashlib.sha256((self.root / "worker.py").read_bytes()).hexdigest())], tasks=tasks)
        atomic_json(self.manifest, value)
        return value

    def invoke(self, *extra, manifest=None, python=None):
        return subprocess.run([str(python or sys.executable), "-X", "utf8", "-B", str(RUNNER),
            "--manifest", str(manifest or self.manifest), "--root", str(self.root), "--python", sys.executable, *extra],
            capture_output=True, text=True, encoding="utf-8", timeout=90, env=child_environment())

    def test_execute_resume_and_explicit_worker_identity(self):
        self.configure(("ok", "ok"))
        result = self.invoke("--execute")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        outputs = list(self.root.glob("H-*/runs/case*/a*/result.json"))
        values = [load_json(p) for p in outputs]
        self.assertEqual(len(values), 2)
        self.assertEqual(len({v["cwd"] for v in values}), 2)
        self.assertEqual(len({v["temp"] for v in values}), 2)
        self.assertTrue(all(v["identity"]["worker"]["python_executable"] == os.path.realpath(sys.executable) for v in values))
        if os.name == "nt":
            self.assertTrue(all(v["console_handle"] == 0 for v in values))
        self.assertEqual(self.invoke("--verify").returncode, 0)
        self.assertEqual(self.invoke("--execute").stdout.count('"status": "verified_resume"'), 2)
        self.assertEqual(len(list(self.root.glob("H-*/runs/case*/a*/result.json"))), 2)

    def test_output_tamper_and_failed_status_rejected(self):
        self.configure(("bad",))
        self.assertEqual(self.invoke("--execute").returncode, 1)
        self.assertFalse(any(self.root.glob("H-*/runs/*/complete.json")))
        self.configure(("ok",))
        self.assertEqual(self.invoke("--execute", "--retry-failed").returncode, 0)
        receipt = load_json(self.root / "H-20261104/runs/case0/complete.json")
        output = self.root / "H-20261104/runs/case0" / receipt["attempt"] / "result.json"
        output.write_text("corrupt", encoding="utf-8")
        self.assertNotEqual(self.invoke("--verify").returncode, 0)

    def test_failure_kept_and_retry_requires_explicit_flag(self):
        self.configure(("fail",))
        self.assertEqual(self.invoke("--execute").returncode, 1)
        self.assertEqual(self.invoke("--execute").returncode, 1)
        self.assertEqual(len(list(self.root.glob("H-*/runs/*/a*/stderr.log"))), 1)
        self.assertEqual(self.invoke("--execute", "--retry-failed").returncode, 1)
        self.assertEqual(len(list(self.root.glob("H-*/runs/*/a*/stderr.log"))), 2)

    def test_input_change_and_unsafe_paths(self):
        value = self.configure()
        (self.root / "worker.py").write_text(HELPER + "\n# changed", encoding="utf-8")
        self.assertEqual(self.invoke().returncode, 2)
        (self.root / "worker.py").write_text(HELPER, encoding="utf-8")
        value["tasks"][0]["outputs"] = ["../escape.json"]
        atomic_json(self.manifest, value)
        self.assertEqual(self.invoke().returncode, 2)
        value["tasks"][0]["outputs"] = ["result.json"]
        value["tasks"].append(dict(value["tasks"][0], id="CASE0"))
        atomic_json(self.manifest, value)
        self.assertEqual(self.invoke().returncode, 2)

    def test_lock_recovery_rejects_live_owner_and_archives_dead_owner(self):
        import platform
        from native_runtime import process_birth
        self.configure()
        lock = self.root / "control/controller.active.lock"
        lock.mkdir(parents=True)
        atomic_json(lock / "owner.json", dict(pid=os.getpid(), birth=process_birth(os.getpid()), host=platform.node()))
        self.assertEqual(self.invoke("--recover-locks").returncode, 2)
        self.assertTrue(lock.exists())
        atomic_json(lock / "owner.json", dict(pid=1073741823, birth="old", host=platform.node()))
        self.assertEqual(self.invoke("--recover-locks").returncode, 0)
        self.assertFalse(lock.exists())
        self.assertEqual(len(list(self.root.glob("control/controller.active.lock.abandoned-*/recovery_review.json"))), 1)

    def test_shared_capacity_across_two_controllers(self):
        value = self.configure(("ok",) * 4, capacity=2, duration="0.4")
        second = json.loads(json.dumps(value))
        second["results"] = "control2"
        for task in second["tasks"]:
            task["workspace"] = "P-20261104"
        second_path = self.root / "second.json"
        atomic_json(second_path, second)
        command = [sys.executable, "-X", "utf8", "-B", str(RUNNER), "--manifest", str(self.manifest), "--execute"]
        one = subprocess.Popen(command, stdout=subprocess.PIPE, stderr=subprocess.PIPE, env=child_environment())
        command[command.index(str(self.manifest))] = str(second_path)
        two = subprocess.Popen(command, stdout=subprocess.PIPE, stderr=subprocess.PIPE, env=child_environment())
        first_out = one.communicate(timeout=60)
        second_out = two.communicate(timeout=60)
        self.assertEqual(one.returncode, 0, repr(first_out))
        self.assertEqual(two.returncode, 0, repr(second_out))
        values = [load_json(p) for p in self.root.glob("*-20261104/runs/case*/a*/result.json")]
        self.assertEqual(len(values), 8)
        events = sorted([(v["start"], 1) for v in values] + [(v["end"], -1) for v in values])
        active = peak = 0
        for _, change in events:
            active += change
            peak = max(active, peak)
        self.assertEqual(peak, 2)

    def test_parent_releases_slot_for_parallel_validation(self):
        self.configure(capacity=1)
        train = '''import json,os,pathlib
from native_runtime import atomic_json,load_json
from runner_api import run_cases
spec=load_json(os.environ['GTR_RUNTIME_SPEC'])
root=pathlib.Path(spec['root'])
manifest=load_json(root/'manifest.json')
manifest['tasks'][0]['id']='validation'
manifest['tasks'][0]['argv']=['{python}','{root}/worker.py','ok','0.01']
manifest['tasks'][0].pop('json_checks',None)
path=pathlib.Path('validation.json').resolve()
atomic_json(path,manifest)
rows=run_cases(path,root=str(root),python=spec['worker_python'])
atomic_json('result.json',dict(status='complete',children=len(rows)))
'''
        (self.root / "train.py").write_text(train, encoding="utf-8")
        value = load_json(self.manifest)
        value["inputs"].append(dict(path="train.py", sha256=hashlib.sha256((self.root / "train.py").read_bytes()).hexdigest()))
        value["tasks"][0]["argv"] = ["{python}", "{root}/train.py"]
        atomic_json(self.manifest, value)
        result = self.invoke("--execute")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertTrue((self.root / "H-20261104/runs/validation/complete.json").exists())

    def test_timeout_cleans_owned_descendants(self):
        value = self.configure(("tree", "ok"), timeout=2)
        marker = self.root / "escaped.txt"
        value["tasks"][0]["argv"][-1] = str(marker)
        atomic_json(self.manifest, value)
        result = self.invoke("--execute")
        self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
        self.assertTrue(any(self.root.glob("H-*/runs/*/a*/spawned.json")))
        self.assertTrue((self.root / "H-20261104/runs/case1/complete.json").exists())
        time.sleep(4.2)
        self.assertFalse(marker.exists())
        value["tasks"] = [dict(value["tasks"][1], id="after_timeout")]
        atomic_json(self.manifest, value)
        self.assertEqual(self.invoke("--execute").returncode, 0)

    def test_passive_reads_allow_atomic_writes(self):
        path = self.root / "progress.json"
        atomic_json(path, dict(step=0))
        errors = []
        stop = threading.Event()
        def reader():
            while not stop.is_set():
                try:
                    load_json(path)
                except Exception as error:
                    errors.append(repr(error))
        thread = threading.Thread(target=reader)
        thread.start()
        try:
            for step in range(60):
                atomic_json(path, dict(step=step))
        finally:
            stop.set()
            thread.join()
        self.assertFalse(errors)

    def test_pathlib_import_orders_and_bootstrap(self):
        for order in ("before", "after", "startup"):
            code = ("import pathlib;from native_runtime import install;install();" if order == "before" else
                    "from native_runtime import install;install();import pathlib;")
            code += "import os;pathlib.Path('first').write_text('one');pathlib.Path('first').replace('second');os.replace('second','third')"
            env = child_environment()
            env["GTR_COMPUTE_BOOTSTRAP"] = "1" if order == "startup" else "0"
            result = subprocess.run([sys.executable, "-X", "utf8", "-B", "-c", code], cwd=self.root,
                                    env=env, capture_output=True, timeout=20)
            self.assertEqual(result.returncode, 0, repr(result.stderr))
            (self.root / "third").unlink()

    def test_monitor_has_no_historical_failure_alarm(self):
        text = format_view(dict(status="complete", updated_unix=time.time(), tasks=[
            dict(id="P", status="complete", metadata={}, progress=dict(completed_episodes=200))],
            failures=[dict(detail="OLD_RESOLVED_FAILURE")]), self.root, {})
        self.assertNotIn("OLD_RESOLVED_FAILURE", text)
        self.assertIn("200", text)

    def test_real_deployment_scripts_fit_transport(self):
        from bundle_sync import python_ps, ssh_ps, VERIFY_EXTRACT, EXPORT_SNAPSHOT
        for source in (VERIFY_EXTRACT, EXPORT_SNAPSHOT):
            script = python_ps(r'C:\Python\python.exe', source,
                               [r'E:\RemoteRunner\含空格 文件夹\runtime\archive.zip', 'release', 'f' * 64])
            command = ssh_ps('windows-runner', script)[-1]
            self.assertLessEqual(len(command.split(' -EncodedCommand ',1)[1]),7400)

    def test_atomic_publication_under_long_parent(self):
        padding = 210 - len(str(self.root)) - 1
        self.assertGreater(padding, 0)
        parent = self.root / ("p" * padding)
        parent.mkdir()
        path = parent / "record_with_a_long_name.json"
        atomic_json(path, dict(status="published"))
        self.assertEqual(load_json(path), dict(status="published"))

    @unittest.skipUnless(os.name == "nt", "Native Windows held-handle regression")
    def test_windows_held_reader_retried_for_os_and_path(self):
        install()
        import ctypes
        from ctypes import wintypes as w
        kernel = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel.CreateFileW.argtypes = [w.LPCWSTR,w.DWORD,w.DWORD,ctypes.c_void_p,w.DWORD,w.DWORD,w.HANDLE]
        kernel.CreateFileW.restype = w.HANDLE
        kernel.CloseHandle.argtypes = [w.HANDLE]
        for kind in ("os", "path"):
            source, target = self.root / "source", self.root / "target"
            source.write_text("new")
            target.write_text("old")
            handle = kernel.CreateFileW(str(target), 0x80000000, 1, None, 3, 0x80, None)
            self.assertNotEqual(handle, ctypes.c_void_p(-1).value)
            timer = threading.Timer(0.15, lambda: kernel.CloseHandle(handle))
            timer.start()
            try:
                if kind == "os":
                    os.replace(source, target)
                else:
                    source.replace(target)
            finally:
                timer.join()
            self.assertEqual(target.read_text(), "new")

    @unittest.skipUnless(os.name == "nt", "Native Windows pythonw controller regression")
    def test_pythonw_controller_and_python_worker_share_receipts(self):
        self.configure()
        pythonw = Path(sys.executable).with_name("pythonw.exe")
        command = [str(pythonw), "-X", "utf8", "-B", str(TOOLS / "controller_entry.py"),
            "--log-root", str(self.root / "controller_logs"), "--manifest", str(self.manifest),
            "--root", str(self.root), "--python", sys.executable, "--execute"]
        proc = subprocess.Popen(command, env=child_environment())
        self.assertEqual(proc.wait(timeout=40), 0)
        self.assertEqual(self.invoke("--verify").returncode, 0)
        proof = load_json(next((self.root / "controller_logs").glob("*/startup.json")))
        self.assertEqual(proof["console_handle"], 0)

    @unittest.skipUnless(os.name == "nt", "Native PowerShell parser")
    def test_powershell_launcher_parses(self):
        path = str(TOOLS / "launch_windows.ps1").replace("'", "''")
        script = "$t=$null;$e=$null;[void][Management.Automation.Language.Parser]::ParseFile('%s',[ref]$t,[ref]$e);if($e.Count){throw ($e|Out-String)}" % path
        encoded = base64.b64encode(script.encode("utf-16-le")).decode("ascii")
        result = subprocess.run(["powershell.exe", "-NoProfile", "-EncodedCommand", encoded], capture_output=True, timeout=20)
        self.assertEqual(result.returncode, 0, repr(result.stderr))

    @unittest.skipUnless(os.name == "nt", "Native PowerShell argument transport")
    def test_powershell_preserves_json_unicode_and_literal_arguments(self):
        from bundle_sync import python_ps
        values = ['', 'a"b', "a'b", '中文 路径\\end\\', '$HOME $(whoami) `literal`', 'line1\nline2']
        script = python_ps(sys.executable, 'import json,sys;print(json.dumps(sys.argv[1:],ensure_ascii=False))', values)
        command = base64.b64encode(script.encode('utf-16-le')).decode('ascii')
        result = subprocess.run(['powershell.exe','-NoProfile','-EncodedCommand',command],
                                capture_output=True,timeout=20)
        self.assertEqual(result.returncode, 0, repr(result.stderr))
        self.assertEqual(json.loads(result.stdout.decode('utf-8').strip().splitlines()[-1]), values)

    @unittest.skipUnless(os.name == "nt", "Native compressed SSH-script transport")
    def test_long_script_transport_uses_compressed_envelope(self):
        from bundle_sync import python_ps, ssh_ps
        code = ('# long deployment script 中文\n' * 500) + 'import json;print(json.dumps({"status":"passed"}))'
        script = ('# long PowerShell script 中文\n'*500) + python_ps(sys.executable, code, [])
        command = ssh_ps('windows-runner', script)[-1]
        encoded = command.split(' -EncodedCommand ',1)[1]
        self.assertLessEqual(len(encoded),7400)
        result = subprocess.run(['powershell.exe','-NoProfile','-EncodedCommand',encoded],capture_output=True,timeout=20)
        self.assertEqual(result.returncode,0,repr(result.stderr))
        self.assertEqual(json.loads(result.stdout.decode('utf-8').strip().splitlines()[-1]),dict(status='passed'))


if __name__ == "__main__":
    unittest.main(verbosity=2)
