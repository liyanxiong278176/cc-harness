# Sub-F: E3 hardening + Standards 偏硬项修真 — design

> **Status**: spec review (待用户审)
> **Date**: 2026-07-25
> **Branch**: `master`(本 spec 不限分支,merge 后归 F)
> **Author**: code-review 两轴聚合 + Explore 精确位置映射
> **Trigger**: `/code-review` of E3 (9c07af1..f0ff480) → Standards 7 findings + Spec 6 findings (Request Changes)

## Goal

把 E3(自动续接)从"测试通过但生产多半失效"修真为"生产路径真接通",同时清理 3 项 Standards 偏硬 smell(checkpoint_path 死字段、last_loaded_session_id 接入审计、agent.py 内联 import 提到模块级)。F 不重写 E3,不引入新子包,不引入新依赖,不引入新 LLM。

F 修真 6 项(2 Blocking + 3 Important + 1 偏硬),把 E3 从 Request Changes 升级到 Ready to merge,并清理 Standards 硬性遗留。

## 现有代码事实(spec 写入时 Explore 核实)

- **`cc_harness/mcp_client.py:174-175`** `list_tools` 是**同步**方法,返回 `list[dict]`(OpenAI tool schema 形态:`{"type": "function", "function": {"name": ..., "parameters": ...}}`);**无 async sibling**
- **`cc_harness/repl.py:783-790`** 调用 `await mcp.list_tools()` + `{t.name: _sha256_of_tool(t) for t in new_tools}`,两层错(同步方法被 await、dict 无 `.name` 属性);异常被裸 `except` 静默吞掉;`repl.py:816-820` `_sha256_of_tool` 用 `getattr(tool, "params", {})` 同样对 dict 失效;SHA256 被截成 16 hex(违反 spec D7 字面 lock `{hash: "sha256:..."}` 全 64 hex)
- **`cc_harness/repl.py:459-479`** `run_repl` 内调 `run_turn` **未传** `state.cross_session_tools_diff`,虽然 `agent.py:843-848, 268-284` `_refresh_system_prompt` 与 `run_turn` 形参已就位 — E3 工具块在生产永远是死路径
- **`cc_harness/repl.py:503-524`** finally save extra 仅 `{"tool_hash_snapshot": state.tool_hash_snapshot}`,**未保存** `subagent_cancelled` / `in_progress_subagents`;`repl.py:791-792` load 端读得到但生产永远写不出 — D6 `cancelled` 警告无真实来源
- **`cc_harness/project/subagent.py:471-498`** 结束时只读 Todo 实际状态,全文件没有 `todo_service.update(..., cancelled)` 自动 wiring;LLM 必须手动调 `todo_update` 才能动状态
- **`main.py:326-340`** `run_repl(..., manifest=None)` 固定写死;`boot()` 没有 manifest 局部变量;`repl.py:171-224, 296-318` 内部自动加载 manifest 并覆盖传入值 — 参数实际近似死参数,生产不挂但语义不诚
- **`cc_harness/memory/store.py:46-54`** 连接打开后**无** `PRAGMA foreign_keys=ON`;`store.py:149-207 _migrate()` 仅用 `PRAGMA table_info`;全仓生产代码无 FK pragma;`tests/test_memory_store_schema.py:46-68` 测试体手工开启,掩盖生产 orphan 风险
- **`cc_harness/project/manifest.py:271-297 _manifest_to_yaml()`** 未序列化 `cross_session_mode`;`save_manifest` 不能 round-trip,重载后回落 `last_only` 默认值;`tests/test_project_manifest.py:466-498` 仅加载手写 YAML,无 round-trip 测试
- **`cc_harness/repl.py:99`** `checkpoint_path: Path | None = None` 声明但生产从未读写(Speculative Generality)
- **`cc_harness/repl.py:100`** `last_loaded_session_id` 777 行赋值,生产从未读取(死字段)
- **`cc_harness/agent.py:983-991`** 内联 `import re as _re` 在 `_refresh_system_prompt` 内编译 `<cross_session_tools>` block 注入正则;同文件已有模块级预编译 pattern `_SUBAGENT_HINTS_RE:66-69` 模式不一致

## 关键决策(brainstorm 2 轮)

