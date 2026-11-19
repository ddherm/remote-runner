"""Cross-process compute leases released by the OS after a worker crash."""
import contextlib
import errno
import os
from pathlib import Path
import time
import uuid

from native_runtime import atomic_json, load_json, process_birth

DEFAULT_CAPACITY = 18
MAX_CAPACITY = 36
_current = None


def current_slot():
    return _current


def lock_stream(stream):
    stream.seek(0)
    if os.name == "nt":
        import msvcrt
        msvcrt.locking(stream.fileno(), msvcrt.LK_NBLCK, 1)
    else:
        import fcntl
        fcntl.flock(stream.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)


@contextlib.contextmanager
def capacity_lock(directory, timeout=5):
    """Serialize policy changes and admission; closing releases the OS lock."""
    stream = (Path(directory) / ".machine-policy.lock").open("a+b")
    deadline = time.monotonic() + timeout
    try:
        while True:
            try:
                lock_stream(stream)
                break
            except OSError as error:
                if error.errno not in (errno.EACCES, errno.EAGAIN, errno.EDEADLK):
                    raise
                if time.monotonic() >= deadline:
                    raise TimeoutError("Machine capacity policy is being reviewed by another operation")
                time.sleep(0.025)
        yield
    finally:
        stream.close()


class ComputeSlot:
    def __init__(self, directory, identity, status_path, capacity=DEFAULT_CAPACITY,
                 waiting_status="waiting_slot", acquired_status="computing"):
        if not 1 <= capacity <= MAX_CAPACITY:
            raise ValueError("Compute capacity must be between 1 and %d" % MAX_CAPACITY)
        self.directory = Path(directory).resolve()
        self.directory.mkdir(parents=True, exist_ok=True)
        self.identity = identity
        self.status_path = Path(status_path)
        self.capacity = capacity
        self.stream = None
        self.index = None
        self.token = uuid.uuid4().hex
        self.waiting_status = waiting_status
        self.acquired_status = acquired_status
        policy = self.directory / "capacity.json"
        with capacity_lock(self.directory):
            try:
                with policy.open("x", encoding="utf-8") as stream:
                    import json
                    json.dump({"capacity": capacity}, stream)
            except FileExistsError:
                pass
            # Old releases may still be publishing their first few bytes.
            for retry in range(40):
                try:
                    actual = load_json(policy)["capacity"]
                    break
                except (OSError, ValueError):
                    if retry == 39:
                        raise
                    time.sleep(0.025)
        if actual != capacity:
            raise ValueError("Shared slot directory has a different capacity")

    def _lock(self, stream):
        lock_stream(stream)

    def acquire(self, timeout=3600):
        if self.stream is not None:
            raise RuntimeError("Compute slot already held")
        atomic_json(self.status_path, dict(self.identity, status=self.waiting_status,
                                         pid=os.getpid(), updated_unix=time.time()))
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            try:
                with capacity_lock(self.directory, timeout=0):
                    if load_json(self.directory / "capacity.json")["capacity"] != self.capacity:
                        raise ValueError("Shared slot directory has a different capacity")
                    for index in range(self.capacity):
                        path = self.directory / ("slot-%02d.lock" % index)
                        # Lock beyond EOF without writing into another owner's lock.
                        stream = path.open("a+b")
                        try:
                            self._lock(stream)
                        except OSError as error:
                            stream.close()
                            if error.errno not in (errno.EACCES, errno.EAGAIN, errno.EDEADLK):
                                raise
                            continue
                        self.stream, self.index = stream, index
                        owner = dict(self.identity, pid=os.getpid(), process_birth=process_birth(os.getpid()),
                                     token=self.token, slot=index, acquired_unix=time.time())
                        try:
                            atomic_json(self.directory / ("slot-%02d.json" % index), owner)
                            atomic_json(self.status_path, dict(owner, status=self.acquired_status, updated_unix=time.time()))
                        except BaseException:
                            self.release()
                            raise
                        return self
            except TimeoutError:
                pass
            time.sleep(0.025)
        raise TimeoutError("Timed out waiting for shared compute capacity")

    def release(self):
        if self.stream is None:
            return
        try:
            owner = self.directory / ("slot-%02d.json" % self.index)
            owner.unlink(missing_ok=True)
        finally:
            self.stream.close()
            self.stream, self.index = None, None
        atomic_json(self.status_path, dict(self.identity, status="idle", pid=os.getpid(), updated_unix=time.time()))

    @contextlib.contextmanager
    def suspended(self):
        """Pause a learner while its validation workers take independent slots."""
        held = self.stream is not None
        if held:
            self.release()
        try:
            yield
        finally:
            if held:
                self.acquire()

    def __enter__(self):
        global _current
        self.acquire()
        _current = self
        return self

    def __exit__(self, *args):
        global _current
        _current = None
        self.release()


class ComputeLease:
    """Acquire a batch permit before a machine slot, releasing both for children.

    All participants use the same acquisition order. A worker queued for its
    batch permit therefore never holds scarce machine capacity while waiting.
    """
    def __init__(self, directory, identity, status_path, capacity=DEFAULT_CAPACITY,
                 execution_quota=None):
        self.status_path = Path(status_path)
        self.identity = dict(identity)
        self.quota = None
        if execution_quota is not None:
            self.identity["deployment_id"] = execution_quota["id"]
            self.quota = ComputeSlot(execution_quota["root"], self.identity, self.status_path,
                                     execution_quota["capacity"], waiting_status="waiting_batch_slot",
                                     acquired_status="waiting_machine_slot")
        self.machine = ComputeSlot(directory, self.identity, self.status_path, capacity,
                                   waiting_status="waiting_machine_slot" if self.quota else "waiting_slot")

    @property
    def stream(self):
        return self.machine.stream

    @property
    def index(self):
        return self.machine.index

    def acquire(self, timeout=3600):
        if self.machine.stream is not None or (self.quota and self.quota.stream is not None):
            raise RuntimeError("Compute lease already held")
        deadline = time.monotonic() + timeout
        try:
            if self.quota is not None:
                self.quota.acquire(timeout=max(0, deadline - time.monotonic()))
            self.machine.acquire(timeout=max(0, deadline - time.monotonic()))
        except BaseException:
            self.release()
            raise
        return self

    def release(self):
        try:
            self.machine.release()
        finally:
            if self.quota is not None:
                self.quota.release()

    @contextlib.contextmanager
    def suspended(self):
        held = self.machine.stream is not None
        if held:
            self.release()
        try:
            yield
        finally:
            if held:
                self.acquire()

    def __enter__(self):
        global _current
        self.acquire()
        _current = self
        return self

    def __exit__(self, *args):
        global _current
        _current = None
        self.release()


def active_slots(directory):
    result = []
    for path in Path(directory).glob("slot-*.json"):
        try:
            row = load_json(path)
            if process_birth(row["pid"]) == row["process_birth"]:
                result.append(row)
        except (OSError, ValueError, KeyError):
            continue
    return result
