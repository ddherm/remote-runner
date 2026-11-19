"""Scheduled pythonw entry with independent logs and owned descendant cleanup."""
import argparse
from pathlib import Path
import os
import sys
import time
import traceback


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--log-root", required=True)
    args, forwarded = parser.parse_known_args()
    logs = Path(args.log_root).resolve() / ("controller-%d" % time.time_ns())
    logs.mkdir(parents=True)
    sys.stdout = (logs / "stdout.log").open("x", encoding="utf-8", buffering=1)
    sys.stderr = (logs / "stderr.log").open("x", encoding="utf-8", buffering=1)
    try:
        from native_runtime import atomic_json, child_environment, install
        install()
        import runner
        job = runner.win_job_for_self() if os.name == "nt" else None
        atomic_json(logs / "startup.json", dict(pid=os.getpid(), coordinator=sys.executable,
                    console_handle=int(__import__('ctypes').windll.kernel32.GetConsoleWindow()) if os.name == "nt" else None))
        sys.argv = [str(Path(__file__).with_name("runner.py"))] + forwarded
        return runner.main()
    except BaseException:
        traceback.print_exc()
        return 2


if __name__ == "__main__":
    sys.exit(main())
