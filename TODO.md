# TODO — 后备任务清单

> 创建于 2026-07-30,跟着 TUI 转型 spec(`docs/superpowers/specs/2026-07-30-tui-transformation-design.md`)一起落地。
> 当前 sprint 聚焦 TUI 转型,以下条目**不在当前 sprint 范围**,等 TUI 落地后逐步推进。

---

## 优先级:P0 — Multi-Agent Planner/Worker/Reviewer

**来源**:PaiCLI parity 优先级评估
**目标**:D1 SubAgent → D2 三角色扩展
**理由**:
- cc-harness 唯一结构性差距。D1 是单角色 fan-out,PaiCLI "team" 是 Planner/Worker/Reviewer + review 重试
- 落地后承接现有 HTN(B/C/DAG)完成门,让 long-horizon 任务真正可完成
- 复用 D1 SubAgentRunner 几乎所有底座(MAX_DEPTH=2、共享 L4、policy 透传)
**风险**:
- Reviewer 不要绕开 L4 闸门(共享 policy 已有,但要写 spec 钉死)
- Reviewer 提示词不能让"我觉得 OK"通过,必须有 acceptance 解析
- 循环风险:bounded retry + C 完成门要复用
**预期产出**:`docs/superpowers/specs/2026-08-XX-d2-multi-agent-roles-design.md` + plan

---

## 优先级:P1 — Skill 系统

**来源**:PaiCLI parity
**目标**:三位 Layer Skill(builtin / user / project)+ Top-K 匹配 + `load_skill` + `save_skill` HITL
**理由**:
- cc-harness 有 memory(事实) + tool specs(能力),但**没有"做事方法"**这一层
- Skill = 可复用 SOP,Agent 自我沉淀知识
- 自带防御价值:Skill 文档纳入 L5 DLP,Skill 名字匹配纳入 L2(防 Skill-injection)
**风险**:
- Skill-injection:攻击者写 Skill.md 含 prompt injection → 必须 L2 扫 + L5 扫
- Skill 缓存:何时失效(项目文件改)
- 大小限制:不能塞满 context
**预期产出**:`specs/2026-09-XX-skill-system-design.md` + plan

---

## 优先级:P2 — Model Profile(context_window + 价格表)

**来源**:PaiCLI parity
**目标**:每个模型自带 context_window + 价格 profile,OpenAI-compatible 未知模型强制 config
**理由**:
- 当前 `cc_harness/context.py` 4-tier 压缩阈值 hardcode(因为不知道模型真实 context_window)
- 价格:cc-harness 没有 cost 显示
- cache hit/miss:DeepSeek 缓存命中价便宜 10x,cc-harness 5-bucket 不区分
**实现成本**:极低(在 `config.py` 加 profile model,在 `LLMClient` 加 metadata pass-through)
**预期产出**:`specs/2026-09-XX-model-profile-design.md` + plan

---

## 优先级:P3 — RAG / 本地代码索引

**来源**:PaiCLI parity
**目标**:SQLite 代码索引 + watcher + embedding 搜索 + `/index` `/search` 工具
**理由**:
- cc-harness 大仓库靠 grep + MCP filesystem,慢,吞 token
- embedding-based 搜索复用 memory vector store,基础设施 0 增量
- 对 eval-v2 也有价值:自动生成"项目内危险模式" attack
**风险**:
- 索引成本:首次 `cc-harness index` 在 10k+ 文件仓库可能数分钟
- 增量更新:watcher 复杂,先只做 on-demand
- 评估价值 vs token 节省要在真实项目上量化
**预期产出**:`specs/2026-10-XX-rag-code-index-design.md` + plan

---

## 优先级:P4 — Snapshot pre/post-turn + restore

**来源**:PaiCLI parity
**目标**:`pre-turn` / `post-turn` 自动 snapshot + `~/.cc-harness/snapshots/` + `/snapshot` `/restore` `revert_turn`
**理由**:
- 比 git checkout 更"留痕",debug 时看每一步文件状态
- 不污染 .git
**风险**:
- 与 git 双轨:谁是 source of truth?答:git,snapshot 只辅助
- 磁盘占用:大型项目每个 turn 一次 snapshot 很重,要 diff-based
**预期产出**:`specs/2026-10-XX-snapshot-restore-design.md` + plan

