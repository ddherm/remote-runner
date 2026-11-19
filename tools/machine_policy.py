#!/usr/bin/env python3
"""Review or activate machine capacity at an idle native-Windows handover."""
import argparse
import base64
import ctypes
import hashlib
import json
import math
import os
from pathlib import Path
import re
import subprocess
import sys
import time
import uuid

from native_runtime import atomic_json, install, load_json, shared_read
from slots import MAX_CAPACITY, capacity_lock, lock_stream

WINDOWS = os.name == "nt"


class PolicyBlocked(RuntimeError):
    pass


PROCESS_QUERY = r'''
$ErrorActionPreference = 'Stop'
[Console]::OutputEncoding = New-Object System.Text.UTF8Encoding
$currentUser = [Security.Principal.WindowsIdentity]::GetCurrent().Name
$rows = @(); $uncertain = @()
$processes = @(Get-CimInstance -ClassName Win32_Process -ErrorAction Stop)
foreach ($process in $processes) {
    $command = [string]$process.CommandLine
    $candidate = $process.Name -match '^(python(w)?([0-9]+(\.[0-9]+)*)?|powershell|pwsh|cmd)(\.exe)?$'
    $candidate = $candidate -or $command -match '(runner\.py|controller_entry\.py|workload_entry\.py|launch_windows\.ps1)'
    if (-not $candidate) { continue }
    try {
        $owner = Invoke-CimMethod -InputObject $process -MethodName GetOwner -ErrorAction Stop
        if ($owner.ReturnValue -ne 0) { throw ('Owner lookup returned ' + $owner.ReturnValue) }
        $ownerName = $owner.Domain + '\' + $owner.User
        if ($ownerName -ine $currentUser) { continue }
        if ([string]::IsNullOrWhiteSpace($command)) { throw 'CommandLine is unavailable for a current-user candidate' }
        $rows += @{pid=[int]$process.ProcessId;name=[string]$process.Name;owner=$ownerName;commandline=$command}
    } catch {
        # A process can disappear during enumeration. A surviving inaccessible
        # candidate cannot safely be classified as unrelated work.
        $remaining = Get-CimInstance -ClassName Win32_Process -Filter ('ProcessId=' + $process.ProcessId) -ErrorAction Stop
        if ($remaining) { $uncertain += @{pid=[int]$process.ProcessId;reason=$_.Exception.Message} }
    }
}
@{schema=1;current_user=$currentUser;complete=($uncertain.Count -eq 0);processes=@($rows);uncertain=@($uncertain)} | ConvertTo-Json -Depth 6 -Compress
'''


def scan_windows_processes():
    if not WINDOWS:
        raise PolicyBlocked("Process ownership checks require native Windows")
    command = base64.b64encode(PROCESS_QUERY.encode("utf-16-le")).decode("ascii")
    result = subprocess.run(["powershell.exe", "-NoProfile", "-NonInteractive", "-EncodedCommand", command],
                            capture_output=True, encoding="utf-8", errors="replace", timeout=30)
    if result.returncode:
        raise PolicyBlocked("Cannot verify Windows process ownership: " + result.stderr[-2000:])
    try:
        report = json.loads(result.stdout.strip().splitlines()[-1])
    except (ValueError, IndexError) as error:
        raise PolicyBlocked("Windows process inspection returned no verifiable report") from error
    return report


def checked_process_birth(pid):
    """Distinguish an exited PID from an inaccessible live owner."""
    from ctypes import wintypes as w
    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel.OpenProcess.argtypes = [w.DWORD, w.BOOL, w.DWORD]
    kernel.OpenProcess.restype = w.HANDLE
    kernel.WaitForSingleObject.argtypes = [w.HANDLE, w.DWORD]
    kernel.WaitForSingleObject.restype = w.DWORD
    kernel.GetProcessTimes.argtypes = [w.HANDLE] + [ctypes.POINTER(w.FILETIME)] * 4
    kernel.GetProcessTimes.restype = w.BOOL
    kernel.CloseHandle.argtypes = [w.HANDLE]
    handle = kernel.OpenProcess(0x1000 | 0x100000, False, pid)
    if not handle:
        code = ctypes.get_last_error()
        if code in (87, 1168):
            return None
        raise PolicyBlocked("Cannot inspect recorded owner PID %d (Windows error %d)" % (pid, code))
    try:
        wait = kernel.WaitForSingleObject(handle, 0)
        if wait == 0:
            return None
        if wait != 258:
            raise PolicyBlocked("Cannot establish whether recorded PID %d has exited" % pid)
        values = [w.FILETIME() for _ in range(4)]
        if not kernel.GetProcessTimes(handle, *(ctypes.byref(value) for value in values)):
            raise PolicyBlocked("Cannot read recorded owner PID %d creation time" % pid)
        return str((values[0].dwHighDateTime << 32) | values[0].dwLowDateTime)
    finally:
        kernel.CloseHandle(handle)


