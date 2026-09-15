# Codex 自主执行任务书：对标 dsh 一次性完成 cc-harness Web 交互重做（无需中途确认）

你是一名资深前端/交互工程师，兼通 Python 后端。cc-harness 是事件溯源的 Durable Agent Runtime（Python + FastAPI + React18/Vite/TS 单页前端）。你的任务是**一次性自主完成**：调查 → 差距分析 → 方案设计 → 分阶段实现 → 自验证修到全绿 → 交付报告，全程不要停下来等人确认，按本任务书的授权规则自行决策并记录。工作目录 `D:\agent_learning\cc-harness`，参考实现只读目录 `D:\agent_learning\deepseek-harness-reference`。

## A. 自主授权与工作规则（最高优先级）

1. 你被授权独立做出所有实现决策，禁止以"请确认/你想要哪种/是否继续"中断流程；遇到选择题时，按"不破坏 runtime 契约 > 用户实时性体验 > 过程透明 > 视觉精致 > 改动面最小"的优先级自行选定，把备选、选择、理由写进 `docs/audits/web-ux-decisions.md` 决策日志，然后继续。
2. 新建 git 分支 `feat/webux-revamp` 再开工；按任务项原子提交（commit message 带编号，如 P0-1），保证每个 commit 可构建、可回滚。不要 push，不要动 git 历史。
3. 单项连续两次按同一思路修复失败，必须换方案（换通道/换抽象/降级实现），不许死磕；某项最终确实做不成，标注"未完成+根因+影响面+后续建议"，跳过并继续后续项，**不许因此停工**。
4. 全程中文 UI、中文文档与中文 commit body 标题可英文；Windows + PowerShell 环境，路径与命令按 Windows 实测，禁止只在 macOS 成立的假设。
5. 不许拷贝 dsh 源码（MIT 也不行，只学交互模式），不许新增 Redux/antd/MUI 类重框架，优先现有 React18 + 原生 CSS/CSS Modules；确需新依赖（如虚拟化、contentEditable 芯片所需），在决策日志写明理由后自行引入最小依赖。
6. 完成标准是"用户打开就能用、测试全绿"，不是"代码写完"。最后必须真的启动服务做端到端自检。

## B. 环境事实（已确认，先复核再用）

