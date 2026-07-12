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

## 现有代码事实(Q4 落点)

| 文件 | 现状 | Q4 处置 |
|---|---|---|
| `cc_harness/agent.py` | tool 派发后 `messages.append({"role":"tool",...,"content":_external})`(line ~323-327 allow 分支 / 339-347 ask yes 分支) | **after-tool-call hook**:append 前判超阈值 → offload + 换指针 |
| `cc_harness/context.py:maybe_compact` | Plan3 消息级 ratio summarize(protect 系统段) | 并存(Q4 先减 tool 大块,Plan3 兜底) |
| `cc_harness/memory/`(Q3) | L0-L3 分层已落地 + after-turn hook | Q4 独立子模块 `memory/offload/`,不碰 Q3 |
| `cc_harness/tools.py:run_command` | coding 主工具,输出大头 | Q4 主要卸载体(run_command/MCP read 结果) |
| `cc_harness/memory/config.py:MemoryConfig` | 已有(Q3 加 5 字段) | Q4 加 offload 段或新 OffloadConfig |

## 关键决策(brainstorm 确认)

1. **混合触发**:after-tool-call(每 tool result 超阈值即卸载)+ ratio(上下文超比例批量兜底)。
2. **仅卸载 tool result**(非 assistant/user)。工具日志是可回查原始数据,卸载最安全;user 原话保留、assistant 推理保留。
3. **全 Mermaid 任务图**(LLM 抽步骤+流转)。对标腾讯符号化画布核心亮点(非简化节点序列)。成本:每卸载 1-2 次 LLM 调用(抽节点 + summary)。
4. **与 Plan3 并存**(工具级 vs 消息级,不同时机,不冲突)。
5. **node_id 溯源**(refs 原文 + Mermaid 节点 + tool message 指针,100% 可恢复)。
6. **kill-switch**(`offload.enabled=false`)。

## 架构(对标腾讯符号化短期记忆)

```
[tool 执行完 → result]
   │
   ▼ if len(result) > offload_threshold (2000 token)
[offload.py]
   ├─ node_id = gen_id()
   ├─ refs/{node_id}.md ← result 原文(底层证据)
   ├─ mermaid 节点 ← LLM 抽(result → graph node label + edge 流转)
   └─ tool message ← [offloaded node=n1 summary="..." (refs/n1.md)](中层指针)
   │
   ▼ else
[tool message = result](小结果保留)
   │
   ▼ ratio 兜底(context > offload_ratio 0.5)
[批量卸载剩余大 tool result]
   │
   ▼ pre-turn(与 Q3 recall 注入同阶段)
[Mermaid 画布注入系统段(高层全景,轻量,token 预算)]
   │
   ▼ LLM 需细节
[read_ref(node_id) 工具 / grep → refs/{node_id}.md 原文]
```

## 组件(新建 `cc_harness/memory/offload/`)

| 文件 | 责任 | 核心接口 |
|---|---|---|
| `offload.py` | 卸载逻辑:result → refs + tool message 指针 | `async def maybe_offload(result, tool_name, args, threshold, refs_dir) -> OffloadResult | None` |
| `mermaid.py` | Mermaid 画布生成/更新 | `async def update_canvas(node_id, label, summary, canvas_path, llm) -> str`(LLM 抽 graph 节点+边) |
| `models.py` | 数据结构 | `OffloadResult(node_id, summary, refs_path, pointer_msg)` |
| `refs/` 存储 | `logs/memory/refs/{node_id}.md` | 文件 IO |
| `read_ref` 工具 | LLM 下钻原文 | 注入 agent 作 native tool(spec MEMORY_RECALL_SPEC 风格) |

