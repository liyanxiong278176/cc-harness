# Sub-E3: 跨 session 自动续接 — design

> **Status**: spec review (待用户审)
> **Date**: 2026-07-24
> **Branch**: `master`(本 spec 不限分支,merge 后归 E3)
> **Author**: brainstorm 7 轮澄清 + 5 段设计

## Goal

把当前 cc-harness 中"**session 完整上下文跨 session 重建**"从**无**升级为**显式契约**(E3 自动续接通路)。E3 沿用 D1-D5 / E1 / E2 / E4 / E5 既有契约(Plan3 压缩 / D1 subagent 8-status / E2 reflection 7-event / E5 drift 检测),不重新发明 REPL,不引入新子包(轻量加层),不引入新 LLM。

E3 把现有"半截持久化"(conversation 表 L0 切片 + memory.db L1/L2/L3 + todo yaml/md + 5 个 audit jsonl)补成"**全 LLM 上下文 checkpoint 通路**",让新 session 启动时:
1. 检测到上一 session 的 checkpoint → 按 Manifest `cross_session_mode` 决策
2. 完整 `state.messages` 回放(Plan3 压缩兜底)
3. 自动 `memory_recall` 召出 reflection / drift 记录
4. Tool list 变化 warn(不阻断)
5. in-progress subagent 标 cancelled + warn

## 现有代码事实(spec 写入时核实)

- **`repl.py:ReplState`** (lines 67-94) dataclass 11 既有 + 4 E1 / 1 M2 / 3 reject / 2 verify hook,共 **20 字段**;全 in-memory,无 `serialize()` / `deserialize()` 方法
- **`repl.py:run_repl`** (lines 160-512) 入口构造 ReplState(line 200-207)+ session_id 生成(`repl-{ts}-{8hex}`)+ finally 收尾(line 479-512)只 drain scheduler/reflection/drift,不保存 session 状态
- **`memory/store.py:conversation`** (lines 105-115) L0 录制,after-turn hook (`repl.py:530-535`) 写;**不**录 system message / multimodal list
- **`memory/store.py:140-145`** ALTER 迁移已就位,新增 2 表沿此 pattern
- **`project/models.py:Manifest`** (lines 197-215) 字段 8 个;`resume_mode` (ask/auto/manual) Literal 已锁
- **`repl.py:_maybe_ask_resume`** (lines 645-694) 已有交互 ask user 模式,沿此 pattern
- **`agent.py:_refresh_system_prompt`** (lines 837-913) 已有 `<resume_task>` block 注入,anchored regex strip 旧块;E3 加 `<cross_session_prior>` + `<cross_session_tools>` 2 块沿此 pattern
- **`mcp_client.py:list_tools`** (lines 174-175) 每次启动重新 list,tool schema 含 `name` + `params` OpenAI 格式
- **`cli/resume.py`** stub,`cli/resume.py:1-13` 自承认 "真正 attach to REPL 不在本任务范围" — 任务已 merge (Sub-project A Task 6),E3 是其超集

## 关键决策(brainstorm 7 轮)

### D1:范围边界

**A — 完整重现上次 session 的 LLM 上下文**(messages + system + ReAct 工具调用元数据全部持久化 + 反序列化)。不选 B(摘要式回放)/ C(最小化注入)/ D(不做)— Plan3 压缩已 work,完整 replay LLM 续聊体验最连贯。

### D2:持久化层

**A — SQLite 加表(与 memory.db 共库)**。沿 `conversation` 表 pattern + ALTER 迁移机制,不引新文件格式。不选 B(jsonl 灵活但 schema-less)/ C(污染 L1 atom 语义)/ D(markdown 解析成本高)。

### D3:压缩策略

**A — 完整 replay + 让 Plan3 压缩接管**。`state.messages` 全量回放后,context 走 Tier1 snip(头尾保留)/ Tier2 prune(整段砍)/ delta cap(超预算截断)三道兜底;不引 E3 专属 summarization。

### D4:触发时机

