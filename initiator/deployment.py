"""Deterministic binding, immutable upload and one-shot Windows launch."""
import hashlib
import json
import ntpath
from pathlib import Path, PurePosixPath
import shutil
import sys
import zipfile

from .transport import TOOLS, SSH, windows_path
from bundle_sync import select_files

RUNNER_ROOT = TOOLS.parent


def safe_relative(value):
    if not isinstance(value, str) or not value or "\\" in value or ":" in value:
        raise ValueError("任务包路径必须是相对路径")
    p = PurePosixPath(value)
    if p.is_absolute() or ".." in p.parts or value != p.as_posix():
        raise ValueError("任务包路径越界：" + value)
    return value


def digest(path):
    h = hashlib.sha256()
    with Path(path).open("rb") as stream:
        for block in iter(lambda: stream.read(1048576), b""):
            h.update(block)
    return h.hexdigest()


def bind(value, variables):
    if isinstance(value, dict):
        return {key: bind(item, variables) for key, item in value.items()}
    if isinstance(value, list):
        return [bind(item, variables) for item in value]
    if isinstance(value, str):
        if value in variables:
            return variables[value]
        for token, replacement in variables.items():
            value = value.replace(token, str(replacement))
        return value
    return value


def write_json(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False, sort_keys=True, indent=2, allow_nan=False) + "\n", encoding="utf-8")