### F-D1:D7 tool-diff 真接生产

**A — 全链路修真,不绕过。** 4 处生产错误全部修:
1. `mcp_client.py:174 list_tools` 加 `async` 关键字(内部仍同步返回缓存的 `list(self._tools)` — 最简包装,保持 sync 缓存契约)
2. `repl.py:783-790` 取 `t["function"]["name"]` + `t["function"].get("parameters", {})`(OpenAI dict 形态),移除裸 except,改显式 try/except + `print_warn`
3. `_sha256_of_tool` SHA256 `hexdigest()[:16]` → `hexdigest()`(spec D7 字面 lock 全 64 hex)
4. `repl.py:459-479 run_repl` 调 `run_turn` 必须传 `state.cross_session_tools_diff`
5. 首次 session(candidate=None)也要采集快照:`old_hash = {}` → diff = 全部 `+X`(spec 接受,确保 tool 块总是有内容或显式空)

不选 B(改 repl.py 不 await + 假设 mcp_client sync)— 改 repl.py 一处可解第一错,但 D1 修 SHA256/dict/except 三处仍要动 repl.py,只改 mcp_client.py 单文件更对齐"最简包装"原则,且保持 repl.py `await` 异步契约与文件其他 `await mcp.*` 调用风格一致。

### F-D2:D6 cancelled 真标(双端接入)

**A — TodoService 加 list_in_progress + mark_cancelled,repl.py save/load 双端 wiring。** 4 步:
1. `cc_harness/project/service.py` 加 `list_in_progress() -> list[str]`(扫所有 non-terminal 状态 todo)+ `mark_cancelled(ids: list[str]) -> None`(批量标 cancelled)
2. `repl.py:503-524` finally save extra 加 `"in_progress_subagents": service.list_in_progress()` 实时扫
3. `repl.py:789-792` load 端:`candidate.extra.get("in_progress_subagents")` 非空 → `service.mark_cancelled(ids)` + `print_warn("以下 subagent 跨 session 中断,标 cancelled: ...")`
4. `project/subagent.py` 不动 — runner 不自动改 todo 状态(避免覆盖 E1 todo_create / LLM `todo_update` 契约);状态转移完全由 `mark_cancelled` 集中入口负责

不选 B(只加 warn 不改状态)— 半截修真,LLM 下一轮还看到 pending 状态会困惑;不选 C(扩展 runner)— 越界改 D1 契约。

### F-D3:main.py:338 manifest 参数简化

**A — 删除显式 `manifest=None`,repl.py 内部自动加载,加注释说明设计意图。** 修 `main.py:326-340`:`run_repl(...)` 调用删 `manifest=None` 行,加 `# manifest 省略:repl.py:run_repl 内部自动从 .cc-harness/project.yaml 加载/创建(见 repl.py:_load_or_create_manifest)`。`repl.py:run_repl` 签名**保留** `manifest=` 参数(向后兼容未来外部显式传,如 CLI `init` / `resume` 路径)。

不选 B(boot 内构造 manifest 局部变量再传)— 无业务价值(REPL 内部已重读);不选 C(完全删 `run_repl` 的 manifest 形参)— 破坏 CLI 兼容。

### F-D4:FK pragma 生产启用

**A — `store.py` 初始化连接后立即 `PRAGMA foreign_keys = ON` 一次。** 修 `cc_harness/memory/store.py:46-54`:开连接后 `await self._db.execute("PRAGMA foreign_keys = ON")`。`tests/test_memory_store_schema.py:46-68` 既有手工开 PRAGMA 验证 cascade 的测试**保留**(验证 cascade 仍工作)— 但加一个新测试 `test_store_enables_fk_on_open()` 验证自动开启。

不选 B(每连接/每次 op 都开)— over-engineer;PRAGMA 是 connection-scoped,连接级一次性足够。

### F-D5:manifest YAML round-trip

**A — `_manifest_to_yaml()` 加 `cross_session_mode` 字段 + round-trip 测试。** 修 `cc_harness/project/manifest.py:271-297`:`cross_session_mode: str` 序列化,沿 `_manifest_from_yaml` 解析路径(已支持)。`tests/test_project_manifest.py:466-498` 区域加测试 `test_save_load_manifest_round_trip_cross_session_mode()`:Manifest 设 ASK → save_manifest → load_manifest → assert 仍是 ASK。

