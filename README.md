# 公共运行器 · Remote Runner

在本地浏览器管理多台 Windows 电脑上的 Python 批处理任务。每台电脑有独立页面和工作区，部署后可查看进度、日志与机器资源。后台通过 OpenSSH 连接目标电脑，任务在 Windows 上独立运行。

## 快速开始

发起端需要 Python 3.9+ 和 OpenSSH 客户端。目标电脑需要原生 Windows、已配置的 OpenSSH Server、Python 3.9+，以及可运行控制器和监督窗口的 Windows 用户会话。先通过 SSH 客户端验证连接与主机密钥。

```bash
python3 -B tools/initiator.py open --request initiator/examples/engineering.json
```

1. 在浏览器里填写目标地址。在「连接与机器设置」里填写目标机的 Python 绝对路径、运行器目录和计算槽目录，然后点击「连接」。用同样的方式添加其他电脑。
2. 在电脑列表勾选要同时管理的电脑。点击电脑标签，切换该电脑的工作区、运行进度和资源页面。
3. 从待分配列表打开新工作区，保存到已连接的电脑。设置该机的目标文件夹与并行上限后，可以部署或接入现有任务。每个工作区对应一台电脑，各台电脑可以同时运行各自的工作区。
4. 在工作区菜单归档记录，或从已归档列表恢复。删除会移除运行器中的工作区记录，保留 Windows 上的任务文件与结果。运行器无法确认任务已结束时会阻止删除。

重开已存在的工作区可用 `python3 -B tools/initiator.py open --session <id>`。启动器把本地状态存放在 `initiator/state/`，可通过全局参数 `--state-dir` 指向其他目录。保留原目录即可继续使用既有电脑配置与工作区记录。后台只监听回环地址，带访问凭据的页面链接供当前设备使用。

详细的任务请求格式、监控接入与命令见 [发起端使用说明](initiator/README.md)。

## 本地测试

```bash
python3 -B -m unittest discover -s tests -v
```

测试使用短时计算样例和模拟 SSH。Windows 专用检查在 Windows 上运行。