def is_dispatcher(commandline):
    boundary = r"(?:^|[\\/\s\"'])"
    tail = r"(?=$|[\s\"'])"
    if re.search(boundary + r"(?:controller_entry\.py|workload_entry\.py)" + tail,
                 commandline, re.IGNORECASE):
        return True
    if re.search(boundary + r"launch_windows\.ps1" + tail, commandline, re.IGNORECASE):
        # The persistent Windows desktop monitor uses this same launcher, but
        # -ShowMonitor/-MonitorOnly never dispatch scientific work.
        passive = re.search(r"(?:^|[\s\"'])-(?:ShowMonitor|MonitorOnly)(?=$|[\s\"'])", commandline, re.IGNORECASE)
        return passive is None
    runner = re.search(boundary + r"runner\.py" + tail, commandline, re.IGNORECASE)
    executing = re.search(r"(?:^|[\s\"'])--(?:execute|worker)(?=$|[\s\"'])", commandline, re.IGNORECASE)
    return bool(runner and executing)


def review_processes(scanner):
    try:
        report = scanner()
    except Exception as error:
        raise PolicyBlocked("Windows process census could not be verified: " + str(error)) from error
    if (not isinstance(report, dict) or report.get("schema") != 1 or report.get("complete") is not True
            or not report.get("current_user") or report.get("uncertain")
            or not isinstance(report.get("processes"), list)):
        raise PolicyBlocked("Windows process census is incomplete or contains inaccessible candidates")
    matches = []
    for row in report["processes"]:
        if (not isinstance(row, dict) or not isinstance(row.get("pid"), int) or row["pid"] <= 0
                or not isinstance(row.get("commandline"), str) or not row["commandline"].strip()
                or not isinstance(row.get("owner"), str)
                or row["owner"].casefold() != report["current_user"].casefold()):
            raise PolicyBlocked("Process census contains an unverified ownership or command line")
        if is_dispatcher(row["commandline"]):
            matches.append(dict(pid=row["pid"], name=row.get("name"), commandline=row["commandline"]))
    if matches:
        raise PolicyBlocked("Computing, waiting or dispatching processes are still present: " +
                            json.dumps(matches, ensure_ascii=False))
    return dict(current_user=report["current_user"], inspected_candidates=len(report["processes"]),
                checked_unix=time.time())


def review_owners(root, birth_lookup):
    reviewed = []
    for path in sorted(root.glob("slot-*.json")):
        try:
            raw = shared_read(path)
            owner = json.loads(raw.decode("utf-8-sig"))
        except (OSError, ValueError) as error:
            raise PolicyBlocked("Unverifiable slot owner record: " + str(path)) from error
        match = re.fullmatch(r"slot-([0-9]+)\.json", path.name)
        if (not isinstance(owner, dict) or match is None or not isinstance(owner.get("pid"), int)
                or isinstance(owner["pid"], bool) or owner["pid"] <= 0
                or not isinstance(owner.get("process_birth"), str) or not owner["process_birth"].isdigit()
                or not isinstance(owner.get("token"), str) or not owner["token"]
                or not isinstance(owner.get("slot"), int) or owner["slot"] != int(match.group(1))
                or not 0 <= owner["slot"] < MAX_CAPACITY
                or not isinstance(owner.get("acquired_unix"), (int, float))
                or not math.isfinite(owner["acquired_unix"])):
            raise PolicyBlocked("Incomplete or unknown slot owner record: " + str(path))
        try:
            actual = birth_lookup(owner["pid"])
        except Exception as error:
            raise PolicyBlocked("Cannot verify recorded slot owner: " + str(path) + ": " + str(error)) from error
        if actual == owner["process_birth"]:
            raise PolicyBlocked("A recorded compute owner is still alive: " + str(path))
        reviewed.append(dict(path=str(path), sha256=hashlib.sha256(raw).hexdigest(), pid=owner["pid"],
                             recorded_birth=owner["process_birth"], observed_birth=actual,
                             status="exited" if actual is None else "pid_reused"))
    # An owned lock with no JSON is an unknown writer, rather than an idle slot.
    for path in sorted(root.glob("slot-*.lock")):
        try:
            with path.open("r+b") as stream:
                lock_stream(stream)
        except OSError as error:
            raise PolicyBlocked("A slot lock is still owned or inaccessible: " + str(path)) from error
    return reviewed


