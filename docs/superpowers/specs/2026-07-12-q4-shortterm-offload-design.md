# Q4 短期符号化卸载(Context Offload + Mermaid 画布)设计

> **范围**:腾讯 TencentDB-Agent-Memory 短期记忆(符号化)方案的 Python 移植。本 spec 是 3-sub-project 重建的**第 2 块**(Q4)。Q3(长期分层)已完成;Q1(指标公允)最后,各自独立 spec。
>
> **腾讯对标出处**:[TencentCloud/TencentDB-Agent-Memory](https://github.com/TencentCloud/TencentDB-Agent-Memory) §核心技术「符号化记忆:用最少符号表达最多语义(Mermaid 画布)」+ 「上下文卸载 Context Offloading」+ WideSearch benchmark(省 61.38% token,成功率 +51.52%)。
>
> **For agentic workers:** REQUIRED SUB-SKILL: superpowers:subagent-driven-development。

## Goal

给 cc-harness 加**短期符号化卸载**:工具调用结果(tool result)即时卸载到 `refs/{node_id}.md` 原文 + LLM 抽 Mermaid 任务画布(节点+流转)+ 上下文留轻量 node_id 指针。解决 coding agent 工具日志(run_command 输出、文件读)膨胀上下文问题。

对标腾讯"符号化短期记忆":大幅降 token + 100% 可恢复(node_id 下钻原文)。与 Plan3(消息级 summarize)**并存** — Q4 工具级即时卸载,Plan3 消息级 ratio 兜底。

## 背景(为何做)

locomo conv-26 烟测:1M 窗口下 prompt peak 174K(17%),全量样本更冲高。coding 生产场景 run_command 输出(编译日志/test 输出)单次可达数千-数万 token,反复工具调用上下文爆炸 → 触发 Plan3 summarize(不可逆,丢细节)。腾讯 WideSearch 卸载省 61% token + 成功率升。Q4 用可逆卸载替代部分 summarize,且对标腾讯"符号化画布"面试亮点。

## 现有代码事实(Q4 落点,锚点描述 — 行号易漂移以锚点为准)

| 文件 | 现状 | Q4 处置 |
|---|---|---|
| `cc_harness/agent.py` | tool 派发后,`if decision.allow:` 块内 `_external = f"<untrusted>...</untrusted>"` 赋值后、`messages.append({"role":"tool",...})` 前(allow 分支 + ask-yes 分支两处对称) | **after-tool-call hook**:append 前 `_external` 超 token 阈值 → offload + 换 pointer |
| `cc_harness/agent.py:106-117` | Q3 pre-turn memory_layer 注入(persona/scenarios 追加 `messages[0]["content"]`) | Q4 Mermaid 画布注入**同阶段追加**(顺序见下) |
| `cc_harness/agent.py:236-241` | Plan3 `maybe_compact` 在 while 循环内 per-iteration(ratio 触发,protect 系统段) | 并存(Q4 先减 tool 大块,Plan3 兜底) |
| `cc_harness/agent.py:62` | `run_turn` 现签名含 `memory_layer: dict \| None`(Q3) | Q4 加 `offload_deps: dict \| None` **独立参数**(不复用 memory_layer — 写入卸载 vs 读入召回语义不同) |
| `cc_harness/context.py` | Plan3 `_snip_lines` 仅对 `len(lines) > head+tail+1` 触发(~L121-136);tier1 ratio 0.6(`config.py:201`) | Q4 pointer 短(<10 行)不触发 snip;offload_ratio 0.5 < tier1 0.6 |
| `cc_harness/memory/`(Q3) | L0-L3 分层 + after-turn hook + extras deps 7 锭(service/retriever/pipeline/recall/store/persona_path/scenarios_dir) | Q4 独立子模块 `memory/offload/`,不碰 Q3;extras deps 加 offload 锭 |
| `cc_harness/memory/config.py:MemoryConfig` | Q3 加 7 字段,无 offload 段 | Q4 加 offload 段 |
| `cc_harness/tools.py:run_command` | coding 主工具,输出大头 | Q4 主要卸载体 |

## 关键决策(brainstorm 确认 + 本轮核实)

1. **混合触发**:after-tool-call(每 tool result 超 token 阈值即卸载)+ ratio(上下文超比例批量兜底)。
2. **仅卸载 tool result**(非 assistant/user)。工具日志是可回查原始数据,卸载最安全;user 原话保留、assistant 推理保留。
3. **全 Mermaid 任务图**(LLM 抽步骤+流转)。对标腾讯符号化画布核心亮点。成本:每卸载 LLM 调用(抽节点 + summary,可合并 1 次)。
4. **与 Plan3 并存**(工具级 vs 消息级,不同时机,不冲突)。
5. **node_id 溯源**(refs 原文 + Mermaid 节点 + tool message 指针,100% 可恢复)。
6. **kill-switch**(`offload_enabled=false`)。
7. **offload_deps 独立参数**(不复用 memory_layer — Q4 review Important #2)。
8. **触发单位统一 token**(`token_counter.count_text`,非字符 — Q4 review Critical #2)。

## 架构(对标腾讯符号化短期记忆)

```
[tool 执行完 → result]
   │
   ▼ if token_counter.count_text(result.llm_text) > offload_threshold (2000 token)
[offload.py:maybe_offload(result_text, tool_name, args, threshold, refs_dir, llm, token_counter)]
   ├─ node_id = gen_id()
   ├─ refs/{node_id}.md ← result 原文(底层证据)
   ├─ summary = LLM 抽(result → 一句摘要)           ← llm 参数用
   ├─ mermaid 节点/边 = LLM 抽(result → graph node label + edge 流转)  ← llm 参数用
   └─ pointer_msg = f"[offloaded node={node_id} summary='{summary}' (refs/{node_id}.md)]"
   │
   ▼ tool message content = pointer_msg(中层指针,替换 _external)
   │
   ▼ else(result 小)
[tool message = _external(现有 <untrusted> 包装,不卸载)]
   │
   ▼ ratio 兜底(context > offload_ratio 0.5)
[批量 maybe_offload 剩余大 tool result(messages 中 role==tool 且未 offloaded)]
   │
   ▼ pre-turn(与 Q3 memory_layer 注入同阶段)
[Mermaid 画布注入系统段(若 canvas_inject 且 token <= 预算)]
   │
   ▼ LLM 需细节
[read_ref(node_id) 工具 → refs/{node_id}.md 原文]
```

## 组件(新建 `cc_harness/memory/offload/`)

| 文件 | 责任 | 核心接口 |
|---|---|---|
| `models.py` | 数据结构 | `OffloadResult(node_id, summary, refs_path, pointer_msg)` |
| `offload.py` | 卸载逻辑:result → refs + pointer + Mermaid 节点 | `async def maybe_offload(result_text, tool_name, args, threshold, refs_dir, llm, token_counter) -> OffloadResult \| None`(**含 llm + token_counter**) |
| `mermaid.py` | Mermaid 画布更新(node+边) | `async def update_canvas(node_id, label, summary, edge_from, canvas_path, llm) -> str`(LLM 抽 graph 节点+边) |
| `refs/` 存储 | `logs/memory/refs/{node_id}.md` | 文件 IO |
| `read_ref` 工具 | LLM 下钻原文 | native tool spec(spec MEMORY_RECALL_SPEC 风格),handler 读 refs/{node_id}.md |

### 现有改动
- `cc_harness/agent.py:run_turn`:加 `offload_deps: dict | None = None` **独立参数**(与 memory_layer 并列)。tool 派发后(allow + ask-yes 分支,`_external` 赋值后、append 前)调 `maybe_offload`;超阈值 → tool message = pointer_msg,否则原 `_external`。pre-turn(与 memory_layer 注入同处)`messages[0] += Mermaid 画布`(若 canvas_inject)。
- `cc_harness/memory/extras.py:build_memory_extras`:deps dict 加 offload 锭(`refs_dir`/`canvas_path`/`maybe_offload` callable/`read_ref` spec)。返回类型不变 tuple。
- `cc_harness/memory/config.py:MemoryConfig`:加 offload 段字段 + env。
- `cc_harness/repl.py`/`eval/locomo/runner.py`:从 deps 取 offload 组 `offload_deps` 传 run_turn(与 memory_layer 并列)。

## 数据流

```
[tool 派发完成,result = await _dispatch(p, args, project_root)]
  └─ _external = f"<untrusted>{result.llm_text}</untrusted>"
  └─ if offload_deps and offload_deps.get("enabled", True):
       _tok = token_counter.count_text(result.llm_text)
       if _tok > offload_deps["threshold"]:
         off = await maybe_offload(result.llm_text, p.name, args,
                                   offload_deps["threshold"], offload_deps["refs_dir"],
                                   offload_deps["llm"], token_counter)
         # off: node_id + summary + refs/{node_id}.md + pointer_msg
         await update_canvas(off.node_id, p.name, off.summary, edge_from=last_node_id,
                             canvas_path=offload_deps["canvas_path"], llm=offload_deps["llm"])
         tool_message_content = off.pointer_msg   # 替换 _external
         last_node_id = off.node_id               # 下一节点的 edge_from
       else:
         tool_message_content = _external          # 小结果,不卸载
     else:
       tool_message_content = _external            # kill-switch 关
     messages.append({"role":"tool","tool_call_id":...,"content": tool_message_content})

[pre-turn(与 Q3 memory_layer 注入同阶段,顺序见下)]
  └─ if offload_deps and offload_deps.get("canvas_inject", True):
       canvas = read canvas_path if exists
       预算 = mermaid_max_token_ratio × context_window   # 换算公式
       if canvas and count_text(canvas) <= 预算:
         messages[0]["content"] += "\n\n## 任务画布(Mermaid)\n" + canvas

[ratio 兜底(maybe_compact 前)]
  └─ if context_ratio > offload_ratio:
       for msg in messages where role==tool and not offloaded and count_text > threshold:
         maybe_offload → 换 pointer

[LLM 需细节]
  └─ read_ref(node_id) 工具 → refs/{node_id}.md 原文
```

## pre-turn 注入顺序(Q4 review Important #4)

`messages[0]["content"]` 追加顺序固定(可测/可复现):
```
1. 基线 system prompt(_refresh_system_prompt 覆写)
2. Q3 persona(若有)
3. Q3 scenarios(若有)
4. Q4 Mermaid 画布(若 canvas_inject 且 token <= 预算)
```
(顺序固定 → 测试可断言;Q3/Q4 注入同处 agent.py pre-turn 段,Q3 先 Q4 后)

## node_id 溯源链(对标腾讯 100% 可恢复)

| 层 | 内容 | 位置 |
|---|---|---|
| 高层(全景) | Mermaid 画布 `graph LR n1[read]-->n2[run]-->n3[test]` | 系统段注入(pre-turn,token 预算内) |
| 中层(指针) | tool message `[offloaded node=n1 summary="..." (refs/n1.md)]` | messages 中 tool 角色 |
| 底层(原文) | tool result 完整原文 | `logs/memory/refs/{node_id}.md` |

**node_id 三处字面一致**:refs 文件名(`{node_id}.md`)== Mermaid 节点 id(`n1`)== tool message pointer(`node=n1`)。`gen_id()` 一次生成,三处复用同一字符串(单测断言三处相等)。LLM 推理链:画布看全景 → `read_ref(node_id)` 下钻 refs 原文。

## 与 Plan3 并存(时序 + 分工)

**代码事实**:Plan3 `maybe_compact` 在 `agent.py` while 循环**内部** per-iteration(ratio 触发,protect 系统段)。Q4 after-tool-call 在 tool 派发后(per-tool,循环内工具执行段)。

- **分工**:Q4 减 tool result 大块(单条卸载),Plan3 减旧消息整体(summarize)。
- **顺序**:同 turn 内,Q4 工具后即时卸载 → Plan3 下次迭代前 ratio 判定(若 Q4 减够,Plan3 不触发)。
- **不冲突**:Q4 卸载替换 tool message 为 pointer(仍 role=tool,短);Plan3 Tier1 `_snip_lines` 仅对长 tool 触发(`len(lines)>head+tail+1`),pointer <10 行不触发;Plan3 protect 系统段 → Mermaid 画布(系统段)也 protect。
- **ratio 协调**:Q4 offload_ratio(0.5)< Plan3 tier1(0.6)→ Q4 先卸载减载,Plan3 后兜底。config validator 强制 offload_ratio < tier1_threshold。

## 触发参数(`MemoryConfig` 加 offload 段)

| 字段 | 默认 | env | 说明 |
|---|---|---|---|
| `offload_enabled` | True | `MEMORY_OFFLOAD_ENABLED` | kill-switch 总闸 |
| `offload_threshold` | 2000 | `MEMORY_OFFLOAD_THRESHOLD` | 单 tool result **token**(token_counter.count_text)超此即卸载 |
| `offload_ratio` | 0.5 | `MEMORY_OFFLOAD_RATIO` | 上下文超此批量兜底(config validator 强制 < tier1_threshold 0.6) |
| `mermaid_max_token_ratio` | 0.2 | `MEMORY_MERMAID_MAX_TOKEN_RATIO` | 画布注入 token 预算比例(预算 = ratio × context_window) |
| `offload_canvas_inject` | True | `MEMORY_OFFLOAD_CANVAS_INJECT` | pre-turn Mermaid 注入开关 |

## 成本权衡 + 缓解

- **每卸载 LLM 成本**:全 Mermaid 需 LLM 抽节点+边 + summary(可合并 1 次调用,估 ~500 input + ~200 output token/次)。coding 跑命令多 → 频繁。
- **缓解**:
  - kill-switch(`offload_enabled=false`)。
  - Mermaid 抽 lazy:单次 offload 只记节点文本(node_id+label+summary),累积 N 节点或 ratio 触发时批量 LLM 抽边(流转)。MVP 可全即时。
  - threshold(2000 token)过滤小结果(不卸不抽)。
  - canvas_inject=false 时只卸载不注入画布(省 system token,LLM 靠 read_ref 下钻)。

## 测试策略

### 位置
- `tests/test_memory_offload.py`(unit,mock LLM/token_counter)
- `eval/locomo/tests/`(集成)

### Unit
- `test_maybe_offload_large` — result token > threshold → refs/{node_id}.md 生成 + pointer_msg + OffloadResult 对(用 token_counter.count_text 判)
- `test_maybe_offload_small` — result token < threshold → 返 None(不卸载)
- `test_maybe_offload_threshold_boundary` — result token == threshold 不卸载 / == threshold+1 卸载(边界)
- `test_offload_kill_switch` — `offload_enabled=false` → maybe_offload 直接返 None / agent 不调 hook
- `test_update_canvas` — LLM 抽 Mermaid 节点+边(mock LLM 返 graph),canvas.md 更新
- `test_read_ref_tool` — read_ref(node_id) 返 refs 原文
- `test_node_id_three_way_consistent` — refs 文件名 == Mermaid 节点 id == tool message pointer 三处字面相等(gen_id 复用)
- `test_node_id_traceability` — 溯源全链:Mermaid 节点 → tool message pointer → refs 原文 可下钻
- `test_offload_ratio_batch` — context 超 offload_ratio → 批量卸载剩余大 tool result
- `test_canvas_inject_disabled` — canvas_inject=false → 只卸载不注入,系统段不增长
- `test_agent_after_tool_call_hook` — run_turn 工具调用后大 result 被卸载(tool message = pointer),小 result 保留
- `test_plan3_coexist` — Q4 卸载后 ratio < tier1 → Plan3 不触发;Q4 失效(kill)→ Plan3 接管(summarize)
- `test_pre_turn_inject_order` — 系统段顺序:基线 → persona → scenarios → mermaid(固定)

### 集成(locomo)
- 降 `CONTEXT_WINDOW=32768` 跑 conv-26:验 tool result 卸载率(refs/*.md 数)+ token 降量(vs 无 Q4 同窗口)+ memory_recall 正常
- Mermaid 画布白盒可读(人工)

### 现有不破
- Q3 `tests/test_memory_layered.py` 16 test 继续过
- `tests/test_agent.py`/`test_repl.py`/`test_memory_extras.py` 继续过

## 非目标(out of scope)

- **长期分层 L0-L3**(Q3,已完成)
- **指标公允 / semantic f1**(Q1,最后)
- **Skill 自动生成 / 跨 Agent 迁移**(腾讯 Roadmap,远期)
- **卸载 assistant/user**(本 spec 仅 tool result)

## 风险

1. **LLM 抽 Mermaid 成本**:每卸载调 LLM。缓解:lazy 抽 + threshold 过滤 + kill-switch(见成本权衡)。
2. **node_id 一致性**:refs/Mermaid/tool message 三处 node_id 须字面一致。缓解:gen_id 一次传三处 + `test_node_id_three_way_consistent` 单测。
3. **LLM 不主动 read_ref 下钻**:卸载后 LLM 可能凭 pointer summary 推理(够)或忽略。缓解:summary 足够 + read_ref 工具暴露 + prompt 引导(可选)。
4. **ratio 协调**:offload_ratio(0.5)< tier1(0.6)须严格,否则 Plan3 先 summarize 盖过 Q4。缓解:config validator 强制 + `test_plan3_coexist` 双向。
5. **refs 磁盘累积**:长 session 多 tool call → refs 多文件。缓解:read_top/按 session 清理(后续);MVP 接受。
6. **Plan3 Tier1 snip 误 snip pointer**:pointer 短(<10 行),`_snip_lines` 仅长 tool 触发 → 不误 snip(架构保证 + 测试)。

## 实现顺序(writing-plans 细化)

1. `offload/models.py`(OffloadResult)+ MemoryConfig offload 段 + validator(offload_ratio < tier1)
2. `offload/offload.py`(maybe_offload:refs + pointer + summary,token_counter 判阈值,含 llm)
3. `offload/mermaid.py`(update_canvas:LLM 抽节点+边,mock 可测)
4. `read_ref` 工具(spec + handler)+ extras deps 加 offload 锭(refs_dir/canvas_path/maybe_offload/read_ref/llm)
5. `agent.py` 加 `offload_deps` 独立参数 + after-tool-call hook(allow + ask-yes 分支,append 前)
6. `agent.py` pre-turn Mermaid 注入(预算 = mermaid_max_token_ratio × context_window,顺序 persona→scenarios→mermaid)
7. ratio 批量兜底 + Plan3 协调(validator + 交互测试)
8. locomo 降窗口集成验证(token 降量 + 白盒 md)
