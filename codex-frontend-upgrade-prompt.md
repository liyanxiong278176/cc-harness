# 给 Codex 的任务书：对标 DeepSeek Harness（dsh）重做 cc-harness Web 交互体验

> 用法：把「==== 提示词开始 ====」与「==== 提示词结束 ====」之间整段粘贴给 Codex，工作目录为 `D:\agent_learning\cc-harness`。参考实现位于 `D:\agent_learning\deepseek-harness-reference`（只读对标，不许整体拷贝）。

==== 提示词开始 ====

你是一名资深前端/交互工程师。cc-harness 是一个事件溯源的 Durable Agent Runtime（Python，FastAPI 承载 WebUI），它的运行时能力（持久化、崩溃恢复、租约、压缩、审批）已经很完整，但 **Web 交互体验明显落后于参考产品 DeepSeek Harness（下称 dsh）**。你的任务是：先亲自对比两个产品，再按本任务书分阶段重做 cc-harness 的 Web 前端，**第一优先级是"模型实时回复"的真实流式体验**，然后是过程透明化、布局与操作效率。不许重写后端 runtime，不许破坏事件溯源契约。

## 0. 先做对比调查（没有完成调查不许写代码）

### 0.1 两个本地实例都要亲手用一遍
- cc-harness：`http://127.0.0.1:3080/`（本机已在跑；若没跑，在仓库根目录用项目既有方式启动 WebUI）。
- dsh 参考：`http://127.0.0.1:3180/?token=mS6L1qIGeSdEii7rCvb8IjUKdT-LQ_qKk9DTH0AeQag`（本机已在跑；若端口变动，在 `D:\agent_learning\deepseek-harness-reference` 用 `pnpm dsh web --no-open --port 3180` 启动，DSH_HOME 指向其 `.dsh-home-observe`）。
- 在两个产品里各做三件事并截图/录屏记录：①发一句简单自我介绍，全程盯流式过程；②发一个需要 Glob+Read 两步工具的任务；③生成中途点停止。

### 0.2 读代码，定位现状（这是已确认的事实，先复核再动手）
- cc 前端：`web/src/main.tsx`（约 74KB 单文件）、`web/src/styles.css`（约 75KB），React 18 + Vite + TS，marked + dompurify + lucide-react。
- cc 后端 SSE：`cc_harness/webui.py` 的 `/api/sessions/{run_id}/events`（约 1402–1440 行）——它每 **0.5 秒轮询一次已提交事件树**推给浏览器，**没有 token 增量通道**；worker 在 `cc_harness/worker.py` 里把整段 LLM 流消费完之后才追加 `AssistantMessageCommitted`。这就是"界面先卡 'Runtime 正在工作'，然后整段答案突然蹦出来"的根因。
- cc 的 LLM 适配层 `cc_harness/llm.py` 其实已经逐 chunk 拿到了 content / reasoning_content / tool_call_delta（流式 StreamEvent），只是这些增量没有离开 worker 到达前端——**数据在源头是有的，缺的是转发通道与前端消费**。
- dsh 参考实现（只读学习其设计，不复制代码）：
  - `packages/client/ui-chat/src/client/chat/`：ChatView、AssistantMarkdown、ReasoningRow、MessageItem、StatsPills、TurnUsagePanel、ContextInjectionRow、register-node-renderers；
  - `packages/client/ui-tool/src/client/tool/`：ToolCallTree、ToolRow 与 per-tool 视图（read-row/bash-sample/file-mutation-row/search-row/todo-row）；
  - `packages/client/ui-trajectory/src/client/`：TrajectoryView/TrajectoryTimeline（分阶段时间条）；
  - `packages/client/ui-conversation/src/client/`：input/editor（@ 芯片的 ContentEditable）、queue/QueueDock、skeleton（EmptyHero/InputBar/ContextMeter）；
  - 学习重点：AssistantMarkdown 如何把一条助手消息拆成 reasoning / tool-call / prose 类型化 block 分别渲染，如何用 memo 化的 MarkdownText 避免每个 token 重建整棵 markdown 树，`streaming`/`interrupted` 状态如何驱动光标与"已停止"标记。

### 0.3 产出一份《交互差距清单》markdown（放 `docs/audits/web-ux-gap.md`）
按下表逐行填写：观察到的现象 | dsh 的做法（带其模块路径）| cc 现状（带 file:line）| 根因层次（传输层/状态层/渲染层/样式层）| 改造项编号。后续每个 commit 都要能回链到这里的编号。

## 1. 设计原则（贯穿所有阶段）