def archive_files(root, files, destination):
    """Stable archive bytes; output ZIP deliberately lives outside its sources."""
    root, destination = Path(root).resolve(), Path(destination)
    rows = []
    folded = set()
    for path in sorted(files, key=lambda p: p.relative_to(root).as_posix()):
        name = safe_relative(path.relative_to(root).as_posix())
        if path.is_symlink() or path.resolve() != root.joinpath(*PurePosixPath(name).parts).resolve():
            raise ValueError("任务包不接受链接")
        path.resolve().relative_to(root)
        if name.casefold() in folded:
            raise ValueError("任务包包含 Windows 大小写冲突：" + name)
        folded.add(name.casefold())
        rows.append(dict(path=name, sha256=digest(path), bytes=path.stat().st_size))
    destination.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(destination, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        for row in rows:
            info = zipfile.ZipInfo(row["path"], date_time=(1980, 1, 1, 0, 0, 0))
            info.compress_type = zipfile.ZIP_DEFLATED
            archive.writestr(info, (root / row["path"]).read_bytes())
        info = zipfile.ZipInfo("bundle_manifest.json", date_time=(1980, 1, 1, 0, 0, 0))
        info.compress_type = zipfile.ZIP_DEFLATED
        archive.writestr(info, json.dumps(dict(schema=1, files=rows), ensure_ascii=False, sort_keys=True))
    return dict(archive=str(destination), archive_sha256=digest(destination), files=len(rows))


def build_runtime(directory):
    files = list(TOOLS.glob("*.py")) + [TOOLS / "launch_windows.ps1", RUNNER_ROOT / "provenance.json"]
    files += [RUNNER_ROOT / "initiator" / name for name in ("__init__.py", "progress.py")]
    files += list((RUNNER_ROOT / "tests").glob("test_runtime.py"))
    files += [RUNNER_ROOT / "tests" / name for name in
              ("test_execution_quota.py", "test_machine_policy.py", "test_observer.py", "test_progress.py", "test_bundle_selection.py")]
    files += list((RUNNER_ROOT / "tests").glob("test_capacity*.py"))
    record = archive_files(RUNNER_ROOT, [p for p in files if p.is_file()], Path(directory) / "runtime.zip")
    record["release_id"] = record["archive_sha256"][:16]
    return record


def prepare_package(request, options, directory, runtime_release):
    source = Path(request["source_root"]).resolve()
    if not source.is_dir():
        raise ValueError("Codex 准备的任务源目录不存在")
    # New deployment inputs must be prepared separately from historical releases.
    if "releases" in source.parts or (source / "bundle_manifest.json").exists():
        raise ValueError("冻结发布只允许监控；请由 Codex 准备新的部署模板")
    stage = Path(directory) / "bound"
    if stage.exists():
        shutil.rmtree(stage)
    stage.mkdir(parents=True)
    supplied = request.get("files")
    paths = [source / safe_relative(name) for name in supplied] if supplied is not None else select_files(source)
    for path in paths:
        path.resolve().relative_to(source)
        if not path.is_file() or path.is_symlink() or any(p.is_symlink() for p in path.parents if p != source.parent):
            raise ValueError("任务输入必须是源目录中的普通文件：" + str(path))
        destination = stage / path.relative_to(source)
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(path, destination)
    execution_root = ntpath.join(options["remote_root"], "releases", options["deployment_id"])
    effective = min(options["parallel"], options["global_limit"])
    variables = {"@remote_root@": execution_root, "@target_root@": options["remote_root"],
                 "@python@": options["python"], "@runtime@": runtime_release,
                 "@slot_root@": options["slot_root"], "@parallel@": effective,
                 "@requested_parallel@": options["parallel"], "@global_limit@": options["global_limit"],
                 "@deployment_id@": options["deployment_id"]}
    for name, template in request.get("templates", {}).items():
        write_json(stage / safe_relative(name), bind(template, variables))
    manifest_name = safe_relative(request.get("manifest", "manifest.json"))
    if "manifest_template" in request:
        write_json(stage / manifest_name, bind(request["manifest_template"], variables))
    manifest_files = list(dict.fromkeys([manifest_name] + request.get("manifest_files", [])))
    documents = {}
    for name in manifest_files:
        path = stage / safe_relative(name)
        doc = bind(json.loads(path.read_text(encoding="utf-8-sig")), variables)
        if not isinstance(doc.get("tasks"), list) or not doc["tasks"]:
            raise ValueError("任务清单没有可执行任务")
        doc.update(python=options["python"], slot_root=options["slot_root"], slot_capacity=options["global_limit"])
        # Preserve sequential coordinator manifests; leaf pools follow the effective quota.
        declared = doc.get("max_parallel", effective)
        doc["max_parallel"] = min(int(declared), effective)
        doc["execution_quota"] = dict(id=options["deployment_id"],
            root=ntpath.join(options["slot_root"], "quotas", options["deployment_id"]), capacity=options["parallel"])
        doc.setdefault("env", {}).update(GTR_BATCH_ROOT=execution_root, GTR_PUBLIC_RUNTIME=runtime_release)
        documents[name] = doc
        write_json(path, doc)
    # Hash dependency manifests after they have their final bindings, never the
    # earlier template bytes. Cycles cannot have well-defined input SHA values.
    finalized, visiting = set(), set()
    def finalize(name):
        if name in finalized:
            return
        if name in visiting:
            raise ValueError("任务清单之间存在循环 SHA 依赖")
        visiting.add(name)
        doc = documents[name]
        for item in doc.get("inputs", []) + [i for task in doc["tasks"] for i in task.get("inputs", [])]:
            relative = safe_relative(item["path"])
            if relative in documents:
                finalize(relative)
            file = stage / safe_relative(item["path"])
            if not file.is_file():
                raise ValueError("任务清单缺少输入：" + item["path"])
            item["sha256"] = digest(file)
        write_json(stage / name, doc)
        visiting.remove(name)
        finalized.add(name)
    if request.get("work_plan"):
        documents[manifest_name]["work_plan"] = request["work_plan"]
    for name in manifest_files:
        finalize(name)
    manifest = documents[manifest_name]
    record = archive_files(stage, [p for p in stage.rglob("*") if p.is_file()], Path(directory) / "task.zip")
    record.update(execution_root=execution_root, manifest=ntpath.join(execution_root, manifest_name.replace("/", "\\")),
                  state_file=ntpath.join(execution_root, str(manifest["results"]).replace("/", "\\"), "campaign_state.json"),
                  manifest_sha256=digest(stage / manifest_name), effective_parallel=effective)
    return record


PREPARE_UPLOAD = r'''
import json,pathlib,sys
p=pathlib.Path(sys.argv[1]);p.parent.mkdir(parents=True,exist_ok=True)
print(json.dumps(dict(path=str(p))))
'''

EXTRACT = r'''
import hashlib,json,pathlib,sys,uuid,zipfile
archive,release,expected=pathlib.Path(sys.argv[1]),pathlib.Path(sys.argv[2]),sys.argv[3]
def sha(p):
 h=hashlib.sha256()
 with p.open('rb') as f:
  for b in iter(lambda:f.read(1048576),b''):h.update(b)
 return h.hexdigest()
if sha(archive)!=expected:raise RuntimeError('ZIP SHA mismatch')
with zipfile.ZipFile(archive) as z:
 m=json.loads(z.read('bundle_manifest.json'));names=[r['path'] for r in m['files']]
 if len({n.casefold() for n in names})!=len(names) or set(z.namelist())!=set(names+['bundle_manifest.json']):raise RuntimeError('Unexpected ZIP entries')
 for name in names:
  p=pathlib.PurePosixPath(name)
  if p.is_absolute() or '..' in p.parts or '\\' in name or ':' in name:raise RuntimeError('Unsafe ZIP path')
 if release.exists():
  marker=json.loads((release/'DEPLOYMENT.json').read_text(encoding='utf-8'))
  if marker['archive_sha256']!=expected:raise RuntimeError('Immutable target belongs to another package')
  for row in m['files']:
   if sha(release/row['path'])!=row['sha256']:raise RuntimeError('Existing input changed: '+row['path'])
  print(json.dumps(dict(release=str(release),reused=True,archive_sha256=expected)));sys.exit(0)
 stage=release.with_name(release.name+'.staging-'+uuid.uuid4().hex);stage.mkdir(parents=True)
 for row in m['files']:
  target=stage.joinpath(*pathlib.PurePosixPath(row['path']).parts);target.parent.mkdir(parents=True,exist_ok=True)
  with z.open(row['path']) as src,target.open('xb') as dst:
   for data in iter(lambda:src.read(1048576),b''):dst.write(data)
  if target.stat().st_size!=row['bytes'] or sha(target)!=row['sha256']:raise RuntimeError('Extracted SHA mismatch')
 (stage/'bundle_manifest.json').write_text(json.dumps(m,ensure_ascii=False),encoding='utf-8')
 (stage/'DEPLOYMENT.json').write_text(json.dumps(dict(archive_sha256=expected)),encoding='utf-8')
 stage.rename(release)
print(json.dumps(dict(release=str(release),reused=False,archive_sha256=expected)))
'''


def upload_package(ssh, record, release, incoming_root):
    incoming = ntpath.join(incoming_root, ".incoming", record["archive_sha256"] + ".zip")
    ssh.run(PREPARE_UPLOAD, [incoming])
    ssh.upload(record["archive"], incoming)
    return ssh.run(EXTRACT, [incoming, release, record["archive_sha256"]], timeout=180)


START = r'''
import json,os,pathlib,subprocess,sys,time
root,manifest,tools,python,principal,identifier=sys.argv[1:7];root=pathlib.Path(root)
sys.path.insert(0,tools);from native_runtime import atomic_json,load_json
record=root/'deployments/initiator-launch.json';record.parent.mkdir(exist_ok=True)
if record.exists():
 prior=load_json(record)
 if prior['deployment_id']!=identifier:raise RuntimeError('Deployment identity mismatch')
 if prior['status']=='started':print(json.dumps(prior,ensure_ascii=False));sys.exit(0)
 raise RuntimeError('启动结果需要核对；保留启动记录，不自动重启。请让 Codex 检查计划任务和进程。')
lock=record.with_suffix('.lock');lock.mkdir()
try:
 atomic_json(record,dict(deployment_id=identifier,status='starting',started_unix=time.time()))
 command=['powershell.exe','-NoProfile','-ExecutionPolicy','Bypass','-File',str(pathlib.Path(tools)/'launch_windows.ps1'),'-BatchRoot',str(root),'-Manifest',manifest,'-Python',python,'-User',principal]
 result=subprocess.run(command,stdout=subprocess.PIPE,stderr=subprocess.PIPE,encoding='utf-8',errors='replace',timeout=150)
 if result.returncode:raise RuntimeError((result.stderr or result.stdout)[-3000:])
 launched=load_json(root/'deployments/launched.json');out=dict(deployment_id=identifier,status='started',launched=launched,started_unix=time.time())
 atomic_json(record,out);print(json.dumps(out,ensure_ascii=False))
finally:lock.rmdir()
'''

RECONCILE = r'''
import json,pathlib,subprocess,sys
root,state,tools=sys.argv[1:4];sys.path.insert(0,tools);from native_runtime import load_json
p=pathlib.Path(root)/'deployments/launched.json'
if not p.exists():raise RuntimeError('尚无启动回执，请让 Codex 核对部署阶段')
launched=load_json(p);name=launched['controller_task']
payload="(Get-ScheduledTask -TaskName '"+name.replace("'","''")+"').State.ToString()"
r=subprocess.run(['powershell.exe','-NoProfile','-NonInteractive','-Command',payload],capture_output=True,encoding='utf-8',errors='replace',timeout=15)
status=r.stdout.strip();doc=load_json(state) if pathlib.Path(state).exists() else {}
if status!='Running' and doc.get('status') not in ('complete','completed','failed'):
 raise RuntimeError('启动状态尚不能确认，不会自动重复启动；请让 Codex 检查控制器')
print(json.dumps(dict(status='started',launched=launched,controller_state=status),ensure_ascii=False))
'''
