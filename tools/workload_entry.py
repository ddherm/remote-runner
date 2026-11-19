"""Run a Python script in its own process with an independently releasable slot."""
import os
from pathlib import Path
import runpy
import sys

from native_runtime import current_environment, install, load_json
from slots import ComputeLease


def main():
    install()
    spec_path = str(Path(sys.argv[1]).resolve())
    spec = load_json(spec_path)
    argv = spec["argv"]
    python = Path(spec["worker_python"]).resolve()
    if Path(argv[0]).resolve() != python or Path(sys.executable).resolve() != python:
        raise ValueError("Compute executable must match the configured worker")
    if current_environment() != spec["worker_environment"]:
        raise ValueError("Worker environment differs from the frozen task identity")
    args = argv[1:]
    while args and args[0] in ("-B", "-u", "-X"):
        option = args.pop(0)
        if option == "-X":
            if not args or args.pop(0) != "utf8":
                raise ValueError("Only -X utf8 is supported")
    if not args or args[0].startswith("-"):
        raise ValueError("Workload must name a Python script")
    entry = Path(args[0]).resolve()
    sys.argv = [str(entry)] + args[1:]
    sys.path.insert(0, str(entry.parent))
    os.environ["GTR_WORKER_PYTHON"] = str(python)
    os.environ["GTR_RUNTIME_SPEC"] = spec_path
    with ComputeLease(spec["slot_root"], spec["slot_identity"],
                      Path(spec["attempt"]) / "resource.json", spec["slot_capacity"],
                      spec.get("execution_quota")):
        runpy.run_path(str(entry), run_name="__main__")


if __name__ == "__main__":
    main()
