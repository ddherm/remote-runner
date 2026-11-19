# 公共运行器发起端

发起端由本地 Python 后台和浏览器界面组成。Codex 负责准备科学任务、输入与预算；用户在界面选择目标电脑、批次并行上限和远端工作根，后台完成部署并持续观察。任务在 Windows 独立运行，关闭页面或结束 Codex 对话不终止计算。

## Codex 交接流程

1. 在新的实验批次内准备未冻结的任务源与 `request.json`。复用历史输入时复制到本次准备目录或建立明确输入清单，不直接改写历史 release。
2. 执行 `python -B <remote_runner>/tools/initiator.py open --request <request.json>`，自动打开本地浏览器，记录返回的 `session_id`。
3. 等待用户填写并点击开始部署；用 `wait --session <id> --until deployed --timeout 55` 进行有界等待。检查返回的状态、`error`、`launch_receipt` 和远端路径；`wait_timed_out`、`failed`、`interrupted` 或只有 `disconnected` 均不能直接宣称部署成功。
4. 确认状态为 `running`、`monitoring` 或已完成，并有启动/接入记录后，结束本轮对话。后台继续每 5 秒同步；后续需要时调用 `status`。部署失败或启动返回不明先核对已有回执与进程，不能重复启动科学任务。

以下命令在 `remote_runner` 目录执行；`--state-dir` 为可选全局参数，若使用必须放在 `open/status/wait/list` 之前，并在各次调用中保持一致。

```bash
python3 -B tools/initiator.py open --request /absolute/path/request.json
python3 -B tools/initiator.py wait --session <session_id> --until deployed --timeout 55
python3 -B tools/initiator.py status --session <session_id>
python3 -B tools/initiator.py open --session <session_id>
python3 -B tools/initiator.py list
```

`wait --until finished` 可按需等待完成事件，每次最多55秒。等待超时不会取消部署或计算。默认状态目录为 `initiator/state`，保存连接配置、会话、部署包、回执和本地服务访问 token。该目录及带 `#token=` 的界面 URL 属于本机访问凭据，不外传、不提交仓库；向用户汇报时只给会话标识与运行结果。使用 `open --session` 可重新打开已保存会话。

## 多电脑与工作区管理

在电脑列表中勾选多台目标电脑，通过标签切换。保存草稿后，该工作区按电脑归属持久化，可在电脑页面查看。每台电脑的工作区独立部署，可并行运行。

归档的工作区进入该电脑的「已归档」列表。归档后仍可以查看记录和进度，正在进行的监控会继续。恢复后，工作区回到活动列表。删除只移除本地工作区记录，远端目录与结果保留。

已有 SSH 配置可以直接使用。需要为某台电脑配置备用连接时，可在启动后台前设置 `REMOTE_RUNNER_SSH_PRIMARY_ALIAS` 和 `REMOTE_RUNNER_SSH_FALLBACK_HOSTNAME`。备用连接使用原别名校验主机密钥，也可通过 `REMOTE_RUNNER_SSH_HOST_KEY_ALIAS` 指定已知主机别名。

## request 接口

`request.json` 是 UTF-8 JSON 对象。请求定义任务包，界面收集目标电脑、部署路径和并行设置。

| 字段 | 用法 |
| --- | --- |
| `mode` | `deploy` 为新部署，省略时默认此模式；`monitor` 只读接入已有远端任务。 |
| `title`、`summary` | 面向用户的实验名与真实组数、回合/场数摘要。`summary` 可为字符串、数组或对象。 |
| `source_root` | `deploy` 必填，本地未冻结任务源目录。CLI 支持相对于 request 文件的路径，也可使用绝对路径；HTTP 接口使用绝对路径。源目录不能位于 `releases` 下，也不能是已有 `bundle_manifest.json` 的冻结发布。 |
| `files` | 可选，要复制的源文件相对路径列表。建议明确列出；省略时使用公共打包器的代码/配置/输入/工具等目录选择规则，不会自动复制任意根目录文件。 |
| `manifest` | 顶层任务清单相对路径，默认 `manifest.json`。可从源目录复制，也可由 `manifest_template` 生成。 |
| `manifest_template` | 可选，顶层清单的 JSON 对象，绑定后写到 `manifest` 指定位置。 |
| `templates` | 可选，`{"包内相对路径": JSON对象}`；生成部署配置、子清单等 JSON 文件。它不改写 Python 源码。 |
| `manifest_files` | 可选，额外需要绑定和重新计算输入 SHA 的子清单相对路径列表。顶层清单自动包含在内。 |
| `work_plan` | 可选，计划分母。单个根协调器可用 `{"training":1000,"validation":260,"testing":0}`；多根任务使用 `{"tasks":{"任务id":{"training":500,"validation":130}}}`。只声明真实存在的工作。 |
| `remote_root` | 可选，界面目标目录初值。实际选择值作为部署容器根。 |

包内路径统一使用 `/`，必须为相对路径。不能使用 `..`、盘符、反斜杠、符号链接或 Windows 大小写冲突文件名。

最小部署请求示意如下；`worker.py` 及其参数、输出、科学预算必须由 Codex 按具体实验实现。

