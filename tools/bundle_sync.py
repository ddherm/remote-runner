#!/usr/bin/env python3
"""Immutable native-Windows ZIP deployment; upload/pull default to plan only."""
import argparse
import hashlib
import gzip
import json
from pathlib import Path, PurePosixPath
import base64
import os
import re
import subprocess
import sys
import time
import uuid
import zipfile

sys.dont_write_bytecode = True
from runner import atomic_json, inside, load, sha

EXCLUDE = {"__pycache__", ".git", "test_artifacts", "cache", "results", "bundles", "sync", "releases"}
INITIATOR_LOCAL_DIRS = {"state", "packages", "policy_packages", ".cache", "node_modules", "screenshots"}
LOCAL_STATE_FILES = {"runtime.json", "runtime.json.tmp", "service.log", "daemon.lock", ".DS_Store"}


def initiator_local_artifact(path, parts):
    """Keep launcher sources and assets, never its host/session/token store."""
    return (any(part in INITIATOR_LOCAL_DIRS for part in parts[1:])
            or path.name in LOCAL_STATE_FILES or path.name.startswith("launcher.sqlite3")
            or path.suffix.lower() in {".sqlite", ".sqlite3", ".db", ".log", ".lock"}
            or path.name.startswith(".tmp-"))


DEFAULT_ROOT = r"C:\RemoteRunner\runtime"
DEFAULT_PYTHON = "python.exe"


def ps_literal(text):
    return "'" + text.replace("'", "''") + "'"


def ssh_ps(host, script):
    if not re.fullmatch(r"[A-Za-z0-9_.@:-]+", host) or host.startswith("-"):
        raise ValueError("Unsafe SSH host argument")
    encoded = base64.b64encode(script.encode("utf-16-le")).decode("ascii")
    if len(encoded) > 7000:
        packed = base64.b64encode(gzip.compress(script.encode("utf-8"), mtime=0)).decode("ascii")
        loader = ("$m=[IO.MemoryStream]::new([Convert]::FromBase64String('" + packed + "'));"
                  "$g=[IO.Compression.GZipStream]::new($m,[IO.Compression.CompressionMode]::Decompress);"
                  "$r=[IO.StreamReader]::new($g,[Text.Encoding]::UTF8);"
                  "try{&([scriptblock]::Create($r.ReadToEnd()))}finally{$r.Dispose();$g.Dispose();$m.Dispose()}")
        encoded = base64.b64encode(loader.encode("utf-16-le")).decode("ascii")
    if len(encoded) > 7400:
        raise ValueError("SSH script exceeds the Windows command limit; stage code/data as files")
    return ["ssh", "-o", "BatchMode=yes", "-o", "ConnectTimeout=10", host,
            "powershell.exe -NoProfile -NonInteractive -EncodedCommand " + encoded]


def python_ps(python, code, argv):
    # PowerShell 5.1 rewrites quotes in native argv. One ASCII payload preserves
    # scripts and structured arguments, including JSON, empty strings and CRLF.
    data = json.dumps({"code": code, "argv": ["-c"] + list(argv)}, ensure_ascii=False).encode("utf-8")
    payload = base64.b64encode(gzip.compress(data, mtime=0)).decode("ascii")
    bootstrap = "import base64,gzip,json,sys;p=json.loads(gzip.decompress(base64.b64decode(sys.argv[1])));sys.argv=p['argv'];exec(compile(p['code'],'<remote-python>','exec'))"
    return ("$ErrorActionPreference='Stop'\n[Console]::OutputEncoding = New-Object System.Text.UTF8Encoding\n"
            "$OutputEncoding = [Console]::OutputEncoding\n& " + ps_literal(python) + " -X utf8 -B -c " +
            ps_literal(bootstrap) + " " + ps_literal(payload) +
            "\nif ($LASTEXITCODE -ne 0) { throw 'Python command failed' }")


