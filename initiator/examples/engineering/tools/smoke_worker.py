"""Engineering-only sleep/JSON helpers. No simulator, model or scientific data."""
import argparse
import hashlib
import os
from pathlib import Path
import time

from native_runtime import atomic_json, load_json
from runner_api import run_cases


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--group", default="helper")
    parser.add_argument("--case", action="store_true")
    args = parser.parse_args()
    out = args.output
    out.mkdir(parents=True, exist_ok=True)
    if args.case:
        for step in range(9):
            atomic_json(out / "progress.json", dict(status="running", phase="validation",
                metrics=[dict(id="case_steps", label="工程步骤", current=step, total=8, unit="步", contributes_to_overall=False)]))
            print("工程验证步骤 %d / 8" % step, flush=True)
            time.sleep(1)
        atomic_json(out / "completion.json", dict(status="complete", engineering_helper=True))
        atomic_json(out / "progress.json", dict(status="complete", phase="validation"))
        return
    spec = load_json(os.environ["GTR_RUNTIME_SPEC"])
    root = Path(spec["root"])
    for episode in range(4):
        atomic_json(out / "progress.json", dict(status="running", phase="training", completed_episodes=episode,
            target_episodes=3, validation_done=0, validation_total=4, updated_unix=time.time()))
        print("%s 工程训练步骤 %d / 3" % (args.group, episode), flush=True)
        time.sleep(1)
    atomic_json(out / "progress.json", dict(status="running", phase="validation", completed_episodes=3,
        target_episodes=3, validation_done=0, validation_total=4, validation_episode=3,
        validation_environment_done=0, updated_unix=time.time()))
    folder = out / "v" / "e3"
    folder.mkdir(parents=True)
    entry = "tools/smoke_worker.py"
    tasks = [dict(id=args.group + "-v%d" % i, workspace="helpers/" + args.group + "/cases",
        argv=["{python}", "{root}/" + entry, "--case", "--output", "{attempt}/case"],
        timeout_seconds=60, outputs=["case/completion.json"], progress_file="case/progress.json",
        metadata=dict(phase="validation", group=args.group)) for i in range(4)]
    manifest = dict(results=folder.relative_to(root).as_posix() + "/control", budget_seconds=120,
        slot_root=spec["slot_root"], slot_capacity=spec["slot_capacity"], env=spec["env"],
        inputs=[dict(path=entry, sha256=hashlib.sha256((root / entry).read_bytes()).hexdigest())], tasks=tasks)
    atomic_json(folder / "manifest.json", manifest)
    run_cases(folder / "manifest.json", root=root, python=spec["worker_python"])
    atomic_json(out / "completion.json", dict(status="complete", engineering_helper=True))
    atomic_json(out / "progress.json", dict(status="complete", phase="complete", completed_episodes=3,
        target_episodes=3, validation_done=4, validation_total=4, updated_unix=time.time()))


if __name__ == "__main__":
    main()
