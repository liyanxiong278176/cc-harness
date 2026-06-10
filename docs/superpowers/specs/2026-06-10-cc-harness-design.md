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
- **文本格式的工具调用解析**(用 `Action: {json}` 之类的协议);**所有工具调用走 OpenAI 原生 `tool_calls` 字段**

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
| `cc_harness/agent.py` | `run_turn(messages, mcp)` 单问题内部 ReAct 循环;`finish_reason` 路由;危险命令拦截;max_iter 护栏 | 60 |
| `cc_harness/repl.py` | REPL 主循环;维护 messages 列表;输入提示 | 30 |
| `cc_harness/tools.py` | `is_dangerous(name, args)` 危险命令模式匹配 + `confirm()` | 30 |
| `cc_harness/render.py` | `print_thought()`/`print_tool_call()`/`print_tool_result()`/`print_final()` 颜色封装 | 50 |
| `cc_harness/prompts.py` | 系统提示词常量 | 12 |
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
@dataclass
class PendingToolCall:
    index: int                         # delta 中的工具序号(从 0 开始)
    id: str | None = None              # 首 chunk 拿到,兼容 API 不给时为 None
    name: str | None = None            # 首 chunk 拿到,流式结束仍为 None 视为解析失败
    arguments_json: str = ""           # 后续 delta.arguments 字符串 concat

# MCP 工具调用返回结构(区分成功/失败)
@dataclass
class ToolResult:
    text: str                          # 序列化后的文本(给 LLM 看的)
    is_error: bool = False             # True = 异常或 isError=True;False = 正常结果