def select_files(root):
    paths = []
    for name in ("code", "config", "configs", "inputs", "tools", "tests", "provenance", "initiator"):
        folder = root / name
        if folder.exists():
            for path in folder.rglob("*"):
                parts = path.relative_to(root).parts
                # The frozen simulator has Python modules in code/runtime/results.
                # Only generated result trees are excluded; source naming is retained.
                excluded = any(part in EXCLUDE and not (name == "code" and part == "results") for part in parts)
                if name == "initiator":
                    excluded = excluded or initiator_local_artifact(path, parts)
                if path.is_file() and not excluded and path.suffix not in (".pyc", ".pyo"):
                    paths.append(path)
    paths += [p for p in root.iterdir() if p.is_file() and p.suffix in (".md", ".json")
              and p.name != "bundle_manifest.json" and p.name not in LOCAL_STATE_FILES]
    if any(p.is_symlink() for p in paths):
        raise ValueError("Bundle sources must be regular files, not symlinks")
    return sorted(set(paths), key=lambda p: p.relative_to(root).as_posix())


def build(root, output):
    files = select_files(root)
    if not files:
        raise ValueError("No input files selected")
    rows = [{"path": p.relative_to(root).as_posix(), "sha256": sha(p), "bytes": p.stat().st_size} for p in files]
    output.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(output, "x", compression=zipfile.ZIP_DEFLATED) as archive:
        for row, path in zip(rows, files):
            item = zipfile.ZipInfo(row["path"], date_time=(1980, 1, 1, 0, 0, 0))
            item.compress_type = zipfile.ZIP_DEFLATED
            archive.writestr(item, path.read_bytes())
        item = zipfile.ZipInfo("bundle_manifest.json", date_time=(1980, 1, 1, 0, 0, 0))
        item.compress_type = zipfile.ZIP_DEFLATED
        archive.writestr(item, json.dumps({"schema": 1, "files": rows}, ensure_ascii=False, sort_keys=True, indent=2))
    digest = sha(output)
    record = {"archive": str(output), "archive_sha256": digest, "release_id": digest[:16], "files": len(rows), "bytes": output.stat().st_size}
    atomic_json(output.with_suffix(output.suffix + ".record.json"), record)
    return record


VERIFY_EXTRACT = r'''
import hashlib,json,pathlib,sys,uuid,zipfile
archive,release,expected = pathlib.Path(sys.argv[1]),pathlib.Path(sys.argv[2]),sys.argv[3]
def digest(path):
 h=hashlib.sha256()
 with path.open('rb') as f:
  for b in iter(lambda:f.read(1048576),b''):h.update(b)
 return h.hexdigest()
if digest(archive)!=expected:raise RuntimeError('ZIP SHA mismatch')
if release.exists():raise RuntimeError('Immutable release exists; refusing overwrite')
stage=release.with_name(release.name+'.staging-'+uuid.uuid4().hex)
stage.mkdir(parents=True)
with zipfile.ZipFile(archive) as z:
 manifest=json.loads(z.read('bundle_manifest.json'))
 names=[x['path'] for x in manifest['files']]
 if len(names)!=len(set(names)) or set(z.namelist())!=set(names+['bundle_manifest.json']):raise RuntimeError('Unexpected ZIP entries')
 for row in manifest['files']:
  name=pathlib.PurePosixPath(row['path'])
  if name.is_absolute() or '..' in name.parts or '\\' in row['path'] or ':' in row['path']:raise RuntimeError('Unsafe ZIP entry')
  dest=stage.joinpath(*name.parts);dest.parent.mkdir(parents=True,exist_ok=True)
  with z.open(row['path']) as src,dest.open('xb') as out:
   for data in iter(lambda:src.read(1048576),b''):out.write(data)
  if digest(dest)!=row['sha256'] or dest.stat().st_size!=row['bytes']:raise RuntimeError('Input SHA mismatch '+row['path'])
 (stage/'bundle_manifest.json').write_text(json.dumps(manifest,ensure_ascii=False,indent=2),encoding='utf-8')
(stage/'DEPLOYMENT.json').write_text(json.dumps({'archive_sha256':expected,'verified_files':len(names)},indent=2),encoding='utf-8')
stage.rename(release)
print(json.dumps({'release':str(release),'verified_files':len(names),'archive_sha256':expected},ensure_ascii=False))
'''