- cc 前端：`web/src/main.tsx`（约 74KB 单文件）、`web/src/styles.css`（约 75KB），React18+Vite+TS，marked+dompurify+lucide-react；构建 `web/` 下 `npm run build`（tsc -b && vite build），FastAPI（`cc_harness/webui.py`）托管 dist。
- cc 后端 SSE：`webui.py` 约 1402–1440 行 `/api/sessions/{run_id}/events`，当前每 0.5s 轮询已提交事件树、无 token 增量；worker（`cc_harness/worker.py`）消费完整段 LLM 流后才追加 AssistantMessageCommitted——这是"先卡 Runtime 正在工作、再整段蹦字"的根因。
- cc 的 `cc_harness/llm.py` 已逐 chunk 产出 content/reasoning_content/tool_call_delta 流式事件，增量在源头存在，缺 worker→web 的转发与前端消费。
- 持久事实源是 run_events（`cc_harness/run_model.py` 状态机、`run_events.py`、`run_store.py`），绝不为流式增量加持久事件。
- 参考产品 dsh 本地实例：`http://127.0.0.1:3180/`（若未运行，自行启动：在 `D:\agent_learning\deepseek-harness-reference` 设 `$env:DSH_HOME=该目录\.dsh-home-observe`，从 `D:\agent_learning\cc-harness\.env` 读 OPENAI_API_KEY 设为 DEEPSEEK_API_KEY，执行 `pnpm dsh web --no-open --port 3180`，后台运行）；cc 实例在 `http://127.0.0.1:3080/`（未运行就用项目既有方式自行启动）。
- dsh 学习地图（只读）：`packages/client/ui-chat/src/client/chat/`（ChatView、AssistantMarkdown、ReasoningRow、MessageItem、StatsPills、TurnUsagePanel、ContextInjectionRow）、`packages/client/ui-tool/src/client/tool/`（ToolCallTree、ToolRow、toolviews/* 按工具族分视图）、`packages/client/ui-trajectory/src/client/`（TrajectoryView、TrajectoryTimeline 分阶段时间条）、`packages/client/ui-conversation/src/client/`（input/editor 的 @芯片、queue/QueueDock、skeleton 的 EmptyHero/InputBar/ContextMeter）。

## C. 总流程（按序一路做完，每步产出落盘）

1. 调查（限时，不纠缠）：复核 B 中事实；两个本地实例各做 ①简单问答 ②两步工具任务 ③中途停止，截图存 `docs/audits/screenshots/`；产出 `docs/audits/web-ux-gap.md`（列：现象 | dsh 做法+模块路径 | cc 现状+file:line | 根因层次 传输/状态/渲染/样式 | 任务编号）。
2. 设计：产出 `docs/design/web-streaming.md`——易失增量广播 vs 持久事件边界、广播消息协议、SSE 对账协议、前端 streaming→committed/stopped/failed 状态机（含断线重连、停止、失败重试三态）、前端新目录结构与组件树。然后直接开工，不等审批。
3. 实现 P0→P4（见 D），每项完成即提交。
4. 自验证（见 E），失败就修到绿再走。
5. 产出最终报告 `docs/audits/web-ux-final-report.md` 并结束。

## D. 实现任务（按阶段顺序，每项含内置验收，达成才进入下一项）

### P0 真实流式管线（最高优先级）
- P0-1 worker 进程内 pub/sub 广播器：按 run_id 订阅、asyncio.Queue 扇出、最后值缓存与订阅即补；在模型段循环把 content/reasoning/tool_call_delta/阶段状态变化即时广播，消息带 run_id、segment 序号、单调 chunk 序号；只驻内存，不写 SQLite、不进 run_events；worker 结束/崩溃时广播终态并清理订阅。
- P0-2 改造 `/events` SSE：订阅广播即时推送增量，已提交事件在追加时即时下发（保留低频对账兜底与 heartbeat、Last-Event-ID 续传）；客户端重连后以 timeline 已提交事件为准对账，流式气泡被权威内容替换，去重不闪烁不错位。
- P0-3 前端流式节点：当前未完成回合维护 streaming 节点，delta 追加而非替换，正文逐 token 上屏带光标；收到 AssistantMessageCommitted 一次性替换为权威内容（长度/末尾校验，不一致以提交版为准并 console.warn）。
- P0-4 增量 markdown：rAF/≥30ms 合帧节流重解析，组件 memo，未闭合代码块不闪坏样式；1500 字长文输出期间输入与滚动不掉帧。
- P0-5 思考独立通道：reasoning.delta 进独立"思考"块（弱化色、流式默认展开、回合结束自动折叠为"已思考 >"可再展开）；reasoning 不落持久化、广播即焚，刷新后只显示"已思考"。
- P0-6 停止即时：点停止立即调现有 stop 接口、冻结流式气泡并打"已停止"，半截内容保留；消除答案已出完还在转、最终落"已暂停/继续"的割裂。
- 验收：逐词上屏无整段蹦字；断网 3s 恢复不丢不重；pytest 覆盖广播扇出/多订阅者/worker 退出清理/SSE 续传/对账去重；前端测试覆盖 delta 追加与权威替换。

### P1 消息与工具过程结构化
- P1-1 助手回合建模为 reasoning/tool-head/tool-result/prose/status 类型化 block 列表与对应组件，替换单个大 marked 字符串。
- P1-2 工具行实时化：开始即出"工具族图标 + 工具名 + 一个关键参数"行（如 读取 · 01-theory.md），执行中转效、结束变稳，按时间穿插在思考/正文之间。
- P1-3 回合结束工具行自动折叠为"N 次工具调用 >"，就地展开入参 JSON 树/有界结果/复制；超限结果显示"显示 x/共 y 行"+"完整结果已落盘，可 offset/limit 续读"，对接 cc 已有 chunk/offload。
- P1-4 结果里文件路径渲染为可点芯片（右侧面板打开）；命令类工具显示退出码+耗时，失败行错误色且可展开 stderr。
- P1-5 修掉用户消息"你 你"重复标签；悬停操作统一：复制/好回答/差回答/从此分支（复用 follow-up、child run API）/重新生成；每条助手消息尾显示"用量 xxK tok · 用时 x 秒"（取自 usage 事件，无则隐藏，禁止编造）。
- P1-6 空状态居中 hero：标题+工作区/模式选择+三张整行可点建议卡；首轮结束自动生成会话标题（有标题能力就接，没有就用首句截断），替换整段任务名当侧栏标题。
- 验收：两步工具任务的过程行出现/折叠/展开全程正确；组件测试覆盖 block 解析与折叠。

### P2 框架、输入区、状态条
- P2-1 56px 图标轨 + 可折叠会话侧栏（按项目分组，状态点：运行中/排队/等待审批/已暂停/失败 + 事件数）+ 主区 + 可开关右侧栏；主区顶"项目 › 会话名"面包屑。
- P2-2 composer：多行自动增高；+ 添加上下文；/ 命令面板分组（添加：文件/目标/计划；指令：压缩/权限/模型/导出，全部只映射已有 API，无后端支撑的不放假入口），↑↓ 选择、Esc 关闭；@ 唤起工作区文件提及并渲染为不可编辑芯片，随消息发送引用。
- P2-3 输入工具行：访问模式三档（仅可查看/工作区内修改/完全权限，对应现有权限预设/沙箱策略）、模型+推理等级两级菜单、上下文占用百分比；生成中发送键变方形"停止生成"，切换零延迟。
- P2-4 底部状态条：运行（色点+具体文案如"正在调用 Glob"）、连接、权限、上下文环、模型；生成中右侧实时"N 轮 M 步 · xxx tok/s · 累计 tok · 缓存命中 x%"（速率按 delta 实时算，轮步/累计以提交事件校准；缓存字段不存在就隐藏该项并在决策日志记录）。
- P2-5 理顺"继续/已暂停"：区分"等待输入（直接打字即续）"与"中断需点继续"，两处入口一致，非必要不弹继续，杜绝用户以为卡死。
- P2-6 降级与错误："沙箱不可用·已降级本机"改为可点详情的细粒度警示而非常驻红字；LLM 重试中显示"网络异常，第 2/3 次重试…"（对齐 llm.py 的 3 次重试），终态失败给"重试本轮"。
- 验收：所有控件键盘可达；命令面板每项都能对应到真实 API；状态条在运行/停止/失败三态文案正确。

### P3 轨迹与透明感
- P3-1 主区"对话/轨迹"双 tab：轨迹按轮次分组+角色色签（系统/上下文/用户/助手/工具），工具行展示 参数→结果摘要；顶部分阶段时间条（输入/模型/工具分段着色、宽度对应耗时，支持时长/轮次/调用切换），配"收起所有轮次/调用"、关键字过滤、按请求跳转；数据全部来自现有 timeline。
- P3-2 每轮"上下文注入 · @xxx"折叠行，列出实际注入的系统提示词、skill 目录、项目规则（接 context projection 组成）。
- P3-3 右侧栏：工作区文件树+多标签页，md 渲染预览、代码高亮、PDF 占位；文件变更类工具执行后对应文件高亮可跳转；面板可全屏可关。
- P3-4 审批卡片进对话流：工具名/目标/参数 diff/风险说明+批准/拒绝+scoped 范围说明，批准后原地变"已批准·时间"；严格走现有 approvals API 与 action_args_digest 校验，禁止绕过 digest。
- 验收：5231 事件级大会话轨迹可过滤可跳转；审批走通批准/拒绝两条路径。

### P4 性能、可达性、回归
- P4-1 虚拟滚动，5000+ 事件会话滚动 60fps；图片/代码块懒渲染。
- P4-2 滚动契约：上翻不被新消息拽底、显示"回到底部"，置底时新 token 平滑跟随；写测试固定。
- P4-3 Enter 发送/Shift+Enter 换行（可设置切换）、Ctrl+K 聚焦搜索、Esc 关弹层、全键盘可用、焦点可见、对比度 AA。
- P4-4 全量中文；相对时间（刚刚/x 分钟前）悬浮显示绝对时间。
- P4-5 重构 main.tsx 单文件为 components（chat/trajectory/composer/sidebar/right-panel）、state、api、styles 分目录，styles.css 同步拆分；FastAPI 托管 dist 的路径与方式不变。
- 验收：见 E 全部门禁。

## E. 自验证门禁（必须全部通过，失败自行修复后重跑，不许跳过）

1. `web/` 下 `npm run build`：tsc 零类型错误、vite 构建成功。
2. 新增前端组件/状态机测试全部通过（沿用仓库现有测试栈，缺最小测试设施则补齐）。
3. 后端：运行与本次改动相关的 pytest，并完整跑 `python -m pytest tests/runtime_rebuild -q`，必须全绿——证明前端/广播改造没有污染 runtime 契约；任何既有测试变红必须修复或证明与本次无关（写进报告）。
4. ruff 对改动文件零新增告警。
5. 真实启动端到端自检：启动 cc WebUI，浏览器/HTTP 实测并截图存 `docs/audits/screenshots/final/`：①简单问答逐字流式且思考块独立折叠；②两步工具任务的工具行实时出现→折叠→展开；③中途停止显示"已停止"；④/ 面板、@芯片、模型/模式菜单、状态条实时数据；⑤对话/轨迹双视图；⑥审批卡片（可构造一个待审批会话）；⑦刷新页面后 SSE 以 Last-Event-ID 正确恢复、无重复消息。无法用浏览器自动化时，用 SSE 直连（curl 或脚本）验证增量事件顺序与终态对账，并在报告注明验证方式。
6. 自检发现的问题全部修复后重跑 1–5，直到连续一轮全绿。

## F. 不可逾越的硬约束

1. 不改 run_events schema 与 run_model.py 状态机迁移白名单，不加持久流式事件；AssistantMessageCommitted 是唯一权威正文。
2. 不改 worker 恢复/租约/审批/压缩的既有语义；前端任何动作只能经现有 HTTP API，不许前端拼事件或直连 SQLite。
3. 保留并继续使用 dompurify 消毒；reasoning 原文不持久化。
4. 不删改无关代码、不动 eval/benchmark、不改用户 .env 与其他项目文件；新文件只落在 web/、cc_harness 必要处、docs/audits、docs/design。
5. 所有数字/字段必须有后端来源，查不到就隐藏并记录，禁止编造；代码里确实不确定的点写"此处未明确，推测为……"并给出验证过程，禁止假装确认。

## G. 最终交付（全部完成后一次性输出）

1. 分支与提交清单；2. `docs/audits/web-ux-gap.md`、`docs/audits/web-ux-decisions.md`、`docs/design/web-streaming.md`、`docs/audits/web-ux-final-report.md`；3. 最终报告包含：任务项完成矩阵（编号|实现摘要|关键 file:line|验证方式与结果）、未完成项及原因、E 节全部命令的输出摘要、改造前后对比截图索引、"因 runtime 架构差异刻意不做"的条目及理由；4. 用三句话总结用户可感知的最大变化。现在开始，直接一路做到 G，中途不要向我提问。
