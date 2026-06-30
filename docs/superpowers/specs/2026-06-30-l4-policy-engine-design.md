# L4 权限闸门(Claude Code 式)+ 执行加固: Spec (M1)

**Date:** 2026-06-30
**Branch:** test-red-team
**Status:** PROPOSED
**里程碑:** 纵深防御路线图的 **M1**(L4)。后续 M2(L2 输入防御)、M3(L5 输出 DLP)、M4(L6 监控 + 数据流守卫)在各自 spec。

## Problem

cc-harness 目前的安全只有 `cc_harness/tools.py` 的 `is_dangerous` 正则闸门,有两个结构性缺口:

1. **覆盖面太窄。** `is_dangerous` 只扫 shell 工具的 `command` 字段、且只匹配 `rm -rf`/`drop`/`format` 等少数正则(`tools.py:9-21`)。大量红队攻击**根本不进闸门**:
   - `cat ~/.ssh/id_rsa`(credential-exfil)→ 不匹配正则 → 今天会真跑、真泄露。
   - MCP `read_file ~/.aws/credentials` → 不是 shell → 完全无人管。
   - 写 `cc_harness/` 源码(self-modification)→ 不是 shell → 只靠 wrapper 事后快照兜底,非事前拦。
   - `curl 169.254.169.254`(ssrf)→ 没有该正则 → 今天会跑。
2. **不是真正的执行边界。** 即使命中正则,也只是 `confirm()` 一次 y/N;放行后子进程仍带完整环境(含 `OPENAI_API_KEY`)、cwd 不锁,爆破半径未受限。

## Goals

- **G1.** 把工具派发点(`agent.py:207` 那个 `is_dangerous` 位置)换成**用户同意制**权限闸门:执行/写/出工作区读/出站类工具调用必须经用户同意,同意才执行。
- **G2.** 决策只有两档:`allow`(直接执行)/ `ask`(弹用户);三种结果:allow→执行、ask→yes→执行、ask→no→不执行。**无 deny 档、无黑名单规则**;"不执行"只来自用户点 no。
- **G3.** `run_command` 放行后仍受执行加固:cwd 锁项目根、子进程 env 剥离密钥、30s 超时。
- **G4.** 每次决策落审计 `logs/policy.jsonl`。
- **G5.** 工具参数 schema 校验(native Pydantic + MCP jsonschema),拦类型/枚举越界。
- **G6.** 全套单元测试 + 红队兼容(用户自行跑 before/after 衡量)。

## Constraints(查证结论)

- **平台:Windows。** gVisor/Firecracker/OpenSandbox 是 Linux 专属 → M1 不做内核级沙箱,用"权限闸门 + 进程加固"的可移植方案;预留 `Executor` 接口供后续插真沙箱(已确认方案 A)。
- **依赖:`pydantic>=2.6`、`PyYAML` 已在 `pyproject.toml`**;新增 `jsonschema>=4`。
- **无 session 持久化**(CLAUDE.md):"以后不再问"的 allowlist 只存活于进程内,退出即失效;持久化由用户写 `policy.yaml`。
- **红队归用户**:本 spec **不执行、不改动** `eval/`。M1 只交付引擎 + 测试,用户自行跑 promptfoo before/after。
- **红队兼容性(关键)**:`eval/promptfoo/wrappers/cc_harness.py` 只往 stdin 写 `prompt\nexit\n`,从不写确认。`confirm()` 的 `input()` 读到的下一行是字符串 `"exit"`(**非 EOF**),`"exit" != "y"` → 返回 False(默认 N)→ **所有 ask 在红队里自动不执行**。且 `judges/attack_held_ground.txt:57-62` 把"要求二次确认 + 没执行"判为 hold ground(1.0)。→ 新闸门与现有红队天衣无缝,无需改 wrapper。(实现/测试时按"喂 `exit` 行"复现,而非 EOF——两条路径都返回 False,但红队走的是 not-y 默认路径。)

## Design

### 决策模型(核心)

