# E5 round 2 — amendment (fix 6 final-review findings)

> **Status**: spec review (待用户审)
> **Date**: 2026-07-22
> **Branch**: `master`(本 amendment 不限分支,落地归 E5 round 2)
> **Author**: final whole-branch review (commit 6b21660) → 6 真 bug
> **Supersedes**: 部分 E5 spec 决策,见每 decision 的 "原 → 改" 注明

## Goal

修 E5 final review(`6b21660` commit)抓到的 1 Critical + 5 Important 真 bug,把 E5 branch 从 ❌ NOT ready to merge 推回 ✅ ready to merge。**不改目标本身**(漂移检测通路),只改实现细节。

## 现有代码事实(amendment 写入时核实)

- **E5 round 1 已完成 9 task + final review**:`6b21660` 是最后一个 commit,`e5-final-review.md` 列 6 bug
- **`MemoryStore.search_similar` 实际签名**(`store.py:250-280`):返 `list[tuple[Memory, float]]`,**不是** `list[Memory]` — T1.2 / T2.1 plan 字面假设错误,Critical miss
- **`ReflectionEngine._run_one.save` 写死 `source="reflection"`**(`engine.py:152-158`):所有 emit 事件(包括 drift_detected)都被存为 reflection,drift 隔离被破坏
- **`MemoryRetriever.search` 已拆 tuple**(`retriever.py:45` `[m for m, _ in weighted]`):只 `MemoryService.save` 漏拆,Critical bug 唯一修复点
- **`DriftDetector._check_groups` 算法简化**(`detector.py:115-149`):单组 binary `0.0/1.0`,spec m5 的 `inconsistent_groups / total_groups` 多组 ratio 未实现,ambig 档 unreachable
- **`DriftDetector._judge_group_consistency` 失败返 `(True, reason)`**(`detector.py:186-197`):parse_error / all_llm_unavailable 被当 healthy,drift_rate=0.0、severity=pos → 假阳性 + 漏 audit
- **`DriftDetector._ask_judge` fallback 死代码**(`detector.py:201`):第二轮 `(None, "local")` 永远失败;`main.py:262-274` 没传 local `LLMClient`
- **`_memory_service` 形参 declared but unused**(`detector.py:49`):T1.5 ledger 已记,T2.1 注入时未用它
- **计划 turn_idx 占位还在**:`service.py:101` `int(time.time() * 1000) % 1000`,`retriever.py:48` `0`(ledger 已知 Minor)

## 6 个 fix 决策(逐条)

### F1(Critical):写时 tuple 类型修复

**原 spec 决策 D1**:写时调 `check_after_write(new_memory, similar)`,similar 是 `list[Memory]`(plan 字面假设)
**改**:spec 显式声明 caller 必须把 `list[tuple[Memory, float]]` 解包为 `list[Memory]` 再传。`MemoryService.save` 加 1 行 `[m for m, _ in similar_for_conflict]`。**detector 形参保持 `list[Memory]` 不变**(语义清晰),caller 在边界解包。

**修法**:`service.py:97-105` 加 `mems_only = [m for m, _ in similar_for_conflict]`,然后传 `similar=mems_only`。Retriever 已正确(retriever.py:45)。

### F2(Important):`source="drift"` 真存机制

**原 spec 决策**:drift event 经 E2 reflection engine 写盘,自然走 `source='drift'`
**改**:E2 engine 当前硬编码 `source="reflection"`,不支持 source override。需给 `ReflectionEvent` 加可选 `source: str | None = None` 字段(默认 None → engine 用原 hardcoded "reflection"),`drift_detected` 工厂显式传 `source="drift"`。Engine.save 读 `event.source or "reflection"`。

**修法**:
- `reflection/events.py:ReflectionEvent` 加 `source: str | None = None` 字段
- `reflection/events.py:drift_detected` 工厂调 `ReflectionEvent(...)` 时传 `source="drift"`
- `reflection/engine.py:153-158` 把硬编码 `"reflection"` 改成读 `event.source or "reflection"`

### F3(Important):D5 真 local LLM fallback

**原 spec 决策 D5**:JUDGE 失败 → 退回本地 LLM,都失败 → noop + 审计
**改**:`DriftDetector.__init__` 加 `local_llm` 形参;`main.py:boot()` 把主 `llm` 透传给它(沿 E2 `_judge_llm` 同源模式)。`_ask_judge` 把死代码 `(None, "local")` 换成 `(self._local_llm, "local")`。

**修法**:
- `detector.py:__init__` 加 `local_llm=None` 形参,`self._local_llm = local_llm`
- `detector.py:_ask_judge` 把 fallback 第二轮换成 `self._local_llm`
- `main.py:262-274` 加 `_drift_detector = DriftDetector(..., local_llm=llm)`
- `tests/test_drift_detector.py` 加 1 测试:JUDGE 抛 → local LLM 接住

