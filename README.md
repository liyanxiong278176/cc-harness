# cc-harness

一个在当前终端中运行的 coding agent。默认打开 focus-first fullscreen workspace，
对话和工具活动在同一个 alternate screen 中呈现，退出后恢复调用者的 shell；
需要可恢复任务控制面时，可显式选择 Durable Runtime。
项目不依赖 Textual。

## 安装与启动

要求 Python 3.11+，推荐使用 `uv`：

```powershell
uv tool install --editable .
cc-harness
```

开发时也可直接运行：

```powershell
python main.py
```

首次运行且没有模型配置时，会提示填写 OpenAI-compatible API Base URL、模型和
API key，并保存到 `~/.cc-harness/.env`。配置优先级为：进程环境变量 > 项目
`.env` > 用户 `~/.cc-harness/.env`。MCP 配置按用户级、项目级顺序合并，项目
同名配置覆盖用户配置。

## 常用启动方式

```powershell
cc-harness                         # 默认打开 fullscreen focus workspace
cc-harness --runtime durable       # 使用可恢复 Durable Runtime 控制面
cc-harness --runtime legacy --tui default # 兼容的原生 scrollback 视图
cc-harness -c                      # 继续当前目录最近的会话
cc-harness -r                      # 选择当前目录的历史会话
cc-harness -r SESSION_ID           # 继续指定会话
cc-harness --cwd D:\work\project   # 指定工作目录
cc-harness --add-dir D:\shared     # 增加允许访问的目录，可重复
cc-harness -p "summarize this repo" # 非交互打印模式
```

管道输入会自动使用无 ANSI 的打印模式：

```powershell
"explain this error" | cc-harness
```

## 交互

- 启动页采用 Claude Code classic inline shell 的双栏结构，但使用 cc-harness 自有名称、版本、更新记录，以及从用户参考图提取的彩色遮脸月薪喵像素形象；窄于 80 列时自动改为上下布局。
- `Enter` 提交；`Alt+Enter`、`Ctrl+J` 或行尾 `\` 插入换行。超过 800 字符或 2 行的粘贴会折叠为可展开标记，不会自动提交。
- `Shift+Tab` 在 `default`、`auto-edit`、`bypass-prompts` 之间切换。
- `Ctrl+O` 查看完整对话与工具活动；`Ctrl+S` 暂存/恢复草稿；`Ctrl+L` 清屏重绘。
- `F2` 或 `/inspector` 打开 Run Inspector；左右键切换 Overview、Timeline、Token、Context、Files、Errors 标签。
- 全屏 TUI 默认保留终端原生鼠标拖拽选择/复制；若更需要鼠标滚轮滚动，可在
  `.cc-harness/settings.json` 的 `ui` 下设置 `"capture_mouse": true`，此时终端原生选择可能需要按住终端的修饰键。
- Inspector 只显示版本、摘要、token/cache 计数、digest、耗时和错误数量；永不显示有效提示词、规则正文、来源映射或可重建片段。
- `Alt+P` 选择模型；`Alt+T` 切换推理强度；`Alt+V` 从剪贴板附加图片。
- `Ctrl+C` 取消当前请求或清空当前输入；`Ctrl+D` 或 `/exit` 保存并退出。
- 连按两次 `Esc` 会清空并暂存当前草稿；空输入时打开对话检查点恢复选择。
- 输入 `@path` 可附加文本、目录索引或 PNG/JPEG/WebP/静态 GIF 图片。

光标和输入内容实际位于上下边框之间。无背景状态区域紧贴输入框下边框并每 0.5 秒动态刷新，不会固定到终端窗口底部。第一行按实际存在的数据依次显示模型、项目与 Git、会话名、会话时长和自定义短语；第二行显示真实上下文占用；第三行显示当前权限模式，并仅在 agent 服务可用时显示 agents 提示。可在用户级 `~/.cc-harness/settings.json` 或项目级 `.cc-harness/settings.json` 定制，项目配置覆盖用户配置：

```json
{
  "ui": {
    "custom_line": "🛩️  冲鸭",
    "show_project": true,
    "show_git": true,
    "show_duration": true,
    "show_session_name": true,
    "startup_blank_rows": 3
  }
}
```

可用 slash 命令只展示已经接通的能力：

```text
/help /init /release-notes /status /clear /resume /exit
/coding /plan /design /chat /mode
/model /effort /permissions /verbose
/context /compact /tools /mcp /usage /inspector
```

`/init` 在当前目录创建 `CC-HARNESS.md` 项目指令文件；`/release-notes` 显示内置版本记录。若文件已经存在，`/init` 不会覆盖。

### 工具能力包与费用

默认只把小型 core 工具包放入模型请求；Web、MCP 和领域工具必须显式启用，
以保持稳定的工具 schema 前缀并提高供应商 KV cache 命中率。配置同样遵循
进程环境变量 > 项目 `.env` > 用户 `~/.cc-harness/.env`：

```text
CC_HARNESS_TOOL_BUNDLES=core,web
```

费用只显示供应商 API 返回的直接 cost/currency 字段；供应商没有返回直接费用
时显示 `unavailable`，不会按 token 单价估算。`/usage` 与 Inspector 的 Token
页同时展示 API 输入、输出、缓存命中和直接费用状态。

## 会话与安全

每个启动目录拥有独立的 `.cc-harness/sessions.db`。可恢复记录包含经处理的完整
对话、工具调用和结果，但不保存模型的原始推理草稿。图片会复制到该会话的私有
附件目录，删除会话时一并删除。旧版 `logs/memory.db` 只读导入，不修改原库。

附件默认限于启动目录及 `--add-dir`。`.git`、`.venv`、`node_modules`、常见密钥
文件和不支持的二进制文件会被拒绝。`bypass-prompts` 仅跳过普通确认，不绕过
路径边界、sandbox、敏感信息与 hard-deny 规则。

## 架构

```text
cc-harness / python main.py
  -> entrypoint.py
  -> DurableRuntimeClient         # 默认：可恢复任务、supervisor、审批、事件
  -> durable REPL                 # 轻量控制面，不复制运行状态

legacy 兼容入口
  -> SessionRuntime               # 配置、模型、MCP、策略、memory、session
  -> FullscreenTerminalApp        # focus workspace + Run Inspector
  -> InlineTerminalApp            # `/tui default` 兼容 scrollback 视图
  -> agent.run_turn               # 单一结构化事件流
```

提示词采用版本化稳定前缀 + 动态运行时后缀；外部规则已经过审核、适配并固定在
本地 registry，生产运行时不会联网拉取提示词更新。TUI 的可观测信息只引用安全
元数据（版本、不可逆 digest、token/cache 计数、压缩统计和错误事实），不会泄露
生产提示词正文。

## 测试

```powershell
python -m pytest tests -q
python -m ruff check cc_harness tests
```

产品决策与边界见 [CONTEXT.md](CONTEXT.md)、[设计说明](docs/specs/2026-08-02-claude-code-classic-ui-parity-design.md)
和 [ADR](docs/adr/)。
