# cc-harness 设计规格

**日期**: 2026-06-10
**状态**: 草案,等待评审
**目标读者**: 实现者(自己)、未来维护者

## 目标

构建一个运行在终端的编程助手,使用 OpenAI 兼容 LLM 作为后端,通过 MCP 协议访问外部工具(文件、shell 等),输出彩色可读的 ReAct 过程。最少够跑通一个"用户提问 → 思考 → 调工具 → 看结果 → 答用户"的闭环。

## 非目标(YAGNI)

- 工具结果图像/二进制回显(只支持文本)
- 多会话切换
- 会话持久化(进程退出即丢)
- 工具调用权限的精细化黑白名单(只做危险命令提示)
- 多 LLM 后端动态切换(写死 OpenAI 兼容接口)
- 工具调用并发(只串行)
- Plan mode / subagent / TodoWrite(全部留给未来)
- 任何前端 / TUI 框架(只 Rich)

## 架构

```
                ┌─────────────────────────────────┐
                │   REPL (repl.py)                │   Rich 输入提示
                │   while True: input → agent     │
                └────────────┬────────────────────┘
                             │ 用户消息(messages)
                             ▼
                ┌─────────────────────────────────┐
                │   Agent (agent.py)              │   ReAct 循环 max=20
                │   while iter < 20:              │   思考:蓝  调用:黄
                │     LLM.complete(messages)      │   结果:绿  失败:红
                │     parse Thought/Action        │   最终:白
                │     if Action: tool.run()       │
                │       ↳ if dangerous: confirm   │
                │     else: return final          │
                └────────┬───────────────┬────────┘
                         │               │
              ┌──────────▼─────────┐  ┌──▼──────────────┐
              │ LLM (llm.py)       │  │ MCP Client       │
              │ OpenAI stream=True │  │ (mcp_client.py)  │
              │  tool_call 增量拼接│  │ stdio/sse/http   │
              └────────┬───────────┘  │ 转 OpenAI schema │
                       │              └───┬──────────────┘
                       │                  │
              ┌────────▼──────────────────▼──────────┐
              │ Config (config.py)                   │
              │  mcp.json: servers[]                 │
              │  .env: OPENAI_API_KEY/BASE_URL/MODEL │
              └──────────────────────────────────────┘
```

## 文件清单

| 文件 | 职责 | 行数估算 |
|------|------|----------|
| `main.py` | 入口:`load_config()` → `MCPClient.start()` → `REPL.run()` | 15 |
| `cc_harness/__init__.py` | 包标记 | 2 |
| `cc_harness/config.py` | 解析 `mcp.json`、读取 `.env`、路径解析、校验 | 50 |
| `cc_harness/llm.py` | OpenAI 兼容 client 封装;流式 `chat()` 返回 `(text, tool_calls, finished)`;`accumulate_tool_calls()` 拼 delta | 90 |
| `cc_harness/mcp_client.py` | 三种 transport 启动;`list_tools()` 转 OpenAI tool schema;`call_tool(name, args)` 返回 str | 110 |
| `cc_harness/agent.py` | `run_turn(messages, mcp)` 单问题内部 ReAct 循环;解析 Thought/Action/Final | 80 |
| `cc_harness/repl.py` | REPL 主循环;维护 messages 列表;输入提示 | 30 |
| `cc_harness/tools.py` | `is_dangerous(name, args)` 危险命令模式匹配 + `confirm()` | 30 |
| `cc_harness/render.py` | `print_thought()`/`print_tool_call()`/`print_tool_result()`/`print_final()` 颜色封装 | 50 |
| `cc_harness/prompts.py` | 系统提示词常量 | 20 |
| `mcp.json` | MCP servers 配置 | 10 |
| `.env.example` | 环境变量样例 | 5 |
| `tests/test_config.py` | 配置解析 | 40 |
| `tests/test_mcp_client.py` | 内存 stdio MCP server 端到端 | 80 |
| `tests/test_llm.py` | 流式 delta 累积 | 60 |
| `tests/test_agent.py` | ReAct 解析 + 危险命令 + max_iter | 80 |
| `tests/test_render.py` | 颜色 ANSI 码断言 | 30 |
| `tests/conftest.py` | pytest 共享 fixture | 20 |
| `pyproject.toml` | 依赖 + ruff + pytest 配置 | 25 |
| `README.md` | 启动步骤(仅当用户要求时;默认不创建) | — |

## 数据契约