1. **持久事实与瞬时信号分离**：run_events 事件流是唯一持久事实源，绝不允许为了流式效果把半截 token 写进事件流；新增的增量信号是"易失广播"，断线/刷新后以已提交事件为准做对账（reconcile）。
2. **通道分离**：正文（content）、思考（reasoning）、工具调用（tool call）是三条独立视觉通道，思考默认流式可见、回合结束自动折叠为"已思考"，**绝不能像现在这样把英文推理过程平铺进回答正文**。
3. **过程即界面**：工具调用在发生时就以"图标 + 工具名 + 关键参数"逐行出现，回合结束自动折叠为"N 次工具调用"，点击就地展开入参/结果；大结果显示"显示 100 / 共 312"式的有界预览与溢出落盘提示。
4. **任何时候用户都知道系统在干什么**：状态语要具体（"正在调用 Glob…""正在读取 xxx.md"），不要只有一个转圈；底部常驻 轮/步、速率、累计 token、上下文占用；停止是即时的且留下"已停止"标记。
5. 视觉语言对齐 dsh 的克制风格：深色主题、窄图标侧栏 + 会话侧栏 + 主区 + 可开右侧栏、圆角气泡、等宽代码芯片；但保留 cc-harness 自己的品牌，不要照抄 logo/文案。
6. 中文界面；所有 markdown 仍走 dompurify 消毒；不允许新增重量级框架（不要引入 Redux/antd 之类），优先在现有 React + CSS Modules/普通 CSS 体系内完成，确需新依赖先在任务书中说明理由。

## 2. 分阶段任务（按顺序交付，每阶段都要可运行、可回滚、带测试）

### 阶段 P0 — 真实流式管线（最高优先级，没做好后面都没意义）

P0-1 后端增量广播：在 worker 消费 LLM 流的循环里（`llm.py` StreamEvent → `worker.py` 模型段循环），把 content delta、reasoning delta、tool_call_delta、阶段状态变化通过一个**进程内 pub/sub 广播器**（按 run_id 订阅，asyncio.Queue 扇出）实时推给 WebUI 层；广播消息带 run_id、segment 序号、单调 chunk 序号，类型如 `assistant.delta`/`reasoning.delta`/`tool.pending`/`segment.phase`。广播器只在内存，不写 SQLite、不进 run_events。
P0-2 SSE 改造：`/events` 端点从"0.5s 轮询事件树"改为"订阅广播器即时推送增量 + 已提交事件仍走事件树（可保留低频对账或事件追加即触发）"；保留 `Last-Event-ID` 断线续传与 heartbeat；客户端重连后先用 timeline 接口对账，把"流式气泡"替换为已提交内容，去重不闪烁。
P0-3 前端流式渲染：主区为当前未完成回合维护一个 streaming 消息节点，delta 到达时**追加文本而非整体替换**；正文逐 token 上屏并带闪烁光标，结束时用 `AssistantMessageCommitted` 的权威内容一次性替换（对账时校验长度/末尾，不一致以提交版为准，并在 dev 控制台 warn）。
P0-4 增量 Markdown 性能：marked 全量重解析必须做节流（如 rAF 合并、≥30ms 一帧），组件级 memo，避免长回答时输入掉帧；代码块未闭合期间不要闪烁坏样式（参考 dsh MarkdownText 的稳定组件表做法）。
P0-5 思考通道独立：reasoning.delta 渲染为独立"思考"块，流式时默认展开、等宽/弱化色，回合结束自动折叠为"已思考 >"，可再展开；**reasoning 不持久化到会话记录**（遵守 cc 既有 ADR-0009 不存推理原文的约束——广播即焚，刷新后不要求恢复思考原文，恢复时只显示"已思考"）。
P0-6 停止语义：点停止立即调用现有 stop 接口并立刻冻结流式气泡、打上"已停止"终态标记，不允许出现现在这种答案已出完、底下"Runtime 正在工作"还在转、状态最后变成"已暂停/继续"的割裂感；停止后的半截内容保留可见。
P0-7 验收：①简单问答时文字必须逐词出现，肉眼无整段蹦字；②主动断网 3 秒再恢复，不重复不丢已提交内容；③同一任务对比改造前后录屏；④pytest 覆盖广播扇出、SSE 断线续传、对账去重；前端组件测试覆盖 delta 追加与权威替换。

### 阶段 P1 — 消息与工具过程的结构化渲染