**C — Manifest 加 `cross_session_mode` 字段**(off / last_only / ask 三态)。与 `resume_mode` (ask/auto/manual) 模式一致,project 级配置可控。

### D5:E2/E5 整合

**B — 仅 recall,drift verdict 不进 memory**。新 session 启动时 `_attach_cross_session_context` hook 自动 `memory_recall(query=...)` 一次,按现有 vector search 召出(自然涵盖 reflection / drift 记录)。drift verdict 不强求进 memory(目前 audit jsonl 足够)。

### D6:subagent 跨 session

**C — 不续跑,标 cancelled + warn**。沿用 E1 `/reject` 模式:cancelled 不报错,LLM 下一轮 user turn 看到 todo 里 in-progress 子任务会自然重派。

### D7:MCP tool 兼容

**C — Tool whitelist 缓存 + hash 变化 warn**。checkpoint 时记录 tool list 快照(`{tool_name: {"hash": "sha256:...", "captured_at": iso_ts}}`),新 session 启动时拉新 tool list 比对 hash,变化则在 `<cross_session_tools>` block 显示 warn,**不**阻断 replay。

## 组件设计

### 改动点(全部增量,不改 D1 SubAgentRunner 主体)

```
cc_harness/
├── memory/store.py          [MODIFY]  +session_checkpoint +session_message 2 表 + ALTER 迁移
├── memory/checkpoint.py     [NEW]     CheckpointService(save/load_latest/load_messages)
├── project/models.py        [MODIFY]  +CrossSessionMode Literal + Manifest 字段
├── project/manifest.py      [MODIFY]  +cross_session_mode 校验
├── repl.py                  [MODIFY]  ReplState 加 5 字段 + finally save 钩子 + 启动 load 路径
├── agent.py                 [MODIFY]  run_turn 加 prior_messages + tool_diff 形参 + _refresh_system_prompt 注入 2 block
├── render.py                [MODIFY]  +print_cross_session_summary
└── main.py                  [MODIFY]  boot run_repl 透传 checkpoint_service + manifest
```

### 组件 1:`session_checkpoint` + `session_message` 表(memory/store.py)

```python
# cc_harness/memory/store.py 新增 2 表(沿 memories/conversation pattern)

CREATE TABLE IF NOT EXISTS session_checkpoint (
    session_id    TEXT PRIMARY KEY,
    project_root  TEXT,
    mode          TEXT NOT NULL,
    turn_counter  INTEGER DEFAULT 0,
    started_at    TEXT NOT NULL,
    ended_at      TEXT NOT NULL,
    cross_session_mode TEXT DEFAULT 'last_only',
    extra_json    TEXT NOT NULL DEFAULT '{}'
);

CREATE TABLE IF NOT EXISTS session_message (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id    TEXT NOT NULL,
    turn_idx      INTEGER NOT NULL,
    role          TEXT NOT NULL,
    content_json  TEXT NOT NULL,
    ts            TEXT NOT NULL,
    FOREIGN KEY (session_id) REFERENCES session_checkpoint(session_id) ON DELETE CASCADE
);

CREATE INDEX IF NOT EXISTS idx_session_message_session_turn
    ON session_message(session_id, turn_idx);
```

**关键设计**:
- `content_json` 存 OpenAI chat 单条 message 完整 JSON(含 tool_calls / function_call / multimodal list)— system / user / assistant / tool 4 种 role 全存(spec D1 完整 replay 锁)
- ALTER 迁移:`store.py:140-145` 加 schema_version 检查 + ALTER TABLE 加 2 表 + 建 index(沿现有 conversation ALTER 模式)
- `extra_json` 留给未来扩展
- `ON DELETE CASCADE`:删 checkpoint 自动删 messages(避免 orphan)

### 组件 2:`CheckpointService`(memory/checkpoint.py 新文件, ~150 行)