```python
# 一个 MCP 工具被加载后,在 agent 中以 OpenAI tool schema 形式存在
ToolSpec = {
    "type": "function",
    "function": {
        "name": "mcp__{server}__{tool}",
        "description": "...",
        "parameters": {...},  # 来自 inputSchema
    }
}

# 流式累积的 tool_call(LLM 输出 tool_calls 字段的 delta)
PendingToolCall = {
    "id": "call_xxx",            # 流式首 chunk 拿到
    "name": "mcp__fs__read_file", # 流式首 chunk 拿到
    "arguments_json": ""         # 后续 chunk 拼接
}
```

## 数据流(一次用户提问的完整路径)

1. 用户在 REPL 输入 "读一下 main.py 然后给我总结"
2. `repl.py`:`messages.append({"role": "user", "content": "..."})`
3. `agent.run_turn(messages, mcp)` 内部循环开始 (`iter=0`)
4. `llm.chat(messages, tools=tool_specs, stream=True)` → 流式生成
   - 蓝色逐 token 输出 Thought 段
   - 黄色输出 Action: `mcp__fs__read_file(path="main.py")`
   - 工具调用被解析,循环挂起
5. `mcp_client.call_tool("mcp__fs__read_file", {"path": "main.py"})`
   - 调 MCP `session.callTool()` → 拿到 `TextContent`/`ImageContent`
   - 转 str(优先取 `.text`,序列化兜底)
6. 绿色打印工具结果
7. `messages.append({"role":"assistant", "tool_calls":[...]})`
   `messages.append({"role":"tool", "tool_call_id":"...", "content": "..."})`
8. 回到 [4] (`iter=1`),把工具结果喂给 LLM
9. LLM 这次只输出白色 final answer(没有新 `tool_call`)
10. 退出内部循环,返回 final,等下一个用户问题

## 错误处理(分四层)

| 层 | 场景 | 行为 |
|---|------|------|
| **配置** | `mcp.json` 找不到 / 解析错 / `.env` 缺 `OPENAI_API_KEY` | 启动时失败,红字打印 + 退出码 1,不进入 REPL |
| **MCP server 启动** | `npx` 不存在 / 子进程退出 / 握手超时(5s) | 红色打印 "server X failed to start: <err>",**继续启动其他 server**,REPL 仍可用;若所有 server 失败则警告 |
| **工具调用** | MCP session 抛异常 / 返回 `isError=True` / 工具超时(30s) | 红色打印,作为 `role: tool` 的 error 内容回灌给 LLM,让 LLM 自愈(改参数/换工具) |
| **LLM** | 流式连接断 / API key 错 / 429 限流 / 解析失败 | 红色打印,本次 turn 终止,回到 REPL 等用户重试;历史保留 |
| **循环护栏** | `iter >= 20` | 黄色打印 "max iterations reached",强制把当前 LLM 输出作为 final,等用户 |
| **危险命令** | 工具参数含 `rm -rf` / `drop table` 等 | 黄色打印警告 + 工具名 + 参数 + "Confirm? [y/N]",默认 N;N 则回灌 error,LLM 可重选 |

## 危险命令匹配(初版规则)

```python
DANGEROUS_PATTERNS = [
    r"\brm\s+-rf?\b",                # rm -r / rm -rf
    r"\brm\s+--\s",                  # rm -- 强制后续参数
    r"\bdel\s+/[sqf]\b",             # windows del /s /q /f
    r"\bformat\s+[a-zA-Z]:",         # windows format
    r"\bdrop\s+(database|table|schema)\b",
    r"\btruncate\s+table\b",
    r":\(\)\{\s*:\|:&\s*\};:",       # fork bomb
    r"\bdd\s+if=.*of=/dev/",         # 磁盘擦写
    r"\bshutdown\b",                 # 关机
    r"\breboot\b",
]
```

匹配位置:**工具调用前**(对 `mcp__filesystem__write_file` 检查 `path`+`content`,对 `mcp__bash` 类检查 `command`)。仅检查,无内置白名单;只让用户决定。

## 系统提示词(放在 `cc_harness/prompts.py`)