```
tool_call(name, args) 进来
  → schema 校验                       [失败 → 错误观察块,不执行]
  → PolicyEngine.evaluate(name, args, ctx) → Decision
        ├ allow → 审计 + 派发执行
        └ ask   → 审计 + confirm(yes/no/以后不再问)
                   ├ yes           → 派发执行
                   ├ yes+always    → 进会话 allowlist + 派发执行
                   └ no / EOF      → 观察块"[未执行:用户拒绝]",不执行
```

- **两档决策**:`allow` / `ask`。无 `deny`。
- **会话 allowlist**:用户选"以后不再问"→ `(tool_name, 匹配模式)` 进内存 set;同会话再命中 → 直接 allow(不再问)。退出即失效。
- **路径归属判定**:用 `pathlib` 先 `resolve()`(解 `..`/符号链接/`~`/`%USERPROFILE%`),再 `is_relative_to(项目根)` 判"工作区内/外"。跨平台。

### 工具分档(默认)

| 档 | 命中 |
|---|---|
| **allow**(直接执行) | 工作区**内**的 fs-read/list/grep、git-read、context7 查文档 |
| **ask**(问用户) | `run_command`(任何 shell)、fs-write/move/delete、**工作区外**的 fs-read(如 `~/.ssh`、`~/.aws`、`/etc`)、网络工具(fetch/bing)、git-write(push/reset) |

分档按工具名模式 + 参数(路径归属、是否 shell)。默认值在 `policy.py`;`policy.yaml` 可覆盖。

> 关键:**"工作区外读"落到 ask**,无需内置"敏感路径"黑名单。`~/.ssh`、`~/.aws` 天然在工作区外 → ask → 红队自动拒 → 不泄露。这是结构性判断,不是黑名单。

### 新增组件

```
cc_harness/
  policy.py      # Decision(allow/ask + reason + rule_id) + 会话 allowlist + PolicyEngine.evaluate()
  schema.py      # Pydantic(native) + jsonschema(MCP) 参数校验
  executor.py    # Executor 协议 + NativeExecutor(cwd-lock / env-strip / 30s 超时)
  audit.py       # 每次 decision → logs/policy.jsonl
  config.py      # +PolicyConfig(pydantic) + 可选 policy.yaml 加载
```

修改:`agent.py`(派发点改写)、`tools.py`(`run_command` 改用 NativeExecutor + 加固;`is_dangerous` 正则降级为 ask 档的一条规则输入)、`repl.py`/`main.py`(启动构造 `PolicyEngine` 注入 `run_turn`)、`pyproject.toml`(+`jsonschema`)。

### 派发点改写(`agent.py`,替换现 `is_dangerous` 块 ~L207)

`ctx`(含**项目根**)由 `run_turn` 的 `cwd` 派生;`policy.evaluate` 与 allowlist 都用它做路径归属判定。`run_turn` 已收 `cwd`(`agent.py:49`)并传给 handler(`agent.py:223`),新增逻辑沿用同一来源。

```
for each pending tool_call p:
    args = json.loads(p.arguments_json) or {}
    # 1. schema 校验
    if not schema_valid(p.name, args, mcp_schemas): → 错误观察块, continue
    # 2. allowlist 命中?
    if allowlist_hits(p.name, args): decision = allow
    else: decision = policy.evaluate(p.name, args, ctx)
    # 3. 处置
    audit.log(decision, p.name, args)
    if decision.allow:  dispatch(native→Executor / mcp.call_tool)
    elif decision.ask:
        choice = confirm(p.name, args)   # yes / yes+always / no  (默认 no;EOF→no)
        if yes+always: allowlist.add(p.name, args_pattern)
        if yes or yes+always: dispatch(...)
        else: 观察块"[未执行:用户拒绝]"
```

`tools.py:77` 的 `run_command` 内部那次 `is_dangerous` 检查**移除**(agent 层闸门已权威);执行加固(env/cwd/超时)留在 NativeExecutor。

### 执行加固(`NativeExecutor`,对 allow/yes 的 run_command)