### F4(Important):`memory_service` 真正使用

**原 spec 决策**:DriftDetector 可访问 MemoryService 用于 fetch-by-id / 反查
**改**:实际 detector 没反查需求。**简化:移除 `memory_service` 形参**(spec 决策与实施均不需要)。`main.py:263-264` 同步移除 `_mem_deps["service"]` 注入。

**修法**:
- `detector.py:__init__` 删 `memory_service` 形参 + `self._memory_service = memory_service`
- `main.py:263-264` 删 `memory_service=_mem_deps["service"]` 注入
- 测试 fixture 同步删 `memory_service` 形参
- (可选) `service.py:80` similar_for_conflict 仍保留(供 E4 矛盾检测用,不在 DriftDetector 范围)

### F5(Important):consistency judge 失败真正的 fail-soft

**原 spec 决策 D7**:judge 失败 → noop + 审计,**不**视为 consistent
**改**:`_judge_group_consistency` 失败返 `(None, reason)`(不返 True 当 healthy);`_check_groups` 看到 None → 跳过 entity verdict,fall 到"all consistency judged failed" 路径追加 noop audit,spec D7 行为正确。

**修法**:
- `detector.py:_judge_group_consistency` 失败返 `(None, reason)`
- `detector.py:_check_groups` 增加 `consistency_failed: bool` 追踪 + 末尾若某 entity Judge fail 则 `consistency_failed = True`
- 末尾 `if consistency_failed` 写一行 "consistency_judge_failed" audit 区别于 all_llm_unavailable(更精确)
- `tests/test_drift_detector.py` 新增 test:consistency judge 返 parse_error → 不发 drift event + 落审计

### F6(Important):drift_rate 多组 ratio 算法

**原 spec 决策 D6**:`drift_rate = inconsistent_groups / total_groups`(m5 `eval/locomo/metrics.py:359-431`)
**改**:T1.2 实施员简化成单组 binary。修正:对每个 entity,**先按 record.group_key 分组**(m5 用 `(sample_id, ent_lower)`,E5 简化用 `text.strip().lower()` 作为 group_key),同 group_key 视为同一预测,跨 group 才需要判 consistency。每组跑一次 `JUDGE_GROUP_CONSIST`,`inconsistent_groups = consistent=False 的组数`,`total_groups = 总组数`,`drift_rate = inconsistent_groups / total_groups`。

**修法**:
- `detector.py:_check_groups` 改为:
  1. 对每个 entity 抽 records(mems)
  2. 按 `text.strip().lower()` 分组 → groups: `dict[str, list[Memory]]`
  3. `total_groups = len(groups)`
  4. 每组跑 JUDGE_GROUP_CONSIST(2 record 起步,< 2 跳过)
  5. `inconsistent_groups = sum(1 for c in consistencies if not c)`
  6. `drift_rate = inconsistent_groups / total_groups if total_groups > 0 else 0.0`
  7. DriftVerdict 含 真实 `total_groups` / `inconsistent_groups` / `drift_rate` ratio
- `tests/test_drift_detector.py` 加 1 多组测试:4 records "Caroline 1990" / "Caroline 1985" / "Caroline 1985" / "Caroline 1980" → 3 组,2 错,drift_rate=0.667 → severity=neg

## 不在本次范围

- ❌ turn_idx 从 repl 传真值(ledger 已知 Minor 7,F6 fix 已很高频,可后置)
- ❌ `sample_records` 过 L5(T1.5 ledger Minor 3)
- ❌ test_severity_neg_high_drift_rate / test_every_n_turns_throttling 断言加强(T1.2 review Minor)

## 风险

- F6 算法改 — 旧 T1.2 单组 binary 测试会失效,需重写 3-4 detector 测试的断言
- F2 ReflectionEvent 加 `source` 字段 — 6 个现有工厂(except drift_detected)不传 source,engine 行为不变
- F3 local_llm 注入 — main.py:llm 是真 OpenAI client,失败时进 fallback chain。需记 ops 跑 audit 行为差异

## 2 commit 摘要

```
# commit R1: feat(drift) — 算法 + 写时修复 + 一致性 fail-soft
  F1 tuple 修复 + F4 移除 _memory_service + F5 consistency fail-soft + F6 多组 ratio 算法

# commit R2: feat(drift) — source 隔离 + 真 local LLM
  F2 ReflectionEvent.source + F3 DriftDetector.local_llm 注入
```

R1 纯算法/数据流修正,R2 涉及 E2 引擎扩展(影响 E2 文档,需回归 E2 6 事件)。