```

## 数据流(一次用户提问的完整路径)

**核心约定**:**全面使用 OpenAI 原生 tool_calls**,LLM 通过 API 字段返回结构化工具调用,
**不再**用正则解析 assistant 文本中的 `Action: {...}` 块。prompts.py 不再要求 LLM 输出特定 JSON 格式。

**单一判定**:agent.py 只用一个布尔值决定是否走"工具调用路径":

```python
has_tool_calls = (finish_reason == "tool_calls") and bool(pending) and all(p.name for p in pending)
```

`has_tool_calls == True` → 走工具调用路径(黄色打印 + 执行 + 回灌)
`has_tool_calls == False`:
  - 有 `content` → 当作 Final Answer(白色打印,加入 messages)
  - 无 `content` → 空 turn,黄色警告 `[yellow]empty LLM turn, ending[/yellow]`,回到 REPL

**`has_tool_calls == False` 但有 `pending` 的情况(流式结束 `name` 仍为 None)**:
- 不执行工具
- 把这个 pending 工具(`id` 可能也是 None,占位填 `"unknown_{i}"`)作为 `role: tool` 的 error 回灌
  `content = f"[Tool Error] tool_call name missing, raw: {json.dumps({'id': p.id, 'arguments_json': p.arguments_json})}"`
- 黄色警告 `tool_call parse failed: missing name`
- 不终止整个 turn,等 LLM 下一轮重试

1. 用户在 REPL 输入 "读一下 main.py 然后给我总结"
2. `repl.py`:`messages.append({"role": "user", "content": "..."})`
3. `agent.run_turn(messages, mcp)` 内部循环开始 (`iter=0`)
4. `llm.chat(messages, tools=tool_specs, stream=True)` → 流式生成
   - 流式阶段逐 token 处理每个 chunk:
     - `delta.content` → **蓝色**逐 token 打印(LLM 推理过程的"思考");流式结束前不写进 messages
     - `delta.tool_calls[i]` → 累积到 `pending[i]`(id / name 首 chunk 拿,arguments 字符串 concat),
       **不在流式中途执行**
   - 流式结束后:
     - 算出 `has_tool_calls`(见上)
     - `True` → 构建 assistant message(见 § 消息累积)加入 messages → 黄色打印调用 → 进入 §5
     - `False` 且有 content → 构建 assistant message 加入 messages → 白色打印 final → 退出内部循环
     - `False` 且有 pending 但 name 缺 → 把缺失的 pending 当作 `role: tool` error 回灌 → 继续内部循环
     - `False` 且无 content → 黄色警告,本次 turn 终止,回到 REPL
5. `mcp_client.call_tool(name, args) -> ToolResult`
   - 调 MCP `session.callTool()`,捕获异常
   - 成功:序列化 `result.content` 为 `text`;若 `result.isError` 或抛异常,`is_error=True`,`text` 放错误信息
6. 绿色 / 红色打印(根据 `result.is_error`):`render.print_tool_result(text, is_error=result.is_error)`
7. `messages.append({"role":"tool", "tool_call_id": pending[i].id, "content": content_for_llm})`
   - 成功:`content_for_llm = result.text`
   - 失败:`content_for_llm = f"[Tool Error] {result.text}"`(让 LLM 看到前缀并自愈)
8. 回到 [4] (`iter=1`),把工具结果喂给 LLM
9. LLM 这次 `has_tool_calls == False` 且有 content,白色打印 final,退出
10. 退出内部循环,等下一个用户问题

### 消息累积(关键边界)

流式阶段**实时打印**的蓝色 `delta.content` 文字**不**单独保存。流式结束后,**先把内容存在本地变量**
`assistant_content: str`,再根据路由决定怎么写到 messages。

**assistant message 三种形态**(路由后只写其中一种):

1. **纯 content**(`has_tool_calls == False` 且有 content):
   ```python
   messages.append({"role": "assistant", "content": assistant_content})
   ```

2. **纯 tool_calls**(`has_tool_calls == True` 且 `assistant_content` 为空):
   ```python
   messages.append({
     "role": "assistant",
     "content": None,  # 显式 None,不是 ""
     "tool_calls": [to_openai_tool_call(p) for p in pending],
   })
   ```

3. **content + tool_calls**(`has_tool_calls == True` 且 `assistant_content` 非空):
   ```python
   messages.append({
     "role": "assistant",
     "content": assistant_content,  # 思考文本
     "tool_calls": [to_openai_tool_call(p) for p in pending],
   })
   ```
   `content` 蓝色打印为 Thought,`tool_calls` 黄色打印为 Action,**两者都保留**,不互相覆盖。

**避免双计费**:流式打印归打印,`messages` 归 `messages`,不重复渲染。

## 错误处理(分四层)

| 层 | 场景 | 行为 |
|---|------|------|
| **配置** | `mcp.json` 找不到 / 解析错 / `.env` 缺 `OPENAI_API_KEY` | 启动时失败,红字打印 + 退出码 1,不进入 REPL |
| **MCP server 启动** | `npx` 不存在 / 子进程退出 / 握手超时(5s) | 红色打印 "server X failed to start: <err>",**继续启动其他 server**,REPL 仍可用;若所有 server 失败则警告 |
| **工具调用** | MCP session 抛异常 / 返回 `isError=True` / 工具超时(30s) | `call_tool` 返回 `ToolResult(is_error=True, text=<错误信息>)`;agent.py 红色打印 + `content_for_llm = f"[Tool Error] {result.text}"` 回灌,让 LLM 自愈(改参数/换工具);**不终止整个 turn** |
| **LLM** | 流式连接断 / API key 错 / 429 限流 | 红色打印,本次 turn 终止,回到 REPL 等用户重试;历史保留 |
| **LLM 输出异常** | `finish_reason` 既非 `tool_calls` 也非 `stop` | 黄色警告 + 本次 turn 终止,回到 REPL |
| **空 turn** | `has_tool_calls == False` 且 `assistant_content` 为空 | 黄色警告 `[yellow]empty LLM turn, ending[/yellow]`,本次 turn 终止,回到 REPL |
| **tool_call JSON 解析失败** | `json.loads(arguments_json)` 失败 | 红色打印,把 `arguments_json` 原文回灌为 `role: tool` 的 error,让 LLM 下一轮重试,**不终止整个 turn** |
| **tool_call name 缺失** | 流式结束 `pending[i].name` 仍为 `None` | 黄色警告,把这个 pending 用占位 id `"unknown_{i}"` 作为 `role: tool` 的 error 回灌,**不终止整个 turn** |
| **循环护栏** | `iter >= 20` | 黄色打印 "max iterations reached";**若还有 pending tool_calls,不执行它们,直接给一个温和 final**:"达到最大迭代次数,任务未完成。"(若已有 content 则用 content,否则用兜底文案);不暴露 JSON 给用户 |
| **危险命令** | shell 类工具的 `command` 字段含 `rm -rf` / `drop table` 等 | 黄色打印警告 + 工具名 + 参数 + "Confirm? [y/N]",默认 N;N 则回灌 error,LLM 可重选 |

## 危险命令匹配(初版规则)

```python
# 体验级安全 — 不是安全边界。真正安全要靠沙箱和权限控制,这里只是防误操作的提示。
DANGEROUS_PATTERNS = [
    r"\brm\s+-rf?\b",                # rm -r / rm -rf
    r"\brm\s+--\s",                  # rm -- 强制后续参数
    r"\brm\s+.*--no-preserve-root\b",# rm 强制删根
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

匹配位置:**仅对类 shell 工具的 `command` 字符串参数**(`name` 形如 `mcp__*__bash` / `mcp__*__run_command` /
`mcp__*__shell` / `mcp__*__execute` 之一,或工具的 `parameters` schema 中有名为 `command` 的字段)。
对 `mcp__filesystem__write_file` 等写文件工具**不做内容扫描**,避免 `content` 字段里偶然出现的
`rm -rf` 字符串误伤(用户完全可能让 agent 写一份"如何在 rm -rf 之前备份"的文档)。
仅检查,无内置白名单;只让用户决定。

`tools.py` 的签名: `is_dangerous(tool_name: str, arguments: dict) -> bool`。
判定逻辑: 先按工具名后缀(粗筛,模式如 `__bash$|__run_command$|__shell$|__execute$`),
或按 arguments 的 key 集合是否含 `command` 字段;两者命中任一即对 `command` 字符串做正则扫描。

## REPL 退出与 MCP 清理

| 触发 | 行为 | 退出码 |
|------|------|--------|
| REPL 提示符下按 Ctrl+D(EOF) | 走正常退出流程 | 0 |
| REPL 提示符下输入 `exit` 或 `quit` | 走正常退出流程 | 0 |
| REPL 提示符下按 Ctrl+C | 走正常退出流程(提示符层捕获) | 0 |
| LLM 流式生成中按 Ctrl+C | 关闭 OpenAI 流连接,本次 turn 中断并打印 "[yellow]interrupted[/yellow]",回到 REPL 提示符 | — (不退出 REPL) |
| LLM 流式生成中按 Ctrl+D | 同 Ctrl+C 处理(罕见) | — |
| 未捕获异常 | 红色打印 traceback 摘要,走正常退出流程 | 1 |

**正常退出流程(`repl.shutdown()`)**:
1. 红色提示"shutting down" + 等待 0.5s 让 stdout flush
2. 关闭所有 MCP `ClientSession`(调 `session.__aexit__()`)
3. 关闭所有 transport(`stdio_client` / `sse_client` / `streamablehttp_client` 的 `__aexit__()`,
   这会向 stdio 子进程发 EOF)
4. 等待 2s,若子进程未退则 `process.terminate()`(stdio 场景)
5. `asyncio.run()` 正常返回 → Python 进程退出码 0

**异常退出**:任何 step 抛异常都不阻断后续 step(用 `try/except` 串联),最后仍以 1 退出。

### 异步输入桥接(`repl.py` 关键点)

LLM 流式输出是 `async`,但 `input()` 是同步阻塞,**不能**直接 `await input()`。
`repl.py` 用 `asyncio.to_thread` 把阻塞调用丢到默认线程池,事件循环不被阻塞:

```python
import asyncio

async def _read_user() -> str:
    return await asyncio.to_thread(input, "› ")

async def run_repl(agent: Agent) -> None:
    while True:
        try:
            user_input = (await _read_user()).strip()
        except (EOFError, KeyboardInterrupt):
            break
        if not user_input:
            continue                                # 空行:忽略,回到提示符
        if user_input.lower() in ("exit", "quit"):
            break                                   # 显式退出
        await agent.run_turn(messages, mcp)
```

`asyncio.run()` 由 `main.py` 调,内部驱动 `repl.run_repl()` 协程。

## 配置加载约定

- `.env` 与 `mcp.json` **同目录**(项目根 `D:\agent_learning\cc-harness`)。
- `config.py` 入口函数 `load_config() -> AppConfig`,内部先 `dotenv.load_dotenv(env_path)`,再 `os.getenv`
  读:
  - `OPENAI_API_KEY` — **必填**,缺失则抛 `ConfigError`,启动失败
  - `OPENAI_BASE_URL` — 可选,默认 `https://api.openai.com/v1`
  - `OPENAI_MODEL` — 可选,默认 `gpt-4o-mini`
- `AppConfig` 用 `pydantic.BaseModel` 定义,字段类型 + 默认值 + 校验都走 pydantic,无 `dataclass`。
- 任何业务模块都**不直接**调 `dotenv`,只通过 `config.AppConfig` 拿到所需字段。

```python
class AppConfig(BaseModel):
    openai_api_key: str
    openai_base_url: str = "https://api.openai.com/v1"
    openai_model: str = "gpt-4o-mini"
    mcp_servers: dict[str, MCPServerConfig]  # 从 mcp.json 解析

    model_config = SettingsConfigDict(env_prefix="")  # 不从 env 读,只从 .env 显式灌入
```

`MCPServerConfig` 用 Pydantic 区分三种 transport:

```python
class MCPServerConfig(BaseModel):
    type: Literal["stdio", "sse", "http"] = "stdio"  # http = streamable-http
    command: str | None = None
    args: list[str] = []
    url: str | None = None
    env: dict[str, str] = {}
```

**mcp.json key 与配置类字段映射**:`mcp.json` 用标准 MCP 配置的 `mcpServers` 驼峰命名,
`AppConfig` 用 Python 风格 `mcp_servers` 蛇形命名。`load_config()` 内部做一次映射:

```python
raw = json.loads(mcp_json_path.read_text())
servers_raw = raw.get("mcpServers", {})            # mcp.json 原生 key
return AppConfig(
    openai_api_key=os.getenv("OPENAI_API_KEY"),
    mcp_servers={name: MCPServerConfig(**cfg) for name, cfg in servers_raw.items()},
)
```

业务代码**永远**用 `cfg.mcp_servers`,不接触 `mcpServers` 拼写。

## 系统提示词(放在 `cc_harness/prompts.py`)

```text
你是一个运行在终端里的编程助手,可以访问一组 MCP 工具(文件、shell 等)。
当前工作目录: {cwd}

# 规则
1. 当你需要执行操作时,请先输出你的思考过程(以"思考:"开头或自然段落皆可),然后通过工具调用来执行。
2. 工具调用由系统处理,你不需要在文本中输出 JSON 格式的 Action 块;
   **不要在文本中输出 `Action: {...}` 或模拟工具调用格式**,所有工具调用由系统通过结构化字段处理。
3. 如果不需要工具就能回答用户问题,直接回答,不要硬塞工具调用。
4. 如果工具执行失败,根据错误信息调整参数或换工具,不要重复同样的失败调用。
5. 危险操作(rm -rf、删库、format 等)即使工具允许,也请先在思考中向用户说明并请求确认。
6. 不要编造文件内容,没读过就说没读过。
7. 简洁优先,不要写无谓的客套话。
```

**无文本正则解析**。所有工具调用走 OpenAI 原生 `tool_calls` 字段;`agent.py` 只看
`finish_reason` + `tool_calls` + `content` 三个信号,不再 grep 文本中的 `Action:` / `Final Answer:`。

### 流式 tool_call delta 拼接规范(`llm.py`)

- OpenAI 的 tool_calls 流式响应,`delta.tool_calls[i].arguments` 是一段**不完整的 JSON 字符串片段**,
  不是整段 JSON 也不是 key-value patch。
- `llm.py` 维护 `pending[i].arguments_json`,每个 delta 把 `arguments` 字符串 **concat 追加**到末尾。
- 首个含 `tool_calls` 的 delta 拿到 `id` 和 `name`(`id` 和 `name` 都可能为 `None`,先占位)。
  - 若**流式结束** `id` 仍为 `None` → 在 `role: tool` 回灌时用占位值 `"unknown_{i}"`(i = tool_call 序号),避免 OpenAI SDK 校验报错
  - 若**流式结束** `name` 仍为 `None` → 视为 `name missing`,走 § 错误处理 的"name 缺失"路径
- 流式结束后,对每个 tool_call 整体执行 `json.loads(arguments_json)`。
  - 成功 → 拿 `(name, arguments_dict)`,转交 agent。
  - 失败 → 红色打印 `tool call JSON parse failed: ...`,把 `arguments_json` 原文回灌成 `role: tool` 的 error,
    让 LLM 在下一轮重新输出(不终止整个 turn)。

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
| 思考过程 | LLM 自然语言输出 + 原生 `tool_calls` | **第二轮 review 决定**:放弃文本正则解析,全面用 OpenAI 原生 tool_calls;`delta.content` 蓝色打印当思考,`tool_calls` 走结构化路由 |
| 路径范围 | 整个项目根 `D:\agent_learning\cc-harness` | 用户选定 |
| 循环护栏 | `max_iterations = 20` | 用户选定 |
| 项目结构 | 模块化分层 | 关注点分离,每个文件 < 150 行 |
| 配置类 | Pydantic `BaseModel` | 类型校验,第二 review 建议 |
| 同步输入 | `asyncio.to_thread(input, ...)` 桥接 | 第二 review 建议,避免阻塞事件循环 |

## 测试策略

| 文件 | 测什么 | 怎么测 |
|------|--------|--------|
| `tests/test_config.py` | 解析合法/非法的 mcp.json;`.env` 缺失 key 抛错 | 临时写 mcp.json、用 `monkeypatch` 改 env |
| `tests/test_mcp_client.py` | stdio 启动 in-process fake MCP server(`mcp.server.Server` 实例);`list_tools()` 转换正确;`call_tool()` 序列化结果 | 用 `mcp.server.Server` 起内存 stdio server,client 连过去调 |
| `tests/test_llm.py` | 流式 chunk 累积成完整 text;`tool_calls` delta 拼接正确(空 name → 后续 delta;name 出现后拼 arguments) | 用一个 mock 假流,喂 delta 序列断言 |
| `tests/test_agent.py` | 路由:`finish_reason=tool_calls` + 非空 `pending` → 执行工具并回灌 `role: tool`;`finish_reason=stop` + 非空 `content` → 当作 final 退出;`iter=20` 强制退出;危险命令 confirm 后 N 走回灌;空 turn(无 content 无 tool_calls)黄色警告 | 喂 mock LLM 模拟三类响应 |
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
| OpenAI `tool_call.arguments` delta 拼接在某些兼容 API 上格式不一致 | 兜底:流式拿到完整 JSON 后整体 `json.loads`,失败则把 `arguments_json` 原文回灌为 `role: tool` 的 error,让 LLM 在下一轮重新输出,**不终止整个 turn** |
| MCP stdio server 子进程在 Windows 上假死 | 加 `asyncio.wait_for(..., 30)` 兜底;子进程退出后 session 标记为 dead,REPL 仅打印黄色警告,**不做自动重连** |
| LLM 偶发不返回 `tool_calls` 也不返回 `content` 的空 turn(罕见) | 黄色警告 + 本次 turn 终止,回到 REPL 等用户决定(不重试) |
| `npx` 首次运行需要下载 `@modelcontextprotocol/server-filesystem` | README 中提示提前 `npx -y @modelcontextprotocol/server-filesystem --help` 验证 |
| 同步 `input()` 阻塞事件循环 → 流式输出时无法响应 Ctrl+C | `repl.py` 用 `asyncio.to_thread(input, "› ")` 桥接,事件循环不阻塞 |