```text
你是一个运行在终端里的编程助手,可以访问一组 MCP 工具(文件、shell 等)。
当前工作目录: {cwd}

# 输出格式(每次回复必须严格遵守)
你的每次回复必须包含两段,顺序如下,不能多也不能少:

Thought: <你的自然语言推理,1-3 句,说明下一步要做什么>
Action: <调用一个工具,JSON 格式,例如>
  {"name": "mcp__filesystem__read_file", "arguments": {"path": "main.py"}}

# 收尾
当不需要再调用工具时,直接输出:
Thought: <简短的总结>
Final Answer: <给用户的最终回答,1-5 段,可以包含代码块>

# 规则
1. 每次只调用一个工具,等结果回来再决定下一步。
2. 工具名前缀: mcp__<server_name>__<tool_name>(来自工具清单)。
3. 如果工具执行失败,根据错误信息调整参数或换工具,不要重复同样的失败调用。
4. 如果用户的问题不需要工具就能回答,直接给 Final Answer,不要硬塞工具调用。
5. 危险操作(rm -rf、删库、format 等)即使工具允许,也请先在 Thought 中向用户说明并请求确认。
6. 不要编造文件内容,没读过就说没读过。
7. 简洁优先,不要写无谓的客套话。
```

解析正则:

```python
THOUGHT_RE = re.compile(r"Thought:\s*(.+?)(?=\nAction:|\nFinal Answer:|$)", re.S)
ACTION_RE  = re.compile(r"Action:\s*(\{.*?\})", re.S)
FINAL_RE   = re.compile(r"Final Answer:\s*(.+)$", re.S)
```

`llm.py` 在流式阶段只把"原文"吐给 `render.py`(蓝色),agent 在拿到完整 assistant message 后再解析 Action / Final。**没有原文回灌歧义**。

## Rich 颜色规范

| 元素 | 颜色 |
|------|------|
| 思考 (Thought) | 蓝色 (blue) |
| 工具调用 (Action) | 黄色 (yellow) |
| 工具调用成功结果 | 绿色 (green) |
| 工具调用失败 | 红色 (red) |
| LLM 最终输出 (Final Answer) | 白色 (white) |
| 用户输入提示符 | 青色 (cyan) |
| 警告 / 危险命令 | 黄色 (yellow) |

## MCP 客户端设计

### mcp.json schema

```json
{
  "mcpServers": {
    "filesystem": {
      "type": "stdio",
      "command": "npx",
      "args": ["-y", "@modelcontextprotocol/server-filesystem", "D:/agent_learning/cc-harness"]
    },
    "remote": {
      "type": "sse",
      "url": "http://localhost:8000/sse"
    },
    "streamable": {
      "type": "http",
      "url": "http://localhost:8000/mcp"
    }
  }
}
```

`type` 字段:
- `"stdio"`(或省略 + 有 `command`)→ `stdio_client(StdioServerParameters(...))`
- `"sse"`(有 `url`)→ `sse_client(url)`
- `"http"` 或 `"streamable-http"`(有 `url`)→ `streamablehttp_client(url)`

### Tool schema 转换

`mcp_client.py` 在 `list_tools()` 时:
1. 对每个 server,起一次 MCP session
2. `await session.list_tools()` → `list[Tool]`
3. 转换为 OpenAI 工具 schema,`name` 加 `mcp__{server}__{tool}` 前缀以避免冲突
4. 在 `description` 前添加 `[server: <server_name>] ` 前缀,让 LLM 知道工具来源
5. 缓存到 `self.tools: list[ToolSpec]`,`call_tool(name, args)` 时按前缀路由到对应 server

### 启动流程

```python
async def start(self):
    for name, cfg in self.servers.items():
        try:
            transport = self._make_transport(cfg)  # 异步 context manager
            read, write = await transport.__aenter__()
            session = ClientSession(read, write)
            await session.__aenter__()
            await asyncio.wait_for(session.initialize(), timeout=5.0)
            self.sessions[name] = (transport, session)
        except Exception as e:
            print_render(f"[red]server {name} failed: {e}[/red]")
```

**所有 transport 与 session 必须维持长连接**,直到 REPL 结束;不在每次 `call_tool` 时重新握手。

### stdio path 解析

`args` 中的相对路径解析为相对 mcp.json 所在目录(`os.path.dirname(mcp_json_path)`)。当前项目场景下,filesystem server 的 `args` 写 `"D:/agent_learning/cc-harness"`(绝对路径,避免歧义)。

## 关键设计决策