```python
# cc_harness/memory/checkpoint.py

class CheckpointRecord:
    """frozen dataclass,session checkpoint 元数据"""
    session_id: str
    project_root: Path
    mode: str
    turn_counter: int
    started_at: str
    ended_at: str
    cross_session_mode: str
    extra: dict

class CheckpointService:
    """Session 完整上下文的 save / load / list_recent。沿 memory 既有 pattern。"""
    
    def __init__(self, store: MemoryStore) -> None:
        self.store = store
    
    async def save(
        self,
        *,
        session_id: str,
        project_root: Path,
        mode: str,
        turn_counter: int,
        started_at: str,
        ended_at: str,
        cross_session_mode: str,
        messages: list[dict],
        extra: dict | None = None,
    ) -> None:
        """session 结束时调。1 个事务 + UPSERT。"""
    
    def load_latest(self, project_root: Path) -> CheckpointRecord | None:
        """查最近 1 个 checkpoint(按 ended_at DESC)。off mode 返回 None。"""
    
    def load_messages(self, session_id: str) -> list[dict]:
        """按 turn_idx 升序返回 messages list(可直接喂 state.messages)。"""
    
    def list_recent(self, project_root: Path, limit: int = 5) -> list[CheckpointRecord]:
        """post-merge CLI 用,本期不接。"""
```

### 组件 3:`cross_session_mode` Manifest 字段(project/models.py + manifest.py)

```python
# project/models.py
class CrossSessionMode(str, Enum):
    OFF = "off"
    LAST_ONLY = "last_only"
    ASK = "ask"

# Manifest dataclass 加:
cross_session_mode: CrossSessionMode = CrossSessionMode.LAST_ONLY

# project/manifest.py 加校验
_VALID_CROSS_SESSION_MODES = {"off", "last_only", "ask"}
if raw_cross_session_mode not in _VALID_CROSS_SESSION_MODES:
    raise ConfigError(f"cross_session_mode 必须是 {sorted(_VALID_CROSS_SESSION_MODES)},当前 {raw_cross_session_mode!r}")
```

### 组件 4:`ReplState` checkpoint 字段 + finally 钩子 + 启动 load(repl.py)

```python
# cc_harness/repl.py:ReplState 加字段:
checkpoint_service: "CheckpointService | None" = None
checkpoint_path: Path | None = None
last_loaded_session_id: str | None = None
tool_hash_snapshot: dict[str, str] = field(default_factory=dict)
cross_session_tools_diff: list[str] = field(default_factory=list)

# cc_harness/repl.py:run_repl 启动路径(turn==0 时):
async def _maybe_load_cross_session(state, console, mcp, mode):
    """按 manifest.cross_session_mode 决策 + 加载旧 session 上下文。"""
    if state.manifest.cross_session_mode == CrossSessionMode.OFF:
        return
    candidate = state.checkpoint_service.load_latest(state.project_root)
    if candidate is None:
        return
    if state.manifest.cross_session_mode == CrossSessionMode.ASK:
        # 交互问 user,沿 _maybe_ask_resume 模式
        ...
    # 静默 / 已确认 → load
    state.messages = state.checkpoint_service.load_messages(candidate.session_id)
    state.last_loaded_session_id = candidate.session_id
    state.mode = candidate.mode
    state.turn_counter = 0
    # tool hash diff
    new_tools = await mcp.list_tools()
    state.tool_hash_snapshot = {t.name: sha256(json.dumps(t.params, sort_keys=True)) for t in new_tools}
    state.cross_session_tools_diff = _diff_tool_hash(
        old=candidate.extra.get("tool_hash_snapshot", {}),
        new=state.tool_hash_snapshot,
    )
    print_cross_session_summary(console, candidate, state.cross_session_tools_diff)

# cc_harness/repl.py:run_repl finally:
finally:
    if state.checkpoint_service is not None and state.messages:
        await state.checkpoint_service.save(
            session_id=state.session_id,
            project_root=state.project_root,
            mode=state.mode,
            turn_counter=state.turn_counter,
            started_at=state.started_at,
            ended_at=datetime.now().isoformat(),
            cross_session_mode=state.manifest.cross_session_mode.value,
            messages=state.messages,
            extra={"tool_hash_snapshot": state.tool_hash_snapshot, ...},
        )
    # ... 既有 4 op drain ...
```