不选 B(只补 YAML 不补测试)— 缺防退化防线;不选 C(只加测试不修实现)— 测必红,无意义。

### F-D6:Standards 偏硬项修真

**A — checkpoint_path 删,last_loaded_session_id 接入 audit,agent.py 内联 import 提到模块级。** 3 步:
1. `repl.py:99` 删 `checkpoint_path: Path | None = None` 字段(Speculative Generality,从未读写)
2. `repl.py:100` `last_loaded_session_id` 在 `_maybe_load_cross_session` load 成功后调 `cc_harness/audit.py:log_decision("session_resume", session_id=state.last_loaded_session_id, ...)`,把死字段变审计事件源
3. `cc_harness/agent.py` 顶部加 `_CROSS_SESSION_TOOLS_BLOCK_RE = re.compile(r"\n<cross_session_tools>.*?</cross_session_tools>\n", re.DOTALL)`,`_refresh_system_prompt` 内用预编译 pattern.sub(),移除内联 `import re as _re`

不选 B(只删不接)— 字段仍死,审计信号丢;不选 C(只接不删)— 仍留 Speculative Generality 字段名误导。

## 组件设计

### 改动点(全部增量,不改 E3 主体)

```
cc_harness/
├── memory/store.py            [MODIFY]  +PRAGMA foreign_keys=ON(46-54)
├── project/service.py         [MODIFY]  +list_in_progress() + mark_cancelled()
├── project/manifest.py        [MODIFY]  +_manifest_to_yaml cross_session_mode(271-297)
├── mcp_client.py              [MODIFY]  +async list_tools wrapper(174-175)
├── repl.py                    [MODIFY]  删 checkpoint_path + audit log_decision + D6 双端接入 + run_turn 透传 tool_diff + 首次 snapshot + SHA256 全长
├── agent.py                   [MODIFY]  +_CROSS_SESSION_TOOLS_BLOCK_RE 模块级预编译(顶部) + _refresh_system_prompt 用预编译
├── main.py                    [MODIFY]  删显式 manifest=None(326-340)
└── audit.py                   [MODIFY]  +log_decision 支持 session_resume event type(若尚未支持;否则只调既有接口)

tests/
├── test_memory_store_schema.py    [MODIFY]  +test_store_enables_fk_on_open
├── test_project_manifest.py       [MODIFY]  +test_save_load_manifest_round_trip_cross_session_mode
├── test_project_service.py        [MODIFY]  +list_in_progress + mark_cancelled round-trip 测试
├── test_repl.py                   [MODIFY]  修正 mock AsyncMock return_value dict 形态 + D6 warn + cancelled 测试 + audit log 测试
└── test_agent.py                  [MODIFY]  验证 _CROSS_SESSION_TOOLS_BLOCK_RE 模块级预编译引用
```

### 组件 1:mcp_client async wrapper + dict name access(F-D1)

```python
# cc_harness/mcp_client.py:174-175
async def list_tools(self) -> list[dict]:
    return list(self._tools)
```

OpenAI tool dict 形态访问:`t["function"]["name"]` + `t["function"].get("parameters", {})`。

### 组件 2:repl.py tool-diff 真接生产 + 首次 snapshot + run_turn 透传(F-D1 + F-D6 step 3)

```python
# repl.py:783-790 — 修真
try:
    new_tools = await mcp.list_tools()
    new_hash = {t["function"]["name"]: _sha256_of_tool(t) for t in new_tools}
    state.tool_hash_snapshot = new_hash
    old_hash = candidate.extra.get("tool_hash_snapshot") if candidate else {}
    state.cross_session_tools_diff = _diff_tool_hash(old_hash, new_hash)
except Exception as e:
    print_warn(f"tool hash 采集失败:{e}")
    state.tool_hash_snapshot = {}
    state.cross_session_tools_diff = []

# repl.py:459-479 run_turn 调用加 tool_diff 透传
state.messages = await run_turn(
    ...,
    tool_diff=state.cross_session_tools_diff,
)
```

### 组件 3:TodoService list_in_progress + mark_cancelled(F-D2)