### 现有改动
- `cc_harness/agent.py`:tool 派发后(allow/ask-yes 分支,append 前)调 `maybe_offload`。超阈值 → tool message 换 `pointer_msg`;否则原样。加 `offload_deps` 参数(从 repl/runner 注入,含 refs_dir/canvas_path/llm)。
- `cc_harness/memory/extras.py:build_memory_extras`:deps 加 offload 组件(refs_dir/canvas_path/maybe_offload callable/read_ref spec)。
- `cc_harness/agent.py` pre-turn(与 Q3 memory_layer 注入同阶段):Mermaid 画布注入系统段(若存在 + token 预算内)。
- `cc_harness/memory/config.py:MemoryConfig`:加 offload 段(threshold/ratio/mermaid_max_token_ratio/enabled)+ env 覆盖。
- `cc_harness/repl.py`/`eval/locomo/runner.py`:build_memory_extras deps 取 offload → 传 agent。

## 数据流

```
[tool 派发完成,result = _dispatch(...)]
  └─ if offload_deps and len(result.llm_text) > threshold:
       off = await maybe_offload(result.llm_text, tool_name, args, threshold, refs_dir, llm)
       # off: node_id + summary + refs/{node_id}.md + pointer_msg
       tool_message_content = off.pointer_msg  # [offloaded node=n1 summary="..." (refs/n1.md)]
       await update_canvas(off.node_id, tool_name, off.summary, canvas_path, llm)  # Mermaid 加节点
     else:
       tool_message_content = f"<untrusted>{result.llm_text}</untrusted>"  # 现有,不卸载
     messages.append({"role":"tool", "tool_call_id":..., "content": tool_message_content})

[pre-turn(与 Q3 注入同阶段)]
  └─ if canvas_path exists and mermaid 画布 token <= 预算:
       messages[0] += "\n\n## 任务画布(Mermaid)\n" + canvas_content

[ratio 兜底(maybe_compact 前或独立)]
  └─ if context_ratio > offload_ratio:
       批量 maybe_offload 剩余大 tool result(messages 中 role==tool 且未 offloaded)

[LLM 需细节]
  └─ read_ref(node_id) 工具 → refs/{node_id}.md 原文
```

## node_id 溯源链(对标腾讯 100% 可恢复)

| 层 | 内容 | 位置 |
|---|---|---|
| 高层(全景) | Mermaid 画布 `graph LR n1[read]-->n2[run]-->n3[test]` | 系统段注入(pre-turn,token 预算内) |
| 中层(指针) | tool message `[offloaded node=n1 summary="..." (refs/n1.md)]` | messages 中 tool 角色 |
| 底层(原文) | tool result 完整原文 | `logs/memory/refs/{node_id}.md` |

LLM 推理链:画布看全景(高层符号)→ 需细节时 `read_ref(node_id)` 下钻 refs(底层原文)。任何卸载都可恢复。

## 与 Plan3 并存(时序 + 分工)

**代码事实**:Plan3 `maybe_compact` 在 `agent.py` while 循环**内部** per-iteration(ratio 触发,protect 系统段)。Q4 after-tool-call 在工具派发后(per-tool,循环内工具执行段)。

- **分工**:Q4 减 tool result 大块(单条卸载),Plan3 减旧消息整体(summarize)。
- **顺序**:同 turn 内,Q4 工具后即时卸载 → Plan3 下次迭代前 ratio 判定(若 Q4 减够,Plan3 不触发)。
- **不冲突**:Q4 卸载替换 tool message 为指针(仍 role=tool);Plan3 Tier1/2 处理 tool 段(指针短,更不易触发);Plan3 protect 系统段 → Mermaid 画布(系统段)也 protect。
- **ratio 协调**:Q4 offload_ratio(0.5)< Plan3 tier1(0.6)→ Q4 先卸载减载,Plan3 后兜底。

## 触发参数(`MemoryConfig` 加 offload 段)

