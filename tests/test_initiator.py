"""Local handoff, immutable binding and HTTP access tests; no remote work."""
import hashlib
import json
from pathlib import Path
import sys
import tempfile
import threading
import time
import unittest
from unittest.mock import Mock, patch
from urllib.error import HTTPError
from urllib.request import Request, urlopen

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from initiator.deployment import archive_files, digest, prepare_package, safe_relative
from initiator.server import make_server
from initiator.service import Service
from initiator.transport import DEFAULTS, profile, windows_path


class FakeSSH:
    fail = False
    calls = []

    def __init__(self, host):
        self.host = host

    def run(self, code, argv=(), timeout=45):
        type(self).calls.append((code, argv))
        if self.fail:
            raise RuntimeError("连接失败")
        if "platform.platform()" in code:
            return dict(global_limit=18, python=DEFAULTS["python"], principal=r"SYS\PC", platform="Windows", runtime_available=True)
        return dict(path=argv[0], directories=[], drives=["E:\\"], exists=False)


class InitiatorTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name).resolve()
        FakeSSH.fail, FakeSSH.calls = False, []
        self.service = Service(self.root / "state", ssh_factory=FakeSSH)

    def tearDown(self):
        self.service.close()
        self.temporary.cleanup()

    def test_only_successful_connections_saved_and_rename_survives_probe(self):
        host = self.service.probe(dict(address="windows-runner"))
        self.service.rename(host["id"], "实验电脑")
        checked = self.service.probe(dict(address="windows-runner"))
        self.assertEqual(checked["label"], "实验电脑")
        self.assertEqual(len(self.service.store.list("host")), 1)
        FakeSSH.fail = True
        with self.assertRaises(RuntimeError):
            self.service.probe(dict(address="new-host"))
        self.assertEqual(len(self.service.store.list("host")), 1)
        with self.assertRaises(RuntimeError):
            self.service.probe(dict(address="windows-runner"))
        self.assertEqual(self.service.store.get("host", host["id"])["status"], "offline")

    def test_configuration_is_persisted_and_deploy_click_is_idempotent(self):
        host = self.service.probe(dict(address="windows-runner"))
        session = self.service.create(dict(title="fixture", source_root=str(self.root)))
        gate = threading.Event()
        with patch.object(self.service, "_deploy", side_effect=lambda identifier: gate.wait(2)) as run:
            first = self.service.deploy(session["id"], dict(host_id=host["id"], remote_root=r"E:\中文 文件夹", parallel=36))
            second = self.service.deploy(session["id"], dict(host_id=host["id"], remote_root=r"E:\different", parallel=1))
            self.assertEqual(first["deployment_id"], second["deployment_id"])
            self.assertEqual(second["parallel"], 36)
            self.assertEqual(second["remote_root"], r"E:\中文 文件夹")
            gate.set()
            for _ in range(50):
                if run.call_count:
                    break
                time.sleep(.01)
            self.assertEqual(run.call_count, 1)
        reopened = Service(self.root / "state", ssh_factory=FakeSSH)
        self.assertEqual(reopened.session(session["id"])["parallel"], 36)
        reopened.close()

    def test_stale_source_state_preserved_and_eta_frozen(self):
        session = self.service.create({})
        self.service.store.update("session", session["id"], status="running",
            snapshot=dict(updated_unix=time.time(), stale=True, eta_seconds=100))
        snapshot = self.service.session(session["id"])["snapshot"]
        self.assertTrue(snapshot["stale"])
        self.assertIsNone(snapshot["eta_seconds"])

    def test_workspace_history_and_settings_are_scoped_to_saved_hosts(self):
        alpha = self.service.probe(dict(address="alpha"))["id"]
        beta = self.service.probe(dict(address="beta"))["id"]
        first = self.service.create(dict(source_root=str(self.root)))["id"]
        second = self.service.create({})["id"]
        legacy = self.service.store.put("session", dict(status="draft", request={}))
        self.service.assign(first, dict(host_id=alpha, remote_root=r"E:\Alpha", parallel=4))
        self.service.assign(second, dict(host_id=beta, remote_root=r"F:\Beta", parallel=7))
        self.service.rename(alpha, "新电脑名称")

        reopened = Service(self.root / "state", ssh_factory=FakeSSH)
        for host, identifier, path, count in ((alpha, first, r"E:\Alpha", 4), (beta, second, r"F:\Beta", 7)):
            scoped = reopened.bootstrap(host_id=host)
            self.assertEqual([record["id"] for record in scoped["sessions"]], [identifier])
            self.assertEqual((scoped["session"]["host_id"], scoped["session"]["remote_root"], scoped["session"]["parallel"]),
                             (host, path, count))
        pending = reopened.bootstrap(host_id="")
        self.assertEqual([record["id"] for record in pending["sessions"]], [legacy["id"]])
        self.assertFalse(pending["session"]["archived"])
        with self.assertRaisesRegex(ValueError, "另一台电脑"):
            reopened.deploy(first, dict(host_id=beta))
        with patch.object(reopened, "_deploy"):
            started = reopened.deploy(first, {})
        self.assertEqual((started["host_id"], started["remote_root"], started["parallel"]), (alpha, r"E:\Alpha", 4))
        reopened.close()

    def test_two_computers_can_deploy_independently(self):
        hosts = [self.service.probe(dict(address=name))["id"] for name in ("alpha", "beta")]
        sessions = [self.service.create(dict(source_root=str(self.root)))["id"] for _ in hosts]
        entered = {identifier: threading.Event() for identifier in sessions}
        release = threading.Event()

        def deploy(identifier):
            entered[identifier].set()
            release.wait(2)

        with patch.object(self.service, "_deploy", side_effect=deploy) as run:
            try:
                for identifier, host in zip(sessions, hosts):
                    self.service.assign(identifier, {"host_id": host})
                    self.assertEqual(self.service.deploy(identifier, {})["status"], "deploying")
                for identifier in sessions:
                    self.assertTrue(entered[identifier].wait(2))
                    self.assertFalse(self.service.session(identifier)["can_delete"])
                deployments = {self.service.session(identifier)["deployment_id"] for identifier in sessions}
                self.assertEqual(len(deployments), 2)
                self.assertEqual(run.call_count, 2)
            finally:
                release.set()

    def test_archive_restore_and_delete_do_not_touch_remote_work(self):
        host = self.service.probe(dict(address="alpha"))["id"]
        identifier = self.service.create({})["id"]
        self.service.assign(identifier, dict(host_id=host))
        self.service.store.update("session", identifier, status="running", runtime_release=r"E:\runner")
        original_calls = list(FakeSSH.calls)

        archived = self.service.archive(identifier)
        self.assertTrue(archived["archived"])
        self.assertEqual(archived["status"], "running")
        self.assertFalse(archived["can_delete"])
        self.assertEqual(self.service.archive(identifier)["archived_unix"], archived["archived_unix"])
        self.assertEqual(self.service.bootstrap(host_id=host)["sessions"], [])
        self.assertEqual(self.service.bootstrap(identifier)["session"]["id"], identifier)
        self.assertEqual(self.service.bootstrap(host_id=host, archived=True)["sessions"][0]["id"], identifier)
        with patch.object(self.service, "start_observer") as recover_observer:
            self.service.recover()
            recover_observer.assert_called_once_with(identifier)
        with self.assertRaisesRegex(ValueError, "仍在运行"):
            self.service.delete(identifier)
        self.assertFalse(self.service.archive(identifier, archived=False)["archived"])
        self.service.store.update("session", identifier, status="failed", launch_attempted=True)
        self.assertFalse(self.service.session(identifier)["can_delete"])
        self.service.store.update("session", identifier, status="completed")
        stop = threading.Event()
        observer = Mock()
        observer.poll.return_value = None
        self.service.observer_stops[identifier] = stop
        self.service.observer_processes[identifier] = observer

        self.assertEqual(self.service.delete(identifier), dict(id=identifier, deleted=True))
        self.assertTrue(stop.is_set())
        observer.terminate.assert_called_once_with()
        self.assertEqual(FakeSSH.calls, original_calls)
        self.assertEqual(self.service.bootstrap(host_id=host)["sessions"], [])
        with self.assertRaises(KeyError):
            self.service.store.update("session", identifier, status="disconnected")
        self.service.observer_processes.clear()

    def test_remote_clock_offset_does_not_make_fresh_telemetry_stale(self):
        session = self.service.create({})
        self.service.store.update("session", session["id"], status="completed", last_observed_unix=time.time(),
            snapshot=dict(updated_unix=time.time() - 3600, stale=False, eta_seconds=0))
        self.assertFalse(self.service.session(session["id"])["snapshot"]["stale"])

    def test_shutdown_only_terminates_owned_observation_connections(self):
        observer = Mock()
        observer.poll.return_value = None
        self.service.observer_processes["owned-session"] = observer
        self.service.close()
        observer.terminate.assert_called_once_with()

    def test_browse_missing_folder_never_calls_create(self):
        host = self.service.probe(dict(address="windows-runner"))
        result = self.service.browse(host["id"], r"E:\尚未创建")
        self.assertFalse(result["exists"])
        self.assertNotIn("mkdir", FakeSSH.calls[-1][0])

    def test_path_and_ssh_argument_validation(self):
        for invalid in ("-oProxyCommand=bad", "host;echo", "a\nother"):
            with self.assertRaises(ValueError):
                profile(dict(address=invalid))
        for invalid in ("E:\\", "relative", "E:\\bad\npath"):
            with self.assertRaises(ValueError):
                windows_path(invalid)
        for invalid in ("../secret", "/secret", "C:/secret", "a\\secret"):
            with self.assertRaises(ValueError):
                safe_relative(invalid)

    def test_archive_is_reproducible_and_rejects_case_collisions(self):
        source = self.root / "source"
        source.mkdir()
        (source / "entry.py").write_text("pass\n")
        one = archive_files(source, [source / "entry.py"], self.root / "a.zip")
        two = archive_files(source, [source / "entry.py"], self.root / "b.zip")
        self.assertEqual(one["archive_sha256"], two["archive_sha256"])

    def test_binding_updates_child_sha_before_parent_and_keeps_source_unchanged(self):
        source = self.root / "source"
        (source / "tools").mkdir(parents=True)
        worker = source / "tools" / "worker.py"
        worker.write_text("pass\n")
        original = dict(results="control", budget_seconds=10, max_parallel=1, inputs=[dict(path="child.json", sha256="old")],
            tasks=[dict(id="coordinator", argv=["{python}", "{root}/tools/worker.py"], outputs=["done.json"], timeout_seconds=10)])
        child = dict(results="child-control", budget_seconds=10, inputs=[dict(path="tools/worker.py", sha256="old")],
            tasks=[dict(id="case", argv=["{python}", "{root}/tools/worker.py"], outputs=["done.json"], timeout_seconds=10)])
        (source / "manifest.json").write_text(json.dumps(original))
        (source / "child.json").write_text(json.dumps(child))
        before = digest(source / "manifest.json")
        options = dict(remote_root=r"E:\中文 文件夹", deployment_id="stable-id", parallel=36,
                       global_limit=18, python=DEFAULTS["python"], slot_root=DEFAULTS["slot_root"])
        request = dict(source_root=str(source), manifest_files=["child.json"], work_plan=dict(testing=10))
        result = prepare_package(request, options, self.root / "output", r"E:\runner\releases\abc")
        bound = self.root / "output" / "bound"
        parent = json.loads((bound / "manifest.json").read_text())
        sub = json.loads((bound / "child.json").read_text())
        self.assertEqual(parent["inputs"][0]["sha256"], digest(bound / "child.json"))
        self.assertEqual(sub["inputs"][0]["sha256"], digest(bound / "tools/worker.py"))
        self.assertEqual(parent["max_parallel"], 1)
        self.assertEqual(sub["max_parallel"], 18)
        self.assertEqual(parent["execution_quota"]["capacity"], 36)
        self.assertEqual(result["effective_parallel"], 18)
        self.assertEqual(digest(source / "manifest.json"), before)

    def test_failed_session_cannot_be_redeployed_implicitly(self):
        session = self.service.create({})
        self.service.store.update("session", session["id"], status="failed")
        with self.assertRaisesRegex(ValueError, "重复部署"):
            self.service.deploy(session["id"], {})

    def test_http_requires_token_and_rejects_cross_origin(self):
        server, token = make_server(self.service)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        url = "http://127.0.0.1:%d" % server.server_port
        try:
            with self.assertRaises(HTTPError) as error:
                urlopen(url + "/api/bootstrap")
            self.assertEqual(error.exception.code, 403)
            with urlopen(Request(url + "/api/bootstrap", headers={"X-Runner-Token": token})) as response:
                self.assertEqual(json.load(response)["defaults"]["max_supported"], 36)
            with self.assertRaises(HTTPError) as error:
                urlopen(Request(url + "/api/bootstrap", headers={"X-Runner-Token": token, "Origin": "https://untrusted.example"}))
            self.assertEqual(error.exception.code, 403)
        finally:
            server.shutdown()
            server.server_close()
            thread.join()

    def test_http_workspace_routes_and_legacy_unassigned_scope(self):
        host = self.service.probe(dict(address="alpha"))["id"]
        session = self.service.create({})["id"]
        server, token = make_server(self.service)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        url = "http://127.0.0.1:%d" % server.server_port
        route = "/api/sessions/" + session

        def api(path, data=None):
            body = json.dumps(data).encode() if data is not None else None
            request = Request(url + path, data=body, headers={"X-Runner-Token": token})
            with urlopen(request) as response:
                return json.load(response)

        try:
            self.assertEqual(api("/api/bootstrap?host_id=")["session"]["id"], session)
            self.assertEqual(api(route + "/assign", {"host_id": host})["host_id"], host)
            self.assertEqual(api("/api/bootstrap?host_id=")["sessions"], [])
            self.assertTrue(api(route + "/archive", {})["archived"])
            self.assertEqual(api("/api/bootstrap?archived=true&host_id=" + host)["session"]["id"], session)
            self.assertFalse(api(route + "/restore", {})["archived"])
            self.assertTrue(api(route + "/delete", {})["deleted"])
            with self.assertRaises(HTTPError) as error:
                api(route)
            self.assertEqual(error.exception.code, 404)
        finally:
            server.shutdown()
            server.server_close()
            thread.join()


if __name__ == "__main__":
    unittest.main()