EXPORT_SNAPSHOT = r'''
import hashlib,json,pathlib,sys,zipfile
root,out,relative=pathlib.Path(sys.argv[1]),pathlib.Path(sys.argv[2]),sys.argv[3]
source=(root/relative).resolve();source.relative_to(root.resolve())
if not source.exists():raise RuntimeError('Result root missing')
rows=[];omitted=[];payload=[]
for p in sorted(source.rglob('*')):
 if not p.is_file() or p.is_symlink():continue
 name=p.relative_to(root).as_posix()
 if p.suffix.lower() not in ('.json','.log','.txt','.csv') or p.stat().st_size>8*1024*1024 or any(s in p.name.lower() for s in ('ledger','packet_prefix','trajectory_records')):
  omitted.append(name);continue
 # A synchronized snapshot is never a local runnable completion receipt.
 export_name=name+'.snapshot' if p.name=='complete.json' else name
 data=p.read_bytes();payload.append((export_name,data));rows.append({'path':export_name,'original_path':name,'sha256':hashlib.sha256(data).hexdigest(),'bytes':len(data)})
index={'schema':1,'scope':'compact_snapshot_only','remote_results':relative,'files':rows,'omitted':omitted,'completion_markers_promoted':False}
out.parent.mkdir(parents=True,exist_ok=True)
with zipfile.ZipFile(out,'x',compression=zipfile.ZIP_DEFLATED) as z:
 for name,data in payload:z.writestr(name,data)
 z.writestr('sync_manifest.json',json.dumps(index,ensure_ascii=False,indent=2))
h=hashlib.sha256(out.read_bytes()).hexdigest()
print(json.dumps({'archive':str(out),'archive_sha256':h,'files':len(rows),'omitted':len(omitted)}))
'''


def import_snapshot(archive, destination, expected):
    if sha(archive) != expected:
        raise ValueError("Downloaded snapshot ZIP SHA mismatch")
    if destination.exists():
        raise FileExistsError("Snapshot destination already exists")
    staging = destination.with_name(destination.name + ".staging-" + uuid.uuid4().hex[:8])
    staging.mkdir(parents=True)
    with zipfile.ZipFile(archive) as z:
        manifest = json.loads(z.read("sync_manifest.json"))
        names = [row["path"] for row in manifest["files"]]
        if len(names) != len(set(names)) or set(z.namelist()) != set(names + ["sync_manifest.json"]):
            raise ValueError("Unexpected snapshot entries")
        if manifest.get("completion_markers_promoted") is not False:
            raise ValueError("Untrusted completion scope")
        for row in manifest["files"]:
            name = PurePosixPath(row["path"])
            if name.is_absolute() or ".." in name.parts or "\\" in row["path"] or ":" in row["path"] or name.name == "complete.json":
                raise ValueError("Unsafe snapshot entry")
            data = z.read(row["path"])
            if hashlib.sha256(data).hexdigest() != row["sha256"] or len(data) != row["bytes"]:
                raise ValueError("Snapshot file SHA mismatch")
            path = staging.joinpath(*name.parts)
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(data)
        atomic_json(staging / "sync_manifest.json", manifest)
    staging.rename(destination)
    return {"snapshot": str(destination), "files": len(names), "scope": "compact_snapshot_only"}


def fallback_ssh_options(host, hostname, host_key_alias):
    if not hostname:
        if host_key_alias:
            raise ValueError("--fallback-host-key-alias requires --fallback-hostname")
        return ()
    host_key_alias = host_key_alias or host.rsplit("@", 1)[-1]
    if not all(re.fullmatch(r"[A-Za-z0-9_.:\[\]-]+", value) for value in (hostname, host_key_alias)):
        raise ValueError("Invalid SSH fallback host name or host key alias")
    return ("-o", "Hostname=" + hostname, "-o", "HostKeyAlias=" + host_key_alias)


def capture_checked(command, label, fallback_options=()):
    # Windows PowerShell may mix legacy-codepage CLIXML diagnostics into stderr.
    # Keep the underlying remote error visible; never echo long encoded commands.
    result = subprocess.run(command, capture_output=True, text=True, encoding="utf-8", errors="replace")
    if result.returncode == 255 and fallback_options and command[0] in ("ssh", "scp"):
        fallback = [command[0], *fallback_options, *command[1:]]
        result = subprocess.run(fallback, capture_output=True, text=True, encoding="utf-8", errors="replace")
    if result.returncode:
        raise RuntimeError("%s failed (exit %s)\nstdout:\n%s\nstderr:\n%s" %
                           (label, result.returncode, result.stdout[-6000:], result.stderr[-6000:]))
    return result