| 字段 | 默认 | env | 说明 |
|---|---|---|---|
| `offload_enabled` | True | `MEMORY_OFFLOAD_ENABLED` | kill-switch 总闸 |
| `offload_threshold` | 2000 | `MEMORY_OFFLOAD_THRESHOLD` | 单 tool result token 超此即卸载 |
| `offload_ratio` | 0.5 | `MEMORY_OFFLOAD_RATIO` | 上下文超此批量兜底(< Plan3 tier1 0.6) |
| `mermaid_max_token_ratio` | 0.2 | `MEMORY_MERMAID_MAX_TOKEN_RATIO` | 画布注入占 context 比例预算 |
| `offload_canvas_inject` | True | `MEMORY_OFFLOAD_CANVAS_INJECT` | pre-turn Mermaid 注入开关 |

## 成本权衡 + 缓解

- **每卸载 LLM 成本**:全 Mermaid 需 LLM 抽节点+边(1 次)+ summary(可合并)。coding 跑命令多 → 频繁。
- **缓解**:
  - kill-switch(`offload_enabled=false`)。
  - Mermaid 抽 lazy:单次 offload 只记节点(node_id+label+summary 文本),累积 N 节点后或 ratio 触发时批量 LLM 抽画布边(流转)。MVP 可全即时。
  - threshold(2000)过滤小结果(不卸不抽)。
  - offload canvas_inject=false 时只卸载不注入画布(省 system token,LLM 靠 read_ref 下钻)。

## 测试策略

### 位置
- `tests/test_memory_offload.py`(unit,mock LLM/embedder)
- `eval/locomo/tests/`(集成)

### Unit
- `test_maybe_offload_large` — result 超 threshold → refs/{node_id}.md 生成 + tool message = pointer + OffloadResult 对
- `test_maybe_offload_small` — result < threshold → 返 None(不卸载),tool message 原样
- `test_update_canvas` — LLM 抽 Mermaid 节点+边(mock LLM 返 graph),canvas.md 更新
- `test_read_ref_tool` — read_ref(node_id) 返 refs 原文
- `test_node_id_traceability` — 溯源:Mermaid 节点 → tool message pointer → refs 原文 全链
- `test_offload_ratio_batch` — context 超 offload_ratio → 批量卸载剩余大 tool result
- `test_agent_after_tool_call_hook` — run_turn 工具调用后大 result 被卸载(tool message = pointer)
- `test_plan3_coexist` — Q4 卸载后 Plan3 触发减少(模拟:卸载后 ratio < tier1)

### 集成(locomo)
- 降 `CONTEXT_WINDOW=32768` 跑 conv-26:验 tool result 卸载率(refs/*.md 数)+ token 降量(vs 无 Q4)+ memory_recall 正常
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
2. **node_id 一致性**:refs 文件名 vs Mermaid vs tool message 三处 node_id 须一致。缓解:gen_id 一次传三处 + 单测。
3. **LLM 不主动 read_ref 下钻**:卸载后 LLM 可能凭指针 summary 推理(够)或忽略。缓解:summary 足够 + read_ref 工具暴露 + prompt 引导(可选)。
4. **与 Plan3 ratio 协调**:offload_ratio(0.5)< tier1(0.6)须严格,否则 Plan3 先触发 summarize(不可逆)盖过 Q4。缓解:config validator + 交互测试。
5. **refs 磁盘累积**:长 session 多 tool call → refs 多文件。缓解:read_top/按 session 清理(后续);MVP 接受。

## 实现顺序(writing-plans 细化)

1. `offload/models.py`(OffloadResult)+ config offload 段 + OffloadConfig validator
2. `offload/offload.py`(maybe_offload:refs + pointer,threshold 判)
3. `offload/mermaid.py`(update_canvas:LLM 抽节点+边,mock 可测)
4. `read_ref` 工具(spec + handler)+ extras deps 加 offload 组件
5. `agent.py` after-tool-call hook(allow/ask-yes 分支,append 前 maybe_offload)
6. `agent.py` pre-turn Mermaid 画布注入(token 预算)
7. ratio 批量兜底 + Plan3 协调(offload_ratio < tier1)
8. locomo 降窗口集成验证(token 降量 + 白盒 md)