| 决策 | 选择 | 理由 |
|------|------|------|
| LLM 客户端 | OpenAI 兼容 SDK | 用户选定;覆盖面最广,本地/云端都可用 |
| MCP 客户端 | 官方 `mcp` Python SDK | 用户选定;协议实现正确,三种 transport 都有现成 client |
| 响应模式 | 流式 | 用户选定;体验贴近 Claude Code |
| 会话 | 多轮 REPL | 用户选定;真正的编程 agent 体验 |
| 角色 | 通用编程助手 | 用户选定;提示词通用化 |
| 安全 | 危险命令提示 | 用户选定;本地用,无需精细权限 |
| 历史 | 不持久化 | 用户选定;贴合"最小闭环" |
| 思考过程 | 提示词驱动 | 用户选定;兼容任何 OpenAI 兼容 API |
| 路径范围 | 整个项目根 `D:\agent_learning\cc-harness` | 用户选定 |
| 循环护栏 | `max_iterations = 20` | 用户选定 |
| 项目结构 | 模块化分层 | 关注点分离,每个文件 < 150 行 |

## 测试策略

| 文件 | 测什么 | 怎么测 |
|------|--------|--------|
| `tests/test_config.py` | 解析合法/非法的 mcp.json;`.env` 缺失 key 抛错 | 临时写 mcp.json、用 `monkeypatch` 改 env |
| `tests/test_mcp_client.py` | stdio 启动 in-process fake MCP server(`mcp.server.Server` 实例);`list_tools()` 转换正确;`call_tool()` 序列化结果 | 用 `mcp.server.Server` 起内存 stdio server,client 连过去调 |
| `tests/test_llm.py` | 流式 chunk 累积成完整 text;`tool_calls` delta 拼接正确(空 name → 后续 delta;name 出现后拼 arguments) | 用一个 mock 假流,喂 delta 序列断言 |
| `tests/test_agent.py` | ReAct 解析:Thought/Action 拆分;Final Answer 截断;`iter=20` 强制退出;危险命令 confirm 后 N 走回灌 | 喂 LLM 输出字符串、模拟 tool_call 返回值 |
| `tests/test_render.py` | 颜色常量与 Rich Style 匹配(蓝/黄/绿/红/白) | 断言 `console.export_text()` 含 ANSI 颜色码 |

**不测**:`tools.py` 走经验,3 条命令覆盖即可;`repl.py` 是 I/O 胶水,集成测试覆盖。
**覆盖率目标**:`pytest --cov=cc_harness`,70% 业务代码。
**测试运行**:`pytest -q`。

## 依赖(`pyproject.toml`)

```toml
[project]
name = "cc-harness"
version = "0.1.0"
requires-python = ">=3.11"
dependencies = [
  "openai>=1.40",
  "mcp[cli]>=1.0",
  "rich>=13.7",
  "python-dotenv>=1.0",
  "pydantic>=2.6",
]

[project.optional-dependencies]
dev = ["pytest", "pytest-asyncio", "pytest-cov", "ruff"]

[tool.pytest.ini_options]
asyncio_mode = "auto"
addopts = "-q"
```

## 启动序列

```
$ cp .env.example .env       # 编辑填 OPENAI_API_KEY / BASE_URL / MODEL
$ pip install -e ".[dev]"
$ python main.py
```

REPL 提示符:

```
[cc-harness] /d/agent_learning/cc-harness ›  (3 tools, 1 server)
›
```

输入 `exit` / `quit` / Ctrl+C / Ctrl+D 退出。

## 验收标准(Definition of Done)

1. `python main.py` 启动,加载 `mcp.json` 中所有 server,显示工具数量
2. 输入 "读一下 main.py 然后给我总结" 能:
   - 蓝色看到 Thought
   - 黄色看到 Action
   - 绿色看到 main.py 的内容
   - 白色看到总结
3. 输入 "删除 main.py" 会被拒绝,显示黄色警告 + Confirm 提示
4. 关闭 REPL 后再启动,`messages` 为空(不持久化)
5. `pytest -q` 全部通过
6. `ruff check` 全部通过
7. README 不强制创建(用户没要求)

## 已知风险

| 风险 | 缓解 |
|------|------|
| OpenAI `tool_call.arguments` delta 拼接在某些兼容 API 上格式不一致 | 兜底:流式拿到完整 JSON 后整体 `json.loads`,失败则报错并终止 turn |
| MCP stdio server 子进程在 Windows 上假死 | 加 `asyncio.wait_for(..., 30)` 兜底;子进程退出后 session 标记为 dead,REPL 提示重连 |
| 不同 LLM 提示词遵从度不同,可能不输出 `Action:` 段 | 解析失败时把 LLM 原文回灌成 `role: tool` 的 error,提示"请按格式回复" |
| `npx` 首次运行需要下载 `@modelcontextprotocol/server-filesystem` | README 中提示提前 `npx -y @modelcontextprotocol/server-filesystem --help` 验证 |