```json
{
  "mode": "deploy",
  "title": "实验X",
  "summary": "执行已确认的一场测试",
  "source_root": "/absolute/path/experiment-X/prepared_source",
  "files": ["tools/worker.py"],
  "manifest": "manifest.json",
  "manifest_template": {
    "results": "control",
    "budget_seconds": 3600,
    "max_parallel": "@parallel@",
    "inputs": [{"path": "tools/worker.py", "sha256": "绑定时重新计算"}],
    "tasks": [{
      "id": "test-1",
      "workspace": "group-X",
      "argv": ["{python}", "{root}/tools/worker.py", "--output", "{attempt}/result.json"],
      "timeout_seconds": 3600,
      "outputs": ["result.json"],
      "metadata": {"phase": "testing", "tests_total": 1}
    }]
  },
  "work_plan": {"testing": 1}
}
```

### 路径与资源绑定

模板中的完整 token 值保留其实际类型，例如 `"max_parallel":"@parallel@"` 会得到整数。token 嵌入较长字符串时作字符串替换。`argv` 中的 `{python}`、`{root}`、`{attempt}` 继续由 Windows 运行器按每个 attempt 展开，与发起端 token 分工不同。

| Token | 绑定结果 |
| --- | --- |
| `@target_root@` | 用户选择的远端部署容器根。 |
| `@remote_root@` | 本次实际执行根：`<target_root>\releases\<deployment_id>`。 |
| `@python@` | 目标电脑配置并探测的计算 Python。 |
| `@runtime@` | 本次独立公共运行器 release 根目录。 |
| `@slot_root@` | 目标电脑全局共享机器槽目录。 |
| `@parallel@` | 有效批次并行上限，即请求批次额度与已激活全机上限的较小值。 |
| `@requested_parallel@` | 用户填写的批次额度，范围1–36。 |
| `@global_limit@` | 目标电脑当前已激活的全机并行上限。 |
| `@deployment_id@` | 此会话持久保存的部署标识，用于工作根、额度和幂等核对。 |

绑定只发生在本地会话的独立 staging 副本。`manifest_template/templates` 生成 JSON 后，顶层及 `manifest_files` 中的清单统一写入计算解释器、机器槽策略和本批次 `execution_quota`，重新计算各项 `inputs[].sha256`。若父清单把子清单列为输入，先完成子清单，再计算父清单 SHA；循环清单依赖会拒绝。需要绑定的子清单必须列入 `manifest_files`，仅把它复制进包内不足以更新其中的路径和 SHA。

科学代码通过部署配置或运行时 spec 获取路径和额度，不在运行前对冻结源码作字符串替换。根协调器可以保留 `max_parallel: 1`；其内部训练/validate/测试子池使用 `@parallel@`、有效运行配置，或省略 `max_parallel` 以采用 `run_cases` 继承默认。不要在 Python 控制器或动态生成的子清单中硬编码18，否则全机调至36后内部仍只能派发18个。

### 只读监控请求

历史冻结批次使用以下模式，不提供 `source_root` 或重新打包科学代码。`execution_root` 是已有远端批次根；省略时取界面选择的目录。`state_file` 可省略，默认是该根下 `control\campaign_state.json`。

```json
{
  "mode": "monitor",
  "title": "查看已有实验",
  "execution_root": "C:\\RemoteRunner\\workspaces\\example",
  "manifest": "manifest.json"
}
```

发起端可向独立运行器 release 同步只读观察工具，但不改变旧任务使用的科学代码、运行器身份或验收版本。重连只恢复观察，原始日志和结果继续留在 Windows。

## 并行与进度

“批次并行上限”覆盖同一批次的所有消融组、训练、validate 和测试；“全机并行上限”覆盖该机器所有批次。两者支持1–36，全机默认沿用已激活的18。两组训练时显示实际2个计算任务，训练等待 validate 时释放自身额度，由独立验证 case 申请；任务数量不足时不填满额度。

界面先显示总计划项，再显示实际运行数、各组及并行任务明细。UavNetSim 兼容既有训练回合、优化更新、验证和测试字段；其他任务可发布 `metrics` 列表，每项包含 `id/label/current/total/unit`，并可用 `contributes_to_overall:false` 标注附属指标。只有实际声明的阶段才出现，优化更新默认不与训练回合重复计入总条。同一份父子工作只计一次，子 case 用于展开明细。缺少分母的任务显示不定进度；工作完成与回执验收是两个状态。

观察器每5秒采样资源，ETA依据观测速率估算；样本不足显示估算中，断线保留上一帧并标明更新时间。CPU首次采样及不可读取的GPU显示缺省状态，不伪造0%。日志按任务与attempt增量读取。

高级设置应用全机容量时，通过 `machine_policy.py --slot-root <目录> --capacity <1..36> --execute` 进行空闲交接；不带 `--execute` 只读计划。真实应用要求原生 Windows 核对计算owner、等待进程和派发器均已退出；权限不足、未知owner或仍在运行时拒绝，不自动停止任务。

## 示例

- `examples/engineering.json`：两个短时任务，演示训练、并行验证和实时日志。运行前按目标机的环境配置 Python 和工作区路径。
- `examples/monitor.json`：接入正在运行或已经完成的 Windows 工作区。使用前将 `execution_root` 和 `state_file` 改为目标工作区的实际路径。
