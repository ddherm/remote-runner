import sys
from pathlib import Path
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from initiator.progress import normalize_metrics, overall_progress, ProgressEstimator


class ProgressTests(unittest.TestCase):
    def test_training_validation_count_updates_only_as_detail(self):
        metrics = normalize_metrics(dict(completed_episodes=250, optimizer_steps=1000, validation_done=65),
                                    dict(episodes_total=500, updates_total=2000, validation_total=130))
        task = dict(id="train", metrics=metrics, parent_id=None)
        child = dict(id="child", parent_id="train", metrics=normalize_metrics(dict(validation_done=5, validation_total=5)))
        overall = overall_progress([task, child])
        self.assertEqual(overall["current"], 315)
        self.assertEqual(overall["total"], 630)
        self.assertEqual(overall["percent"], 50)

    def test_pure_test_and_unknown_generic(self):
        metrics = normalize_metrics(dict(tests_completed=25, tests_total=50))
        self.assertEqual([m["id"] for m in metrics], ["testing"])
        self.assertEqual(overall_progress([dict(id="test", metrics=metrics)])["percent"], 50)
        self.assertIsNone(overall_progress([dict(id="generic", metrics=[])])["percent"])

    def test_generic_explicit_metrics_and_weights(self):
        metrics = normalize_metrics(dict(metrics=[dict(id="files", label="文件", current=2, total=4, weight=2)]))
        result = overall_progress([dict(id="copy", metrics=metrics)])
        self.assertEqual((result["current"], result["total"]), (4, 8))

    def test_generic_phase_work_is_dynamic(self):
        metrics = normalize_metrics(dict(phases=[dict(id="compile", label="编译", current=3, total=6),
                                                dict(id="copy", metrics=[dict(id="files", current=4, total=8)])]))
        self.assertEqual([metric["id"] for metric in metrics], ["compile", "copy:files"])
        self.assertEqual(overall_progress([dict(id="generic", metrics=metrics)])["percent"], 50)

    def test_eta_requires_samples_and_excludes_inactive_intervals(self):
        estimator = ProgressEstimator()
        row = dict(id="one", attempt="a1", metrics=[dict(id="testing", current=0, total=20)])
        self.assertIsNone(estimator.update([row], 0, {"one": {"testing"}}))
        for index in (1, 2):
            row["metrics"][0]["current"] = index
            self.assertIsNone(estimator.update([row], index * 5, {"one": {"testing"}}))
        row["metrics"][0]["current"] = 3
        self.assertAlmostEqual(estimator.update([row], 15, {"one": {"testing"}}), 85)
        estimator.update([row], 20, {})
        row["metrics"][0]["current"] = 4
        estimator.update([row], 25, {"one": {"testing"}})
        self.assertEqual(len(next(iter(estimator.rates.values()))), 3)

    def test_eta_accumulates_time_between_long_episodes(self):
        estimator = ProgressEstimator()
        row = dict(id="train", metrics=[dict(id="training", current=0, total=10)])
        for step in range(13):
            row["metrics"][0]["current"] = step // 4
            result = estimator.update([row], step * 5, {"train": {"training"}})
        self.assertAlmostEqual(result, 140)


if __name__ == "__main__":
    unittest.main()
