# E5 round 3 — amendment (fix 5 minor ledger items)

> **Status**: spec review (待用户审)
> **Date**: 2026-07-22
> **Branch**: `master`(本 amendment 不限分支,落地归 E5 round 3)
> **Author**: round 2 final review 留 ledger
> **Supersedes**: 无(纯 ledger 清理,不改 spec 决策)

## Goal

清 E5 round 2 final review ledger 的 5 minor(`.superpowers/sdd/progress.md` E5 round 2 末),branch 留 ledger 数量从 5 → 0。**不改 spec 决策或 D1-D7 实现**,只清账。

## 现有代码事实(amendment 写入时核实)

- **E5 round 2 已 ready to merge**(commit 7330a21),5 minor 是 known gap 不影响最终 acceptance
- **`ReplState` 现有字段见 `cc_harness/repl.py:67-87`**:已有 dataclass 字段包括 messages / mode,但**无 turn counter 字段**
- **`MemoryService.save` 无 turn_idx 形参**:`service.py:34-110` 当前 turn_idx 在 service.py:104 硬编码 `int(time.time()*1000) % 1000`
- **`MemoryRetriever.search` 无 turn_idx 形参**:`retriever.py:27-39`,retriever.py:48 硬编码 `0`
- **`cc_harness/reflection/engine.py:_run_one` line 129-136 重建 `ev_safe`** 不带 `source=` kwarg(虽然 R2.2 后续 `save` 调用读 `event.source` 不影响,但 round 2 final review minor 留 ledger)
- **`DriftDetector._emit_drift` 未对 `sample_records` 过 L5**:detector 仅对 entity text 走 `l5.sanitize`,但 records 的 `m.text` 没 sanitize
- **`tests/test_drift_detector.py::test_severity_neg_high_drift_rate` 断言偏弱**:`severity in {neg, ambig, pos}` 应断言具体预期 severity
- **`tests/test_drift_detector.py::test_every_n_turns_throttling` 断言偏弱**:`>=` 应为 `== 0` / `>= 1` 精确
- **`tests/_test_drift_e2e.py` 是占位**:`pytest.skip("E2E 占位 — ...")` 双 OPENAI_API_KEY / EMBEDDING_API_KEY 守卫

## 5 minor fix 决策(逐条)

### M1:`ev_safe` 重建补 `source` 字段

**修法**:`cc_harness/reflection/engine.py:_run_one` line 129-136 加 `source=event.source` 一行(若 `event.source is None`,ev_safe.source 是 None,符合默认行为)。

**风险**:无副作用,纯补字段。

### M2:turn_idx 从 repl 注入到 service/retriever

**修法**:
1. `ReplState`(repl.py:67)加 `turn_counter: int = 0`
2. `run_repl` 主循环每轮 `state.turn_counter += 1`
3. `MemoryService.save` 加 `turn_idx: int | None = None` 形参(默认 None 时仍占位)
4. `MemoryRetriever.search` 加 `turn_idx: int | None = None` 形参(同上)
5. repl.py 在 `_after_turn_memory` 调用 save 时透传 `state.turn_counter`
6. agent.py run_turn 在 memory_recall 时透传 turn_idx(若 memory_recall handler 支持)

**简化**:**M2 仅做 1+2(ReplState 计数 + run_repl 递增)+ 3+4(service/retriever 接受 turn_idx 形参,默认 None 时退回旧占位逻辑)**,agent.py 透传**留 post-merge**(agent.py 不在 E5 spec 范围,e2 reflection 已经管 emit)。这样每轮 turn counter 真实递增,drift 检测频率守门 `_should_run` 正常工作。

**测试**:repl 测试加 1 测试 mock turn counter 递增。

### M3:`sample_records` 文本过 L5 sanitize

**修法**:`cc_harness/drift/detector.py:_check_groups` 在构造 `sample_records = [{"id": m.id, "text": m.text} for m in mems[:10]]` 后过 `l5.sanitize`(已存 `self._l5`),apply 到每条 `text`。

**测试**:1 测试 mock L5 替换 entity/record text,验 detector 传的 records 已被 sanitize。

### M4:测试断言加强

**修法**:
- `test_severity_neg_high_drift_rate`:断言 severity 应是 `ambig`(drift_rate=0.5 边界 → `_severity_for` 返 "ambig" 因 `0.5 < 0.5` False → 落到 `else: "neg"`)。验算:drift_rate=0.5 + `_severity_for`:`< 0.2` False,`< 0.5` False,else "neg"。**断言应是 `"neg"`**(具体预期)
- `test_every_n_turns_throttling`:`turn_idx=1 (1%2=1) 不跑 → emit_count==0`,`turn_idx=2 (2%2=0) 跑 → emit_count==1`

### M5:E2E 真测

**修法**:`tests/_test_drift_e2e.py` 写真 LLM 端到端:写 3 条同 entity 不一致 memory → drift emit → ReflectionEngine.save 走 source="drift" → store.search_reflections(24h) 召出(若 source='drift' 被 reflection search 包含,需扩 E2 search_reflections 多源支持 — 这是 E2 范围,**留 E5 范围外做**(review ledger M5 仅写 E5 内部端到端,不验 E2 search))。

**简化**:**M5 仅写真 LLM drift emit 验证**(写 3 同 entity 不同 fact memory → DriftDetector 真 Judge 调 JUDGE_MODEL/本地 LLM → 验证 emit 至少 1 次 `drift_detected` event),不验 reflection 端到端。

## 不在本次范围

- agent.py run_turn 透传 turn_idx(e2 reflection 已管 emit,M2 简化)
- E2 search_reflections 多 source 支持(e2 范围,e2 post-merge ticket)
- `ev_safe` 用 `event.source` 改 downstream `getattr(ev_safe, "source", None)`(功能等同 R2.2 后 `event`,无实际差异)

## 风险

- M2 turn_idx 透传是最大改动 — 涉及 4 个文件(repl.py / service.py / retriever.py + 测试),但修法机械
- M5 真测可能耗时(真 LLM 调)— 加 `--timeout` 守卫

## 1 commit 摘要

```
feat(drift): 5 minor ledger 清理 — turn_idx 注入 / sample L5 / 断言加强 / E2E 真测 / ev_safe source
5 task 串到 1 commit(短改,可控)
```