### 组件 5:`prior_messages` 透传 + `<cross_session_tools>` block(agent.py)

```python
# cc_harness/agent.py:_refresh_system_prompt 加 prior_messages + tool_diff 形参:

def _refresh_system_prompt(
    messages, cwd, mode,
    extra_ctx=None, resume_task=None, todo_hints=None,
    prior_messages: list[dict] | None = None,  # E3
    tool_diff: list[str] | None = None,  # E3 D7
):
    ...
    if tool_diff:
        tool_block = "\n<cross_session_tools>\n" + "\n".join(tool_diff) + "\n</cross_session_tools>\n"
        if messages and messages[0].get("role") == "system":
            messages[0]["content"] += tool_block

# cc_harness/agent.py:run_turn 加 prior_messages + tool_diff 形参:
async def run_turn(
    ..., prior_messages: list[dict] | None = None,
    tool_diff: list[str] | None = None,
):
    ...
```

### 组件 6:`_cross_session_prior` section(_refresh_system_prompt)

```python
def _cross_session_prior(ctx: dict) -> str | None:
    """E3 D1/D3:prior_messages 摘要注入。
    
    Gate:e3_prior_messages flag + mode==coding。
    """
    prior = ctx.get("e3_prior_messages")
    if not prior:
        return None
    if ctx.get("mode") != "coding":
        return None
    summary = _summarize_prior(prior)  # 取 system + 最近 5 轮 + 中间压缩
    return f"\n<cross_session_prior>\n{summary}\n</cross_session_prior>\n"

# SECTION_POOL 注册
SECTION_POOL = [
    ...,
    ("decomposition_hint", _decomposition_hint, "e1_decompose_hint"),
    ("cross_session_prior", _cross_session_prior, "e3_prior_messages"),
]
```

### 组件 7:`print_cross_session_summary`(render.py)

```python
def print_cross_session_summary(
    console: Console,
    candidate: CheckpointRecord,
    tool_diff: list[str],
    in_progress_subagents: list[str],
) -> None:
    """新 session 启动时,若 load 了旧 session 上下文,渲染摘要。"""
    lines = [
        f"🔁 续接上次 session({candidate.session_id}):",
        f"  • 模式: {candidate.mode}",
        f"  • 轮次: {candidate.turn_counter}",
        f"  • 结束: {candidate.ended_at}",
    ]
    if tool_diff:
        lines.append(f"  • 工具变更: +{[d for d in tool_diff if d.startswith('+')].__len__()} -{[d for d in tool_diff if d.startswith('-')].__len__()}")
    if in_progress_subagents:
        lines.append(f"  • 上次 fan-out 中断的 subagent:{len(in_progress_subagents)} 个已标 cancelled")
    console.print("\n".join(lines), markup=False)
```

### 组件 8:`main.py` 透传

```python
# main.py:boot() run_repl 调用加 2 形参
await run_repl(
    ..., e1_decompose_enabled=_policy.e1_decompose_enabled,
    checkpoint_service=_checkpoint_service,  # E3
    manifest=manifest,  # E3
)
```

## 接口规格

### `CheckpointService` 接口(verbatim from 组件 2)

| 方法 | 输入 | 返回 | 说明 |
|---|---|---|---|
| `save` | `session_id` / `project_root` / `mode` / `turn_counter` / `started_at` / `ended_at` / `cross_session_mode` / `messages` / `extra?` | None | 1 事务,UPSERT + INSERT |
| `load_latest` | `project_root` | `CheckpointRecord \| None` | 按 `ended_at DESC LIMIT 1` + project_root 过滤 |
| `load_messages` | `session_id` | `list[dict]` | 按 `turn_idx ASC` 返回完整 OpenAI chat format |
| `list_recent` | `project_root` / `limit=5` | `list[CheckpointRecord]` | post-merge CLI 用,本期不接 |