def read_policy(root):
    path = root / "capacity.json"
    if not path.exists():
        return None, None
    try:
        raw = shared_read(path)
        value = json.loads(raw.decode("utf-8-sig"))
    except (OSError, ValueError) as error:
        raise PolicyBlocked("Existing machine policy cannot be verified") from error
    capacity = value.get("capacity") if isinstance(value, dict) else None
    if isinstance(capacity, bool) or not isinstance(capacity, int) or not 1 <= capacity <= MAX_CAPACITY:
        raise PolicyBlocked("Existing machine policy has an unknown capacity")
    return capacity, hashlib.sha256(raw).hexdigest()


def change_policy(slot_root, capacity, execute=False, process_scanner=None, birth_lookup=None):
    if isinstance(capacity, bool) or not isinstance(capacity, int) or not 1 <= capacity <= MAX_CAPACITY:
        raise ValueError("Machine capacity must be 1..%d" % MAX_CAPACITY)
    root = Path(slot_root).resolve()
    previous, digest = read_policy(root)
    result = dict(schema=1, execute=bool(execute), slot_root=str(root), previous_capacity=previous,
                  requested_capacity=capacity, maximum_supported_capacity=MAX_CAPACITY,
                  previous_policy_sha256=digest, status="plan", requires_idle_native_windows=True)
    if not execute:
        return result
    if not WINDOWS:
        raise PolicyBlocked("Applying machine capacity requires native Windows; this command only plans on this platform")
    install()
    root.mkdir(parents=True, exist_ok=True)
    scanner = process_scanner or scan_windows_processes
    lookup = birth_lookup or checked_process_birth
    with capacity_lock(root, timeout=0):
        previous, digest = read_policy(root)
        result.update(previous_capacity=previous, previous_policy_sha256=digest)
        if previous == capacity:
            return dict(result, status="unchanged")
        census_before = review_processes(scanner)
        owners = review_owners(root, lookup)
        census_after = review_processes(scanner)
        # No immutable owner evidence is deleted. Refuse concurrent changes,
        # including ones originating in a frozen release without this gate.
        if review_owners(root, lookup) != owners or read_policy(root) != (previous, digest):
            raise PolicyBlocked("Machine ownership or policy changed during handover review")
        receipt_id = time.strftime("%Y%m%dT%H%M%SZ", time.gmtime()) + "-" + uuid.uuid4().hex[:12]
        destination = root / "policy_handover" / (receipt_id + ".json")
        destination.parent.mkdir(parents=True, exist_ok=True)
        receipt = dict(result, status="prepared", receipt_id=receipt_id, actor_pid=os.getpid(),
                       process_census_before=census_before, process_census_after=census_after,
                       preserved_owners=owners, prepared_unix=time.time())
        atomic_json(destination, receipt)
        atomic_json(root / "capacity.json", dict(capacity=capacity, handover_receipt=str(destination)))
        receipt.update(status="applied", applied_unix=time.time(), new_policy_sha256=read_policy(root)[1])
        atomic_json(destination, receipt)
        return dict(receipt, receipt=str(destination))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--slot-root", required=True)
    parser.add_argument("--capacity", required=True, type=int)
    parser.add_argument("--execute", action="store_true")
    args = parser.parse_args()
    try:
        result = change_policy(args.slot_root, args.capacity, args.execute)
    except (ValueError, OSError, RuntimeError) as error:
        print(json.dumps(dict(status="blocked", error=str(error)), ensure_ascii=False))
        return 2
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