P1-1 把一条助手回合建模为类型化 block 列表：reasoning / tool-head / tool-result / prose / status，分别由独立组件渲染，替换现在一个大 marked 字符串的做法。
P1-2 工具行实时化：工具开始即出现一行（lucide 图标按工具族区分：搜索/读文件/写文件/命令/网络…+ 工具名 + 一个关键参数，如 `读取 · 01-theory.md`）；执行中转动效；结束变稳。多工具按时间顺序穿插在思考与正文之间，和 dsh 一致。
P1-3 回合结束自动把本回合工具行折叠为"N 次工具调用 >"，点击就地展开（不是跳页）：入参 JSON 树、结果有界预览、复制按钮；超过阈值的结果显示"显示 x / 共 y 行"与"完整结果已落盘，可用 offset/limit 续读"的提示（cc 已有 chunk/offload 机制，把它在 UI 上显式化）。
P1-4 工具结果中的文件路径渲染为可点击芯片（点击在右侧文件面板打开）；命令类工具展示退出码与耗时，失败行用错误色且可展开 stderr。
P1-5 用户消息修正：去掉重复的"你 你"标签；消息悬停操作统一为 复制 / 好回答 / 差回答 / 从此分支（branch，复用 cc 的 follow-up/child run 能力）/ 重新生成；每条助手消息尾部展示"用量 xxK tok · 用时 x 秒"（数据来自 ModelInvocationFinished/usage 事件，缺失则不显示，不许编造数字）。
P1-6 空状态与自动标题：新会话空主区用居中 hero（标题 + 工作区/模式选择 + 3 张可点建议卡，保留 cc 现有"了解项目结构/运行测试并修复/审查当前改动"但做成 dsh 那样的整行可点卡片）；首轮结束后自动生成会话标题（cc 已有标题能力的话直接用，没有则用首条消息截断），替换侧栏里整段任务名当标题的现状。

### 阶段 P2 — 框架、输入区与状态条

P2-1 整体框架：最左 56px 图标轨（logo/新会话/搜索/设置）+ 可折叠会话侧栏（按项目分组，状态点：运行中/排队/等待审批/已暂停/失败，带事件数）+ 主区 + 可开关右侧栏；主区顶部是"项目 › 会话名"面包屑与全局操作；记忆侧栏展开宽度、收起动画与 dsh 对齐。
P2-2 输入区（composer）：多行自动增高；左下角 `+` 添加上下文；**`/` 命令面板**分组呈现（添加类：文件/目标/计划；指令类：压缩 compact、权限 permission、模型 model、导出 export 等，全部映射到 cc 已有 API，没有的指令不许加假入口），支持 ↑↓ 键选择、Esc 关闭；**`@` 唤起工作区文件提及**，选中后渲染为不可编辑芯片（参考 dsh chip-node/ReferenceChip），发送时带文件引用。
P2-3 输入区底部工具行：访问模式三档（仅可查看 / 工作区内修改 / 完全权限，对应 cc 的权限预设与沙箱策略，当前选中文案常驻）、模型选择器（模型 + 推理等级两级菜单，读 cc settings）、上下文占用百分比（已有 context 接口就接上）；发送键在生成中变为方形"停止生成"，状态切换无延迟。
P2-4 底部状态条：左到右 运行状态（颜色点 + 文案）、连接状态、权限模式、上下文环、模型名；**生成中在右侧实时显示"N 轮 M 步 · xxx tok/s · 累计 tok · 缓存命中 x%"**（tok/s 用 delta 字节数实时算，轮/步与 token 累计以提交事件为权威校准；缓存命中取 usage 字段，没有就隐藏该项）。
P2-5 "继续/已暂停"语义整改：cc 现在每轮结束 run 就 yielded/paused、需要用户点"继续"是架构特性，但 UI 上不能让用户以为是卡死——明确区分"等待你输入（可直接打字）"与"运行中断需要点继续"，并在状态条与 composer 上给出一致入口；非必要不弹"继续"按钮，能直接输入续聊就直接续。
P2-6 降级/错误可见但不吓人：像"沙箱不可用·已降级本机"这类状态用可点查详情的细粒度警示条，不要在底部常驻一串红字；LLM 失败、重试中（cc llm.py 有 3 次重试）要显示"网络异常，第 2/3 次重试…"，最终失败给出可点的"重试本轮"。

### 阶段 P3 — 轨迹视图与透明感

P3-1 主区顶部加"对话 / 轨迹"双 tab。轨迹视图：按轮次（第 N 轮）分组，每行带角色色签（系统/上下文/用户/助手/工具），工具行展示 参数 JSON → 结果摘要；顶部一条**分阶段时间条**（输入/模型/工具分段着色、长度对应耗时，支持"时长/轮次/调用"切换），下方提供"收起所有轮次/调用"、关键字过滤、按请求序号跳转。数据全部来自 cc 的 timeline 事件树，不新增持久化。
P3-2 上下文注入透明：每轮把实际注入的系统提示词、skill 目录、项目规则等以"上下文注入 · @xxx"折叠行展示（对应 cc 的 context projection 组成），点击可看清单，让用户知道每轮到底喂了什么。
P3-3 右侧栏：文件树（复用 cc 工作区文件 API）支持多标签页打开，md 渲染预览、代码文件等宽高亮、PDF 占位；文件变更类工具执行后对应文件高亮/可跳转；面板可全屏、可关。
P3-4 审批卡片重做：等待审批时在对话流内出现卡片（工具名、目标、参数 diff、风险说明），批准/拒绝按钮与 scoped 范围说明，批准后卡片原地变为"已批准 · 时间"；拒绝留原因。对接现有 approvals API（含 action_args_digest 校验），不许绕过 digest 直接放行。