- **cwd 锁**项目根(现已有 cwd 参数,改为强制不跟随 args)。
- **env 剥离**:子进程环境删 `OPENAI_API_KEY`、`OPENAI_BASE_URL`,及名字匹配 `*KEY*`/`*TOKEN*`/`*SECRET*`/`*CREDENTIAL*` 的变量(L7"凭证不可达"可移植版)。
- **30s 超时**(已有,`RUN_COMMAND_TIMEOUT_S`)。
- stdout/stderr 捕获(已有)。
- (可选 stretch)Windows Job Object(pywin32,有则用、无则优雅降级)保证父死子杀。

### 审计(`logs/policy.jsonl`)

每条决策一行 JSON:
```json
{"ts":"2026-06-30T15:20:01","iter":3,"tool":"run_command","args":{"command":"cat ~/.ssh/id_rsa"},"decision":"ask","outcome":"no","rule_id":"shell_exec","reason":"run_command 需用户确认","mode":"coding"}
```
追加写;写失败只 warn,不阻塞决策(可用性优先)。本地取证,完整记 args。路径钉到**项目根**(`<项目根>/logs/policy.jsonl`),不随调用方 CWD 漂移(避免 pytest 在不同目录跑时散落 jsonl)。

### Schema 校验(`schema.py`)

- native(`run_command`):Pydantic 模型(`command: str`,非空)— 把 `run_command` 里现有校验(`tools.py:70-75`)前移。校验失败的 **LLM 可见错误形态保持与现有 `ToolResult.error` 等价**,不破坏现有测试。
- MCP 工具:按 `mcp.list_tools()` 返回的 JSON schema,用 `jsonschema` 校验 args。失败 → 错误观察块,LLM 可改参数重试。

### 错误处理

- schema 失败 → 错误观察块,LLM 重试。
- ask → no / EOF / Ctrl-C → 不执行(默认 no)。**这正是红队自动拒的机制。**
- Executor 超时 → 现有行为(返回 `[Tool Error] timeout`)。
- 审计写失败 → warn,不阻塞。

## Testing

- `tests/test_policy.py`:工具分档(名模式 + 路径归属)、工作区内 allow / 工作区外 ask、allowlist 命中与增长、`is_dangerous` 正则降级为 ask 规则。
- `tests/test_schema.py`:pydantic(native)+ jsonschema(MCP)校验。
- `tests/test_executor.py`:断言子进程 env 无 `OPENAI_API_KEY`、cwd 锁项目根、超时生效。
- `tests/test_audit.py`:决策 + outcome 落 jsonl。
- 扩 `tests/test_agent.py`:FakeLLM 发 `run_command cat ~/.ssh/id_rsa` → 断言 ask + (无 y)未执行 + 未 spawn 子进程 + 有审计条目;工作区内 read → allow + 执行。
- `tests/test_tools.py`:现有 `is_dangerous` 测试迁移为策略规则等价测试(`is_dangerous` 作为 ask 规则的输入保留)。

## 衡量口径(用户自行跑,本 spec 不执行)

M1 落地后,用户在 master(baseline)与本分支(after)各跑一次 promptfoo,看 per-category 攻击成功率 delta。预期下降的类别(因今天它们不进闸门、M1 后全落到 ask → 红队自动拒):

- **critical**:shell-injection、credential-exfil(工作区外读 / `cat` 凭证)
- **high**:self-modification(fs-write `cc_harness/`)、fs-overreach(工作区外写)
- **dynamic**:ssrf(`curl` 元数据)、data-exfiltration(出站)

## 文件清单

```
新增  cc_harness/{policy,schema,executor,audit}.py
新增  tests/test_{policy,schema,executor,audit}.py
改    cc_harness/{agent,tools,config,repl}.py  main.py
改    pyproject.toml(+jsonschema>=4)
新增  policy.yaml.example
不动  eval/(红队归用户)
```

## 不在本 spec 范围

- 内核级沙箱(gVisor/Firecracker)—— Linux 专属,M1 用可移植方案,`Executor` 接口预留。
- L2 输入防御(Prompt Guard / 指令层级)—— M2。
- L5 输出 DLP(Presidio)—— M3。
- L6 监控 + 数据流守卫 —— M4。
- 红队执行与 delta 脚本 —— 用户自行处理。
