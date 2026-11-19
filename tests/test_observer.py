import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "tools"))
from observer import Observer, read_log, browse_directories
from monitor import collect_snapshot, ResourceSampler, gpu_metrics


class FakeSampler:
    def sample(self):
        return dict(cpu_percent=None, memory_percent=None, gpus=[])


class ObserverTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)

    def tearDown(self):
        self.temporary.cleanup()

    def write(self, path, value):
        path = self.root / path
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(value), encoding="utf-8")
        return path

    def test_json_snapshot_reads_latest_progress(self):
        attempt = self.root / "group/runs/job/a0001_abc123"
        self.write("group/runs/job/a0001_abc123/train/progress.json", dict(completed_episodes=9))
        state = dict(tasks=[dict(id="job", attempt=str(attempt), progress_file="train/progress.json",
                                progress=dict(completed_episodes=1))])
        result = collect_snapshot(state, self.root, FakeSampler())
        self.assertEqual(result["state"]["tasks"][0]["progress"]["completed_episodes"], 9)

    def test_legacy_validation_is_live_without_parent_mutation(self):
        self.write("manifest.json", dict(results="control", tasks=[dict(id="train", workspace="g",
                   progress_file="train/progress.json", metadata=dict(episodes_total=500, validation_total=130))]))
        attempt = Path("g/runs/train/a0001_abc123")
        self.write(attempt / "train/progress.json", dict(phase="validation", status="running",
                   completed_episodes=20, validation_done=5, validation_episode=20, validation_environment_done=0))
        validation = attempt / "train/v/e020"
        child_tasks = [dict(id="seed%d" % index, workspace=str(validation), progress_file="case/progress.json",
                            metadata=dict(phase="validation", environment_seed=index)) for index in range(5)]
        self.write(validation / "manifest.json", dict(tasks=child_tasks))
        for index in range(3):
            self.write(validation / ("runs/seed%d/complete.json" % index), dict(attempt="a0001_child1"))
        before = (self.root / attempt / "train/progress.json").read_bytes()
        observer = Observer(self.root, sampler=FakeSampler())
        result = observer.snapshot()
        self.assertEqual(result["tasks"][0]["metrics"][1]["current"], 8)
        self.assertEqual(result["overall"]["current"], 28)
        self.assertEqual((self.root / attempt / "train/progress.json").read_bytes(), before)
        self.assertEqual(len(result["tasks"]), 6)

    def test_registered_children_and_pure_test(self):
        self.write("manifest.json", dict(tasks=[dict(id="J", workspace="control", progress_file="testing/progress.json",
                                                      metadata=dict(tests_total=50))]))
        attempt = Path("control/runs/J/a0001_abc123")
        self.write(attempt / "testing/progress.json", dict(tests_completed=25, phase="testing"))
        self.write(attempt / "children/relation.json", dict(child=dict(root=str(self.root), manifest="child.json")))
        self.write("child.json", dict(tasks=[dict(id="case", workspace="cases", metadata=dict(phase="testing"))]))
        result = Observer(self.root, sampler=FakeSampler()).snapshot()
        self.assertEqual(result["overall"]["percent"], 50)
        self.assertEqual(result["tasks"][1]["parent_id"], "J")
        self.assertEqual([m["id"] for m in result["tasks"][0]["metrics"]], ["testing"])
        self.assertTrue(result["tasks"][0]["stdout"].endswith("stdout.log"))

    def test_other_batches_count_globally_but_do_not_appear_in_current_workers(self):
        self.write("manifest.json", dict(slot_root=str(self.root / "slots"), tasks=[]))
        leases = [dict(root=str(self.root), task_id="ours", slot=1),
                  dict(root=str(self.root.parent / "other-batch"), task_id="other", slot=2)]
        with mock.patch("observer.active_slots", return_value=leases):
            result = Observer(self.root, sampler=FakeSampler()).snapshot()
        self.assertEqual(result["global_used"], 2)
        self.assertEqual(len(result["machine_workers"]), 2)
        self.assertEqual([worker["task_id"] for worker in result["active_workers"]], ["ours"])

    def test_single_root_work_plan_declares_denominator_without_inventing_progress(self):
        self.write("manifest.json", dict(work_plan=dict(training=500, validation=130),
                                        tasks=[dict(id="train", workspace="g")]))
        result = Observer(self.root, sampler=FakeSampler()).snapshot()
        self.assertEqual(result["overall"]["total"], 630)
        self.assertEqual(result["overall"]["current"], 0)
        self.assertEqual(result["status"], "waiting")
        self.assertEqual(result["warnings"], ["尚未读取到控制器状态，请检查启动记录和日志"])

    def test_per_task_work_plan_and_generic_declared_metrics(self):
        self.write("manifest.json", dict(work_plan=dict(tasks={
            "first": dict(testing=50),
            "second": dict(metrics=[dict(id="files", label="文件", current=999, total=10)])}),
            tasks=[dict(id="first", workspace="a"), dict(id="second", workspace="b")]))
        self.write("a/runs/first/a0001_abcdef/progress.json", dict(tests_completed=25))
        self.write("b/runs/second/a0001_abcdef/progress.json", dict(metrics=[dict(id="files", current=4)]))
        result = Observer(self.root, sampler=FakeSampler()).snapshot()
        self.assertEqual(result["overall"]["total"], 60)
        self.assertEqual(result["overall"]["current"], 29)

    def test_generic_work_plan_current_is_never_completion_evidence(self):
        self.write("manifest.json", dict(work_plan=dict(tasks={
            "generic": dict(metrics=[dict(id="files", current=8, total=10)])}), tasks=[dict(id="generic")]))
        result = Observer(self.root, sampler=FakeSampler()).snapshot()
        self.assertEqual(result["overall"]["total"], 10)
        self.assertEqual(result["overall"]["current"], 0)

    def test_verified_single_case_receipt_counts_without_worker_counter(self):
        tasks = [dict(id="verified", workspace="one", metadata=dict(phase="testing", tests_total=1)),
                 dict(id="unverified", workspace="two", metadata=dict(phase="validation", validation_total=1)),
                 dict(id="training", workspace="three", metadata=dict(phase="training", episodes_total=500)),
                 dict(id="batch", workspace="four", metadata=dict(phase="validation", validation_total=5)),
                 dict(id="bare", workspace="five", metadata=dict(phase="testing"))]
        self.write("manifest.json", dict(tasks=tasks))
        for task in tasks:
            directory = Path(task["workspace"]) / "runs" / task["id"]
            self.write(directory / "a0001_abcdef/progress.json", dict(status="complete"))
            if task["id"] not in ("unverified", "bare"):
                self.write(directory / "complete.json", dict(attempt="a0001_abcdef"))
        result = Observer(self.root, sampler=FakeSampler()).snapshot()
        values = {row["native_task_id"]: row["metrics"][0]["current"] for row in result["tasks"]}
        self.assertEqual(values, dict(verified=1, unverified=0, training=0, batch=0, bare=0))

    def test_short_work_plan_is_not_assigned_to_multiple_roots(self):
        self.write("manifest.json", dict(work_plan=dict(testing=50), tasks=[dict(id="first"), dict(id="second")]))
        result = Observer(self.root, sampler=FakeSampler()).snapshot()
        self.assertIsNone(result["overall"]["percent"])

    def test_incremental_utf8_and_truncated_logs(self):
        path = self.root / "stdout.log"
        path.write_bytes("abcd中文末尾".encode("utf-8"))
        first = read_log(path, max_bytes=8)
        second = read_log(path, offset=first["offset"])
        self.assertEqual(first["text"] + second["text"], "abcd中文末尾")
        path.write_bytes(b"new")
        reset = read_log(path, second["offset"])
        self.assertTrue(reset["reset"])
        self.assertEqual(reset["text"], "new")

    def test_directory_browser_and_gpu_fallback(self):
        (self.root / "folder").mkdir()
        (self.root / "file").write_text("x")
        self.assertEqual([row["name"] for row in browse_directories(self.root)["directories"]], ["folder"])
        with mock.patch("monitor.subprocess.run", side_effect=FileNotFoundError()):
            self.assertEqual(gpu_metrics(), [])

    def test_sampler_preserves_cpu_baseline_between_frames(self):
        with mock.patch("monitor.metrics", side_effect=[({"cpu_percent": None}, "baseline"),
                                                        ({"cpu_percent": 42}, "next")]) as read:
            with mock.patch("monitor.gpu_metrics", return_value=[]):
                sampler = ResourceSampler()
                self.assertIsNone(sampler.sample()["cpu_percent"])
                self.assertEqual(sampler.sample()["cpu_percent"], 42)
                self.assertEqual(read.call_args_list[1].args, ("baseline",))


if __name__ == "__main__":
    unittest.main()