def upload(args, root):
    archive = inside(root, args.bundle)
    fallback = fallback_ssh_options(args.host, args.fallback_hostname, args.fallback_host_key_alias)
    digest = sha(archive)
    winroot = args.windows_root.rstrip("\\/")
    remote_zip = winroot + "\\.incoming\\" + digest + ".zip"
    release = winroot + "\\releases\\" + digest[:16]
    prepare = "$ErrorActionPreference='Stop'\nif (Test-Path -LiteralPath " + ps_literal(release) + ") { throw 'Immutable release exists' }\nNew-Item -ItemType Directory -Force -Path " + ps_literal(winroot + r"\.incoming") + " | Out-Null"
    extract = python_ps(args.python, VERIFY_EXTRACT, [remote_zip, release, digest])
    commands = [ssh_ps(args.host, prepare), ["scp", "-o", "BatchMode=yes", "-o", "ConnectTimeout=10", str(archive), args.host + ":" + remote_zip.replace("\\", "/")], ssh_ps(args.host, extract)]
    result = {"execute": args.execute, "host": args.host, "release": release, "archive_sha256": digest,
              "commands": commands, "remote_scripts": [prepare, extract]}
    if args.execute:
        for command in commands:
            capture_checked(command, "Immutable release transfer", fallback)
    return result


def pull(args, root):
    fallback = fallback_ssh_options(args.host, args.fallback_hostname, args.fallback_host_key_alias)
    if not re.fullmatch(r"[0-9a-f]{16}", args.release_id):
        raise ValueError("Release id must be 16 lowercase hex characters")
    if PurePosixPath(args.remote_results).is_absolute() or ".." in PurePosixPath(args.remote_results).parts or "\\" in args.remote_results or ":" in args.remote_results:
        raise ValueError("Expected safe release-relative result path")
    token = time.strftime("%Y%m%dT%H%M%SZ", time.gmtime()) + "-" + uuid.uuid4().hex[:8]
    release = args.windows_root.rstrip("\\/") + "\\releases\\" + args.release_id
    remote_zip = release + "\\sync_exports\\" + token + ".zip"
    local_zip = root / "sync" / (token + ".zip")
    destination = root / "sync" / (args.release_id + "-" + token)
    command = ssh_ps(args.host, python_ps(args.python, EXPORT_SNAPSHOT, [release, remote_zip, args.remote_results]))
    download = ["scp", "-o", "BatchMode=yes", "-o", "ConnectTimeout=10", args.host + ":" + remote_zip.replace("\\", "/"), str(local_zip)]
    if not args.execute:
        return {"execute": False, "commands": [command, download], "snapshot": str(destination), "scope": "compact_snapshot_only"}
    result = capture_checked(command, "SSH compact-result export", fallback)
    try:
        record = json.loads(result.stdout.strip().splitlines()[-1])
    except (IndexError, ValueError) as exc:
        raise RuntimeError("SSH export did not return a JSON archive record\nstdout:\n%s\nstderr:\n%s" %
                           (result.stdout[-6000:], result.stderr[-6000:])) from exc
    local_zip.parent.mkdir(parents=True, exist_ok=True)
    capture_checked(download, "Compact snapshot transfer", fallback)
    return import_snapshot(local_zip, destination, record["archive_sha256"])


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("action", choices=("build", "upload", "pull"))
    p.add_argument("--root", required=True)
    p.add_argument("--output", default="bundles/input.zip")
    p.add_argument("--bundle", default="bundles/input.zip")
    p.add_argument("--host", default="windows-runner", help="Target address or OpenSSH config alias")
    p.add_argument("--fallback-hostname", help="Retry SSH transport failures at this address")
    p.add_argument("--fallback-host-key-alias", help="Known-hosts alias for the fallback, defaulting to --host")
    p.add_argument("--windows-root", default=DEFAULT_ROOT)
    p.add_argument("--python", default=DEFAULT_PYTHON)
    p.add_argument("--release-id")
    p.add_argument("--remote-results", default="results")
    p.add_argument("--execute", action="store_true")
    args = p.parse_args()
    root = Path(args.root).resolve()
    if args.action == "build":
        result = build(root, inside(root, args.output))
    elif args.action == "upload":
        result = upload(args, root)
    else:
        result = pull(args, root)
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