### `ReplState` 新字段(verbatim from 组件 4)

| 字段 | 类型 | 默认 | 说明 |
|---|---|---|---|
| `checkpoint_service` | `CheckpointService \| None` | None | main.py:boot() 构造注入 |
| `checkpoint_path` | `Path \| None` | None | 备用物理路径 |
| `last_loaded_session_id` | `str \| None` | None | 启动时 load 的旧 session_id |
| `tool_hash_snapshot` | `dict[str, str]` | {} | 当前 mcp tool hash 快照(D7) |
| `cross_session_tools_diff` | `list[str]` | [] | warn 渲染用 |

### `agent.run_turn` 新形参

| 形参 | 类型 | 默认 | 说明 |
|---|---|---|---|
| `prior_messages` | `list[dict] \| None` | None | 沿 repl → agent.py 透传(本期先不喂 state.messages,只 extra_ctx 注入) |
| `tool_diff` | `list[str] \| None` | None | 注入 `<cross_session_tools>` block |

### `Manifest.cross_session_mode`(verbatim from 组件 3)

```python
class CrossSessionMode(str, Enum):
    OFF = "off"
    LAST_ONLY = "last_only"
    ASK = "ask"
```

`policy.yaml` 不需新增(cross_session_mode 是 Manifest 配置,不是 L4 闸门)。`.cc-harness/project.yaml`:
```yaml
cross_session_mode: last_only  # off / last_only / ask
```

### `_cross_session_prior` extra_ctx

| key | 类型 | 触发 |
|---|---|---|
| `e3_prior_messages` | `list[dict] \| None` | `cross_session_mode != "off"` AND `mode == "coding"` |

### SQLite schema(verbatim from 组件 1)

| 表 | 列 |
|---|---|
| `session_checkpoint` | session_id (PK) / project_root / mode / turn_counter / started_at / ended_at / cross_session_mode / extra_json |
| `session_message` | id (PK) / session_id (FK CASCADE) / turn_idx / role / content_json / ts |

INDEX: `idx_session_message_session_turn (session_id, turn_idx)`

### spec 决策 ↔ 组件映射(回查表)

| 决策 | 实现位置 |
|---|---|
| D1 完整 messages replay | `CheckpointService.save` + `load_messages` |
| D2 SQLite 加表 | `memory/store.py` ALTER 迁移 |
| D3 Plan3 压缩接管 | `_refresh_system_prompt` 注入 prior_messages → 走 Plan3 Tier1/Tier2 |
| D4 Manifest `cross_session_mode` | `project/models.py` + `manifest.py` + `_maybe_load_cross_session` |
| D5 memory_recall 自动召出 | `repl.py:run_repl` 启动时 1 次,query=project name |
| D6 subagent cancelled + warn | `_maybe_load_cross_session` 标 cancelled + summary warn |
| D7 tool hash diff warn | `_diff_tool_hash` + summary warn |

## 测试策略

### 单元测试(6-8 个)

| 测试目标 | 文件 | 关键 case |
|---|---|---|
| `CheckpointService.save` + `load_messages` round-trip | `tests/test_memory_checkpoint.py` | save 5 messages → load → 全字段等值(含 tool_calls / multimodal) |
| `load_latest` 按 `ended_at DESC` + `project_root` 过滤 | 同上 | 同 project 2 session → 最近 1;不同 project → None |
| `_cross_session_prior` section 渲染 | `tests/test_prompts.py` | prior_messages 注入 → 含 "## 跨 session" / 5 轮截断 / system 不渲染 |
| `_diff_tool_hash` 加减 | `tests/test_memory_checkpoint.py` | old={A,B,C} new={B,C,D} → +D -A,无差异 → [] |
| `_maybe_load_cross_session` mode 决策 | `tests/test_repl.py` | off 不调 / last_only 静默 load / ask ask user + 拒绝 |
| `Manifest.cross_session_mode` Literal 校验 | `tests/test_project_manifest.py` | 非法值 → ConfigError / 默认 last_only / yaml 透传 |
| in-progress subagent cancelled | `tests/test_repl.py` | load candidate 有 in_progress subagent → state.subagent_cancelled list |
| tool_diff warn 渲染 | `tests/test_render.py` | `print_cross_session_summary` 输出含 "工具变更: +X -Y" |

