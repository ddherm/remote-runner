"""Legacy bundle selection includes launcher code without local credentials."""
import json
from pathlib import Path
import sys
import tempfile
import unittest
import zipfile

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "tools"))
from bundle_sync import build, select_files


class BundleSelectionTests(unittest.TestCase):
    def test_sources_and_web_ship_without_local_store_or_cached_packages(self):
        with tempfile.TemporaryDirectory(prefix="gtr-bundle-selection-") as directory:
            root = Path(directory)
            wanted = {"tools/observer.py", "initiator/__init__.py", "initiator/progress.py",
                      "initiator/web/index.html", "initiator/web/app.js", "initiator/web/app.css",
                      "initiator/examples/engineering.json", "code/runtime/results/__init__.py"}
            private = {"initiator/state/runtime.json", "initiator/state/launcher.sqlite3",
                       "initiator/state/packages/session/task.zip", "initiator/state/policy_packages/runtime.zip",
                       "initiator/packages/runtime.zip", "initiator/policy_packages/runtime.zip",
                       "initiator/cache/sample.json", "initiator/.cache/sample.json",
                       "initiator/__pycache__/progress.pyc", "initiator/runtime.json",
                       "initiator/launcher.sqlite3", "initiator/launcher.sqlite3-wal",
                       "initiator/service.log", "initiator/daemon.lock", "runtime.json", "launcher.sqlite3"}
            for name in wanted | private:
                path = root / name
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text("fixture", encoding="utf-8")
            selected = {path.relative_to(root).as_posix() for path in select_files(root)}
            self.assertEqual(selected, wanted)
            output = root / "bundles/runtime.zip"
            build(root, output)
            with zipfile.ZipFile(output) as archive:
                self.assertEqual(set(archive.namelist()), wanted | {"bundle_manifest.json"})
                manifest = json.loads(archive.read("bundle_manifest.json"))
                self.assertEqual({row["path"] for row in manifest["files"]}, wanted)
            self.assertTrue((root / "initiator/state/runtime.json").exists())
            self.assertTrue((root / "initiator/state/launcher.sqlite3").exists())


if __name__ == "__main__":
    unittest.main(verbosity=2)