---

## 优先级:P5 — Runtime API + Worker + SDK

**来源**:PaiCLI parity
**目标**:`cc-harness serve --http` + `POST /v1/threads` + `POST /v1/turns` + `POST /v1/tasks` + SQLite 持久化队列 + `cc-harness worker`
**理由**:
- eval-v2 已经用 subprocess + JSONL 跑通,**不是阻塞项**
- 但要把 cc-harness 变成"产品"让别人接入,需要 HTTP API + 队列 + heartbeat
**风险**:
- 队列 + heartbeat + atomic claim 一套并发原语
- 跟 TUI 复用一套 boot,避免分裂
**预期产出**:`specs/2026-11-XX-runtime-api-design.md` + plan

---

## 优先级:P6 — Image input

**来源**:PaiCLI parity
**目标**:`@image:path` 语法 + 本地压缩 + provider/model 能力检测 + 降级路径
**理由**:
- 对 coding agent 价值低(截图场景有限)
- 但未来 multimodal benchmark 越来越多
**预期产出**:`specs/2026-12-XX-image-input-design.md` + plan

---

## 优先级:P7 — DX 工具(`doctor` / `/model` UI / BYOK)

**来源**:PaiCLI parity
**目标**:`cc-harness doctor` 健康检查 + 交互式 `/model` 切换 UI + BYOK 模型持久化(0600 权限)
**理由**:
- cc-harness 用户群偏工程化,DX 优先级低
- 但作为 TUI 落地后的"产品打磨"补完
**预期产出**:小任务,直接做,无需 spec

---

## 优先级:P8 — Eval-v2 增量

**来源**:cc-harness eval 路线图
**目标**(已知 TODO):
- Locomo 真跑端到端(`cc_harness/agent.py` 头部 `from cc_harness.memory import ...` + `_run_locomo` 取消 TODO)
- judge 50 baseline placeholder → 真实人工标注
- Pass^k 跑 5 次的实际数据收集
- Cyber 6 子类每类单独 ASR 报告(已部分落地)
- L5 DLP 在 judge input 的处理(防 judge 看到明文)
**预期产出**:`eval/promptfoo/docs/eval-methodology.md` 增量

---

## 优先级:P9 — D2/D3 SubAgent 深化

**来源**:cc-harness 大蓝图
**目标**:
- D2:SubAgent 嵌套(MAX_DEPTH=2 → 3 + 子 Agent 模式类型)
- D3:SubAgent 工具隔离(子 Agent 不能调危险工具)
- SubAgent 轨迹可视化

---

## 优先级:P10 — TUI 多 session / tab 切换 / 分屏

**来源**:内部讨论
**目标**:Claude Code 风格本身单 pane,但用户可能想要多 session
**触发条件**:TUI 落地后用户反馈"想要多 session" 才做,YAGNI

---

## 优先级:P11 — Mob / Vim 模式

**来源**:Claude Code parity
**目标**:`/vim` 切换 Emacs/Vim 键绑定
**触发条件**:用户反馈需要,YAGNI

---

## 优先级:P12 — 外部生态

**来源**:PaiCLI parity
**目标**:
- `mcp init-chrome` 一键写 Chrome DevTools MCP 配置
- `mcp serve` PaiCLI 自身作为 MCP server
- 教程路线(类似 `paicoding.com/paicli-learning-path`)
- 微信 / 飞书 / 钉钉 集成(都不做,除非有具体场景)

---

## 不做列表(确定不做)

- **Web UI 恢复** — 决定 Q1 已立项,不做
- **Kernel sandbox (gVisor/Firecracker)** — Linux-only,延后
- **Multi-LLM backend switching** — 锁 OpenAI-compatible,不动
- **SubAgent 之外的并发工具调用** — 串行优先

---

## 新增条目

每开新条目,加在该段下方,标 `[日期] 描述` + 优先级 Px。完成时打 ✅ + 在 git message 引用回此 TODO。