```python
# cc_harness/project/service.py
class TodoService:
    _TERMINAL_STATUSES = {"completed", "cancelled", "skipped"}
    
    def list_in_progress(self) -> list[str]:
        """返回所有 non-terminal 状态 todo id。"""
        ...
    
    def mark_cancelled(self, ids: list[str]) -> None:
        """批量标 cancelled;若 todo 不存在或已 terminal,no-op。"""
        ...
```

### 组件 4:repl.py D6 双端接入 + audit log(F-D2 + F-D6 step 2)

```python
# repl.py:503-524 finally save 修真
extra = {
    "tool_hash_snapshot": state.tool_hash_snapshot,
    "in_progress_subagents": state.todo_service.list_in_progress() if state.todo_service else [],
}

# repl.py:789-792 load 端修真
state.subagent_cancelled = list(candidate.extra.get("in_progress_subagents", []))
if state.subagent_cancelled and state.todo_service:
    state.todo_service.mark_cancelled(state.subagent_cancelled)
    print_warn(f"以下 subagent 跨 session 中断,标 cancelled: {state.subagent_cancelled}")

# audit log_decision("session_resume", session_id=state.last_loaded_session_id)
log_decision(state.audit_log_path, "session_resume", session_id=state.last_loaded_session_id, ...)
```

### 组件 5:store.py FK pragma(F-D4)

```python
# cc_harness/memory/store.py:46-54 修真
self._db = await aiosqlite.connect(str(db_path))
await self._db.execute("PRAGMA foreign_keys = ON")
```

### 组件 6:manifest.py YAML cross_session_mode(F-D5)

```python
# cc_harness/project/manifest.py:271-297 _manifest_to_yaml 修真
def _manifest_to_yaml(m: Manifest) -> dict:
    return {
        "project_id": m.project_id,
        "name": m.name,
        ...,
        "cross_session_mode": m.cross_session_mode.value,  # 新增
    }
```

## Out of scope(deferred)

- **D5 recall 注入 session 上下文(Spec Important #3)** — 需要新设计"memory context surface"路径(messages 注入策略 + token budget),非简单 cleanup;留作未来 sub-project
- **`_test_e3_e2e.py` 真 LLM E2E(Spec 测试缺口 #6)** — 需要 sandbox + 真 key,投入大;沿 E1/E5 stub 模式,等用户单独决定
- **Standards 4 个判断 smell**(Duplicated Code `_resolve_cross_session_mode` / Repeated Switches `ToolDiffEntry` / Mysterious Name `_to_record` 类型 / Mysterious Name prompts docstring)— 建议修但不紧急;入 ledger 不入本 sub-project

## Cross-cutting 不破承诺

- D1 SubAgentRunner 8-status 契约 — F 不动 subagent.py 主体,F-D2 仅加 service 接口
- E1 /reject + decomposition hint — F 不动 prompts / agent 分解路径
- E2 reflection 7-event pipeline — F 不动 reflection engine
- E5 drift detection — F 不动 drift detector(但 E5.T2.1 已注入 MemoryService.save/Retriever.search 到 drift,F-T3 list_in_progress 不与 drift 冲突 — drift 走 memory 层,todo 走 service 层)
- L2 输入防御 / L4 权限闸门 / L5 输出 DLP / L8 沙箱 — F 不引入新 L 层
- Plan3 压缩(Tier1 snip / Tier2 prune / delta cap)— F 不引新压缩路径
- CrossSessionMode 字面 lock:off/last_only/ask(无 always)— F-D5 YAML 序列化沿用
- aiosqlite 异步 pattern:`await self._db.execute(...)`(F-D4 新增 PRAGMA 沿此)
- Windows 强制 UTF-8 编码:全部 Python 命令加 `PYTHONIOENCODING=utf-8`
- Pre-existing 13 baseline failures(test_strategies_yaml 4 + test_attacks_exec + test_attacks_yaml + test_promptfoo_configs 6 + test_agent 2)— 不破不修

## 待用户确认

- [ ] 6 项 F-D1..F-D6 范围锁定?
- [ ] 修真后立即合并 master 还是先 sub-branch review?
- [ ] Out of scope 3 项(D5 recall 注入 / 真 LLM E2E / Standards 4 judgement smell)暂不入 F,确认?