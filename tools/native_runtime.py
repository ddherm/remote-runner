"""Native Windows fixes shared by controllers, workers and passive readers."""
import ctypes
import hashlib
import json
import os
from pathlib import Path
import platform
import subprocess
import sys
import time
import uuid

VERSION = "1.3.0"
THREAD_ENV = {name: "1" for name in (
    "OMP_NUM_THREADS", "MKL_NUM_THREADS", "OPENBLAS_NUM_THREADS", "NUMEXPR_NUM_THREADS")}


def replace_with_retry(original, source, target, *args, **kwargs):
    deadline = time.monotonic() + 5.0
    while True:
        try:
            return original(source, target, *args, **kwargs)
        except PermissionError as error:
            if getattr(error, "winerror", None) not in (5, 32, 33) or time.monotonic() >= deadline:
                raise
            time.sleep(0.025)


def install():
    """Retain E's verified callable-object fix for Python 3.9 pathlib."""
    if os.name != "nt" or getattr(sys, "_gtr_public_runtime", False):
        return
    original_replace = os.replace

    class AtomicReplace:
        def __call__(self, source, target, *args, **kwargs):
            return replace_with_retry(original_replace, source, target, *args, **kwargs)

    replacement = AtomicReplace()
    os.replace = replacement
    accessor = getattr(sys.modules.get("pathlib"), "_normal_accessor", None)
    if accessor is not None:
        accessor.replace = replacement

    original_popen = subprocess.Popen

    class ComputePopen(original_popen):
        def __init__(self, *args, **kwargs):
            flags = kwargs.get("creationflags", 0)
            if flags & subprocess.CREATE_NEW_CONSOLE:
                raise ValueError("Compute subprocess requested a new console")
            kwargs["creationflags"] = flags | subprocess.CREATE_NO_WINDOW
            super().__init__(*args, **kwargs)

    subprocess.Popen = ComputePopen

    def unicode_ver(system="", release="", version="", supported_platforms=("win32", "win16", "dos")):
        if sys.platform not in supported_platforms:
            return system, release, version
        command = os.environ.get("COMSPEC", os.path.join(os.environ["SystemRoot"], "System32", "cmd.exe"))
        text = subprocess.check_output([command, "/d", "/u", "/c", "ver"],
                                       encoding="utf-16-le", stderr=subprocess.DEVNULL)
        match = platform._ver_output.match(text.strip())
        if match is None:
            raise RuntimeError("Cannot parse Unicode Windows version")
        system, release, version = match.groups()
        return system, release.rstrip("."), platform._norm_version(version.rstrip("."))

    platform._syscmd_ver = unicode_ver
    platform._platform_cache.clear()
    platform._uname_cache = None
    sys._gtr_public_runtime = True


def atomic_json(path, value):
    path = Path(path)
    temporary = path.with_name(".tmp-" + uuid.uuid4().hex)
    try:
        with temporary.open("x", encoding="utf-8", newline="\n") as stream:
            json.dump(value, stream, ensure_ascii=False, sort_keys=True, indent=2, allow_nan=False)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        replace_with_retry(os.replace, str(temporary), str(path))
    finally:
        if temporary.exists():
            temporary.unlink()


def shared_read(path):
    """Open with FILE_SHARE_DELETE so monitoring cannot block publication."""
    if os.name != "nt":
        return Path(path).read_bytes()
    import msvcrt
    from ctypes import wintypes
    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel.CreateFileW.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD,
                                  ctypes.c_void_p, wintypes.DWORD, wintypes.DWORD, wintypes.HANDLE]
    kernel.CreateFileW.restype = wintypes.HANDLE
    kernel.CloseHandle.argtypes = [wintypes.HANDLE]
    handle = kernel.CreateFileW(str(Path(path).resolve()), 0x80000000, 1 | 2 | 4,
                                None, 3, 0x80, None)
    if handle == ctypes.c_void_p(-1).value:
        raise ctypes.WinError(ctypes.get_last_error())
    try:
        descriptor = msvcrt.open_osfhandle(handle, os.O_RDONLY | os.O_BINARY)
    except BaseException:
        kernel.CloseHandle(handle)
        raise
    with os.fdopen(descriptor, "rb") as stream:
        return stream.read()


def load_json(path):
    return json.loads(shared_read(path).decode("utf-8-sig"))


def child_environment(extra=None, attempt=None):
    env = os.environ.copy()
    env.update(THREAD_ENV)
    env.update(extra or {})
    env.update(PYTHONUTF8="1", PYTHONIOENCODING="utf-8", PYTHONDONTWRITEBYTECODE="1")
    env["GTR_COMPUTE_BOOTSTRAP"] = "1"
    tools = str(Path(__file__).resolve().parent)
    env["PYTHONPATH"] = tools + (os.pathsep + env["PYTHONPATH"] if env.get("PYTHONPATH") else "")
    if attempt is not None:
        cache = Path(attempt) / "tmp"
        cache.mkdir(exist_ok=True)
        env.update(TEMP=str(cache), TMP=str(cache), TMPDIR=str(cache), MPLCONFIGDIR=str(cache / "mpl"))
    return env


def worker_environment(python, extra=None):
    """Probe the configured worker, independently of a pythonw controller."""
    result = subprocess.run([str(python), "-X", "utf8", "-B", str(Path(__file__).resolve())],
                            env=child_environment(extra), stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                            encoding="utf-8", timeout=30, check=True)
    info = json.loads(result.stdout.strip().splitlines()[-1])
    if os.path.normcase(info["python_executable"]) != os.path.normcase(os.path.realpath(python)):
        raise ValueError("Configured worker executable differs from probed executable")
    return info


def current_environment():
    import importlib.metadata
    versions = {}
    for name in ("numpy", "torch", "torch-geometric"):
        try:
            versions[name] = importlib.metadata.version(name)
        except importlib.metadata.PackageNotFoundError:
            pass
    executable = os.path.realpath(sys.executable)
    return dict(python_executable=executable, python_version=sys.version,
                platform=platform.platform(), dependencies=versions,
                executable_sha256=hashlib.sha256(Path(executable).read_bytes()).hexdigest())


def process_birth(pid):
    if os.name != "nt":
        try:
            os.kill(pid, 0)
            return "alive"
        except ProcessLookupError:
            return None
    from ctypes import wintypes as w
    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel.OpenProcess.argtypes = [w.DWORD, w.BOOL, w.DWORD]
    kernel.OpenProcess.restype = w.HANDLE
    kernel.GetProcessTimes.argtypes = [w.HANDLE] + [ctypes.POINTER(w.FILETIME)] * 4
    kernel.CloseHandle.argtypes = [w.HANDLE]
    handle = kernel.OpenProcess(0x1000, False, int(pid))
    if not handle:
        return None
    try:
        values = [w.FILETIME() for _ in range(4)]
        if not kernel.GetProcessTimes(handle, *(ctypes.byref(v) for v in values)):
            return None
        return str((values[0].dwHighDateTime << 32) | values[0].dwLowDateTime)
    finally:
        kernel.CloseHandle(handle)


if __name__ == "__main__":
    install()
    print(json.dumps(current_environment(), ensure_ascii=False))
