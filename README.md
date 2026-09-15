# cc-harness

本地运行的 coding agent。启动后使用浏览器 WebUI 与模型对话，并在选定的项目目录中工作。

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
