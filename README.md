# cc-harness

本地运行的 coding agent。启动后使用浏览器 WebUI 与模型对话，并在选定的项目目录中工作。

## 运行截图

以下截图于 **2026-10-21** 重新构建并启动当前项目后，在真实浏览器和模型调用过程中拍摄。
点击下表链接可直接查看图片。

| 执行阶段 | 图片访问路径 |
| --- | --- |
| 启动 WebUI | [01-welcome.png](img/runtime-20261021/01-welcome.png) |
| 选择当前项目并输入测试任务 | [02-task-ready.png](img/runtime-20261021/02-task-ready.png) |
| 命令等待审批 | [03-command-approval.png](img/runtime-20261021/03-command-approval.png) |
| 允许后执行命令 | [04-command-running.png](img/runtime-20261021/04-command-running.png) |
| 命令结果未知，任务阻塞 | [05-command-blocked.png](img/runtime-20261021/05-command-blocked.png) |
| 提交只读报告任务 | [06-report-reading.png](img/runtime-20261021/06-report-reading.png) |
| 报告摘要及暂停状态 | [07-report-result.png](img/runtime-20261021/07-report-result.png) |
| 查看上下文来源 | [08-context-sources.png](img/runtime-20261021/08-context-sources.png) |
| 查看本地 Skill 管理 | [09-local-skills.png](img/runtime-20261021/09-local-skills.png) |
| 查看 Runtime 状态与事件 | [10-runtime-state.png](img/runtime-20261021/10-runtime-state.png) |

本次命令演示因 `executor_lost` 阻塞；只读任务成功读取报告并生成摘要，随后因完成门槛暂停，
两次运行均未标记为 completed。详情见 [截图索引与运行记录](img/README.md)。
此前已完成的确定性测试结果见 [测试与修复报告](docs/audits/runtime-context-memory-skill-matrix-20261021.md)。

## 下载

```powershell
git clone https://github.com/liyanxiong278176/cc-harness.git
cd cc-harness
```

也可以直接下载 GitHub 仓库压缩包并进入解压后的目录。

## 安装

要求 Python 3.11 或更高版本。使用 `uv`：

```powershell
uv tool install --editable .
```

或使用 Python/pip：

```powershell
python -m pip install -e .
```

## 使用

```powershell
cc-harness
```

命令会启动本地 WebUI，并打印访问地址（默认是 `http://127.0.0.1:3080/`）。
浏览器打开该地址后，先选择项目文件夹，再输入任务。若不希望自动打开浏览器：

```powershell
cc-harness --no-open --port 3080
```

## 当前评测结果访问路径

本地记忆、Skill、续行资料与上下文来源可从 WebUI 的“本地能力与记忆”管理，也可运行 `cc-harness --command assets`。配置、命令及数据保留边界见 [本地信息资产说明](docs/local-information-assets.md)。

以下是当前项目已经完成的评测结果目录。每个目录中的 `report.md` 为阅读版报告，
`summary.json` 为机器可读汇总；评测结果仅保存在本机，未提交到 Git。

| 评测 | 结果目录 |
| --- | --- |
| Terminal-Bench 2.1（89 个任务，5 trials/task） | `D:\agent_learning\cc-harness\eval\result\cc-only\terminal-bench-2.1\deepseek-v4-flash\full-new-260905220633\` |
| LoCoMo（1,986 QA） | `D:\agent_learning\cc-harness\eval\result\cc-only\locomo-memory\deepseek-v4-flash\full\` |
| AgentDojo v1.2.2 balanced（500 trials） | `D:\agent_learning\cc-harness\eval\result\cc-only\agentdojo-v1.2.2-balanced-500\deepseek-v4-flash\portfolio\` |

结果目录中的主要文件：

```text
report.md       # 人工阅读版报告
summary.json    # 机器可读汇总
state.json      # 可恢复执行状态
integrity.json  # 结果完整性校验
raw/            # 原始任务证据
```