### 阶段 P4 — 性能、可达性与回归

P4-1 长会话虚拟化：事件/消息超过 200 条后虚拟滚动，保证 5000+ 事件会话（如测试里那个 5231 事件的 run）滚动不掉帧；图片/代码块懒渲染。
P4-2 滚动契约：用户向上翻看历史时不被新消息强制拽到底部，出现"回到底部"悬浮按钮；用户在底部时新 token 平滑跟随。写前端测试固定这个契约（dsh 有 chat-scroll-contract e2e，可对标）。
P4-3 键盘：Enter 发送 / Shift+Enter 换行（设置可切换）、Ctrl+K 聚焦搜索、Esc 关弹层、命令面板全键盘可用；焦点可见、对比度达 AA。
P4-4 国际化与文案：全量中文，数字/时间格式统一（相对时间"刚刚/x 分钟前"，悬浮显示绝对时间）。
P4-5 测试与验收门禁：`web/` 下 `npm run build` 与 tsc 零错误；为每个 P 级新增组件测试（vitest + testing-library，如未安装先按现有技术栈补齐最小配置）；保留并更新现有 e2e/手工验收清单；后端新增的 pub/sub 与 SSE 改造加 pytest，且**既有 tests/runtime_rebuild 全部绿灯，证明前端改造没有反向污染 runtime 契约**。

## 3. 硬性约束（违反即返工）

1. 不改 run_events 的 schema 与状态机（`cc_harness/run_model.py` 的迁移白名单），不为流式增量加持久事件；AssistantMessageCommitted 仍是唯一权威正文。
2. 不改 worker 的恢复/租约/审批语义；前端任何"重试/继续/批准"都只能调既有 API，不允许前端直接拼事件。
3. 参考 dsh 只学交互模式与信息架构，**不许拷贝其源码**（许可证与品牌都不同）；视觉与命名保持 cc-harness 自身风格。
4. 不删除现有 web/dist 构建产物的发布方式（FastAPI 托管静态产物），改造后必须能离线打包、由现有启动命令直接托管。
5. 每个阶段先开 draft 方案（组件树 + 数据流图 + API 增改清单）给我确认，再写实现；commit 按 P0-1、P0-2… 粒度切分，commit message 带差距清单编号。
6. Windows 环境，路径与换行按 Windows 实测；不允许引入只在 macOS 成立的快捷键/路径假设。
7. 任何"代码里找不到依据"的行为（比如 usage 字段到底有没有缓存命中）必须标注"未确认"并先查后端，不许在前端编造数据。

## 4. 最终交付物

1. `docs/audits/web-ux-gap.md`：0.3 的差距清单（带双侧 file:line 与截图引用）。
2. 分阶段实现代码 + 每个阶段的自测录屏/截图说明（怎么手动复现验证）。
3. `web/src/` 从单文件 main.tsx 重构为按功能分目录的结构（components/chat、components/trajectory、components/composer、components/sidebar、components/right-panel、state/、api/、styles/），styles.css 同步拆分；保持构建产物路径不变。
4. 一份 `docs/design/web-streaming.md`：说明易失增量广播 vs 持久事件的边界、SSE 对账协议、前端 streaming→committed 状态机（含断线、停止、失败重试三态转换图）。
5. 最终对比报告：逐项列出 dsh 有的体验点、cc 现在的实现方式、验收方式（测试 node id 或操作步骤），并单列"因 runtime 架构差异暂不实现"的条目及原因。

==== 提示词结束 ====

## 附：我实测后的关键结论（供你判断任务书是否符合预期，不用贴给 Codex）

- dsh 流式：token 逐字上屏；思考单独成块、结束自动折叠；工具调用逐行冒出来再折叠；底部实时"轮/步 · tok/s · 累计 token · 缓存命中"；停止即时且标"已停止"。
- cc 现状根因不是前端不会渲染，而是**增量根本没出后端**：worker 吞完整段流才提交事件，SSE 还是 0.5s 轮询，所以 P0 必须同时动 worker 广播 + SSE + 前端三层，只改前端没用。
- dsh 值得对标的信息架构：对话/轨迹双视图（轨迹带分阶段时间条）、上下文注入透明行、斜杠命令面板、@文件芯片、模型/推理等级两级菜单、三档访问模式、右侧多标签文件面板、消息分支、每条消息的用量/耗时。
- dsh 已在本机 3180 端口运行，可随时亲自对照。