### 集成测试(3 个)

| 测试 | 文件 |
|---|---|
| 完整 round-trip:session A save → session B load → 沿 Plan3 压缩 | `tests/test_e3_integration.py` |
| E2 reflection 跨 session 召出(load 完调 `memory_recall(query="proj-x")` → 命中 source='reflection') | 同上 |
| E1 `/reject` 状态跨 session 不复活(load 后 `decomposition_rejected=False` 但 `last_decomp_summary=None`) | 同上 |

### E2E(1 个,gated)

| 测试 | 文件 |
|---|---|
| 真 LLM session A (3 轮) → save → session B (1 轮续接) → B 的 system prompt 含 A 的最后 user message | `tests/_test_e3_e2e.py`(双 env 守卫) |

## 风险

- **Plan3 压缩介入时 prior_messages 摘要可能丢关键指令**(D3 代价)— `<cross_session_prior>` block 走 SECTION_POOL 末尾,Plan3 Tier2 prune 默认从尾部砍;E3 加 "prior block 优先级高" hint 待 post-merge 验证
- **ALTER 迁移与现有 conversation 表冲突** — 沿 `store.py:140-145` 已存在的 schema_version 检查 pattern;新表不动现有 schema
- **多项目混用 memory.db** — `load_latest` 过滤 `project_root`;新表加 `project_root` 列
- **MCP tool schema hash 算到参数内部细节变化**(minor change 也算 +X -Y)— warn 文案明确"工具 N 个变更",LLM 自评;不阻断
- **session checkpoint 体积爆炸**(长 session 100+ 轮,可能 10MB+)— checkpoint save 不压缩,Plan3 压缩只在 _refresh_system_prompt 时介入;disk size 可接受
- **in-progress subagent cancelled 信息丢失** — `_print_cross_session_summary` 显示 "上次 fan-out 中断的 subagent:N 个已标 cancelled",LLM 下轮可主动 `todo_list` 查
- **messages 反序列化 multimodal / function_call** — save 时 `json.dumps` 完整 message dict;round-trip test 必覆盖
- **System message 含 `<resume_task>` 跨 session stale** — `_refresh_system_prompt` 已有 anchored regex strip 旧 block(E2 验证);E3 复检 + strip `<cross_session_prior>` 旧 block

## 不做(YAGNI)

- ❌ 不引入 `state.serialize()` 全 dataclass 序列化(改 1 处不解决问题)
- ❌ 不实现 `CheckpointService.list_recent` CLI 入口
- ❌ 不实现 drift verdict 进 memory(spec D5 B)
- ❌ 不实现 subagent 续跑(只 cancelled + warn)
- ❌ 不做 LLM summarization(spec D3 A,完整 replay + Plan3 兜底)
- ❌ 不实现 cross_session_mode `always`(只 off / last_only / ask 三态)
- ❌ 不做 tool schema 兼容性校验(只 hash diff warn)
- ❌ 不做 cross_session_mode 与 `resume_mode` 二选一覆盖(两者独立)
- ❌ 不引入新 LLM API(沿用现有 LLMClient)
- ❌ 不动 Plan3 压缩代码
- ❌ 不动 E2 reflection 7-event / E5 drift 检测通路
- ❌ 不动 D1 subagent 8-status 契约
- ❌ 不做 checkpoint 加密 / 签名
- ❌ 不做 cross-session 与沙箱模式(SandboxExecutor)整合(sandbox 重启 = subagent 全清,与 E3 互斥,post-merge)

## 成功判定

- spec D1-D7 7 决策全部落地
- 单元 + 集成 + E2E 测试全过
- 13 pre-existing failure 持平,0 新失败
- ruff clean
- 全部 commit reviewed APPROVED,final whole-branch review Ready to merge
