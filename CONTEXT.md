# cc-harness

cc-harness 是在用户当前终端中运行的编码代理。其交互语言强调由应用接管当前终端表面的会话，而不是独立窗口或 Web 图形界面。

## Language

**产品对标边界（Product Parity Boundary）**:
cc-harness 对标 Claude Code 的终端 coding-agent 内核及 headless/SDK 能力；Web、Desktop、Mobile、Slack、Chrome 与完整企业平台不属于当前对标范围。_Avoid_: 完整复制 Claude 平台、只对齐终端外观

**可信内核阶段（Trusted Core Phase）**:
第一阶段优先完成原生文件与搜索工具、不可绕过的 hard-deny 与 sandbox、模型和成本追踪、headless JSON 协议及持续 coding benchmark，再建设 Skills、Hooks 与增强型 Subagents；UI 仅修复阻塞真实使用或验证的问题。_Avoid_: 用像素级界面对齐代替底层能力、在核心契约未成立前继续扩大 UI 表面积

**对标发布门（Parity Release Gate）**:
使用固定版本的 Claude Code，在相同代码任务、模型条件、环境与资源预算下持续运行可复现对照评测；同模型榜与等预算榜均以 `deepseek-v4-flash` 为基准，记录服务端返回的具体模型版本，Claude Code 通过 DeepSeek 的 Anthropic-compatible endpoint 运行。成功率至少高 5 个百分点，或成功率差距不超过 3 个百分点且成本或耗时至少降低 20%，才可称为“超过”；安全越界、数据丢失与会话恢复回退不能被综合分抵消，结论必须经过重复运行和置信区间检验。_Avoid_: 按功能数量宣称领先、只展示有利样例、混合模型版本、用效率抵消关键安全或恢复缺陷、改变环境后直接比较

**隔离对标运行（Isolated Parity Run）**:
常规 coding benchmark 从冻结仓库快照、全新 worktree、固定依赖与空白会话开始，关闭跨运行长期记忆并记录缓存状态；只有专门的记忆评测允许继承历史。_Avoid_: 复用上次修改、让长期记忆污染常规任务、双方使用不同依赖或初始状态、隐藏 warm-cache 优势

**升级兼容边界（Upgrade Compatibility Boundary）**:
`0.1.x` 可信内核改造可以调整未公开的内部 API 与配置结构，但必须迁移或兼容已有会话、记忆、项目配置和已文档化 CLI 行为；无法无损迁移时必须在执行前明确报告并提供恢复路径。_Avoid_: 为保留错误内部设计阻止必要重构、静默丢弃用户数据、无提示改变公开命令语义

**可回滚发布（Rollbackable Release）**:
发布包经过签名并区分 stable/canary 通道，支持版本固定、原子升级和一键回滚，升级失败时保留旧程序、harness profile 与用户数据；运行中的任务不被无提示切换版本。_Avoid_: 强制静默升级、覆盖唯一可运行版本、回滚时降级用户数据库、长任务中途改变 harness

**终端内会话（In-Terminal Session）**:
在调用命令的当前终端内持续呈现对话、工具活动和输入区。默认 fullscreen renderer 使用 alternate screen 接管当前终端表面；classic renderer 作为兼容路径把历史写入原生 scrollback；两者退出后都恢复调用者的 shell。
_Avoid_: 另开终端窗口、桌面 GUI、Web UI、退出后破坏原终端状态

**一等本地平台（First-Class Local Platform）**:
Windows、Linux 与 macOS 都必须通过核心工具、路径安全、会话恢复和 CLI 发布门；Windows 支持原生 PowerShell、PTY 与文件语义，不以 WSL 作为运行前提。_Avoid_: 把 Windows 当作尽力兼容、仅在单一开发机验证、用 WSL 结果代表原生 Windows

**`cc-harness` 命令**:
用户在任意项目目录启动原位终端会话的正式命令；`python main.py` 仅作为源码开发与兼容入口。
_Avoid_: 将 `main.py` 视为正式用户入口

**启动面板（Startup Panel）**:
每次开始原位终端会话时显示的品牌与环境摘要；其字符栅格、分区、边框、间距和状态层级以 Claude Code 经典终端界面为视觉基准，但名称、版本、像素图标、提示和更新内容必须属于 cc-harness。
_Avoid_: Web 首页、全屏仪表盘、复制 Claude 名称、图标或发布内容

**月薪喵像素吉祥物（Yuexin Cat Pixel Mascot）**:
启动面板左栏使用的彩色半块像素画；从用户提供的参考图提取米色头部、棕色轮廓、蓝色眼睛、白色身体与遮脸前爪，不重新分发原始位图。
_Avoid_: 泛化猫脸字符、Claude 像素图标、嵌入网络原图、无法在普通终端稳定对齐的图片协议

**已接通能力（Wired Capability）**:
从终端交互一直连接到真实代理后端、会产生可验证结果的用户能力；仅改变界面文字或展示占位数据不属于已接通能力。
_Avoid_: UI 占位、模拟状态、未实现但出现在帮助中的命令

**原生核心工具（Native Core Tool）**:
由 cc-harness 内置并维护的基础 coding 工具，共享统一的路径规范化、权限判定、diff、checkpoint、取消、结构化事件和测试契约；第一阶段锁定 `Read`、`Edit`、`Write`、`Glob`、`Grep` 与 `run_command` 的本地闭环，随后接入 `WebFetch`、`WebSearch` 和 LSP。MCP 用于第三方及领域能力扩展。_Avoid_: 要求用户配置 MCP 才能完成基本编码、让各 MCP 实现分别定义安全与修改语义、在本地工具契约未稳定前混入网络和语言服务器生命周期

**供应商中立模型内核（Provider-Neutral Model Core）**:
会话、工具、权限、记忆、任务与评测运行在不绑定单一模型厂商的共享 agent 内核上，用户可提供自己的供应商凭据；每个 provider/model 通过能力档案声明真实上下文窗口、工具调用、视觉、推理、缓存、结构化输出和价格能力，运行时与界面仅启用其实际支持项。_Avoid_: 将核心绑定 Anthropic、只允许修改 OpenAI-compatible base URL 却忽略模型差异、用最低共同能力限制所有模型、展示虚假的上下文或成本

**会话资源预算（Session Resource Budget）**:
用户为会话或后台任务设置 token、费用、总耗时和工具调用次数上限，接近上限时预警，达到上限后停止启动新模型或工具调用并保存可恢复状态；已启动副作用调用按真实结果或 `outcome_unknown` 收尾。_Avoid_: 超额后继续调用、用估算成本冒充供应商账单、预算停止时丢失任务状态、把被中断调用标记为未执行

**显式模型回退（Explicit Model Fallback）**:
跨模型自动切换只在用户预先配置 fallback 链、费用上限与允许的能力降级后发生，并在事件流、界面和评测中记录实际 provider/model；同模型暂态错误可按受控策略重试。_Avoid_: 静默换模型、恢复会话后偷偷改变模型、fallback 超出预算、把混合模型结果记到单模型榜

**会话 Harness 档案（Session Harness Profile）**:
会话固定系统提示词、工具 schema、压缩 prompt、模型能力、reasoning effort 与安全策略的版本 fingerprint；恢复时沿用原档案，升级只能显式迁移，评测结果必须保存同一 fingerprint。_Avoid_: 恢复后静默切换提示词或 effort、只记录模型名、无法定位 harness 回退、用不同档案结果直接比较

**项目会话（Project Session）**:
归属于一个项目目录的独立对话上下文；普通启动创建新会话，继续或恢复必须由用户通过对应命令显式选择。
_Avoid_: 跨项目共享当前对话、普通启动时隐式续接

**工作目录（Working Directory）**:
启动 `cc-harness` 时所在或由 `--cwd` 显式指定的目录，是会话归属和默认文件权限范围；不会自动提升到 Git 根目录。
_Avoid_: 自动发现的仓库根、进程安装目录

**附加目录（Additional Directory）**:
用户通过 `--add-dir` 明确加入当前会话文件权限范围的额外目录，并在启动面板中可见。
_Avoid_: 隐式扩大工作目录、自动加入 Git 根目录

**排队消息（Queued Message）**:
代理执行期间已提交但尚未开始处理的用户消息；同一项目会话始终只有一个活动轮次，排队消息按提交顺序运行。
_Avoid_: 并发轮次、未提交草稿

**权限模式（Permission Mode）**:
当前项目会话对可询问操作的批准策略，分为逐次确认、项目内编辑免询问和全部可询问操作免询问三档；任何一档都不能覆盖硬拒绝与安全边界。
_Avoid_: 将免询问称为关闭安全保护

**硬安全边界（Hard Safety Boundary）**:
在权限询问与会话 allowlist 之前执行的不可批准拒绝规则，覆盖未显式授权的工作目录外访问、敏感凭据读写或外传、sandbox 强制限制及其他明确禁止的副作用；`bypass-prompts` 只自动批准 `ASK`，不能把 `DENY` 转为允许。用户只能通过显式扩大允许根目录等受审计配置改变边界。_Avoid_: 把危险提示当作拒绝、让日常权限切换关闭安全保护、用一次工具确认临时扩大信任边界

**分层故障策略（Tiered Failure Policy）**:
路径边界、hard-deny、sandbox 与敏感数据保护等强制安全控制异常时 fail-closed；摘要、记忆、反思、建议和注入分类等辅助能力异常时使用上一有效状态或带可见警告降级。_Avoid_: 安全检查异常后放行、辅助服务故障拖垮 agent loop、静默忽略降级、用单一 fail-open/fail-closed 规则覆盖所有组件

**作用域凭据代理（Scoped Credential Broker）**:
认证工具按工具、目标和时限获得临时凭据，长期密钥不进入模型上下文、工具参数、日志或 transcript；模型直接读取密钥文件由 hard-deny 拒绝。_Avoid_: 将 API key 注入通用 shell 环境、在错误输出中回显凭据、让一次授权适用于任意域名或工具、把输出脱敏当作唯一保护

**受控网络出口（Controlled Network Egress）**:
sandbox 默认无网络，只有用户或工具策略明确授权且经重定向与 DNS 解析地址复核的目标域名可访问；云元数据、回环和未授权私网地址默认拒绝，Web 工具通过同一受控代理联网。_Avoid_: 给通用 shell 任意出口、只检查初始 URL、不复核 DNS 或重定向、用前端提示代替网络强制策略

**显式宿主执行（Explicit Host Execution）**:
`run_command` 默认在 sandbox 中运行且 sandbox 不可用时 fail-closed；宿主机 native executor 只能由用户通过独立醒目的启动选项启用，模型不能切换，并继续受 hard-deny 与审计约束。_Avoid_: sandbox 故障后自动降级、把权限免询问当作宿主执行授权、由模型选择执行后端、原生模式关闭所有安全规则

**来源感知信任（Provenance-Aware Trust）**:
本地用户明确输入的控制指令是受 hard-deny 约束的可信意图；网页、文件、附件和工具结果属于不可信数据，不能通过其中的文本提升指令权限。安全判断保留内容来源与权限层级，而不是仅凭可疑关键词硬阻断用户整条输入。_Avoid_: 把研究 prompt injection 的用户请求当作攻击、让外部内容冒充用户或系统指令、judge 异常时静默改变权限

**权限卡片（Permission Card）**:
当真实工具调用需要授权时替代普通输入区的交互卡片，展示工具、命令或路径、用途和触发原因，并提供仅本次允许、按最小明确范围记住允许、拒绝并反馈三类选择。方向键选择、Enter 确认、Esc 拒绝；Bash/PowerShell 可用 Ctrl+E 展开按 Low/Med/High 标注的风险解释。卡片外观与 Claude Code 同构，但判定和持久化由 cc-harness 权限引擎负责，任何模式都不能越过硬拒绝、路径边界、sandbox 或敏感数据保护。
_Avoid_: 模糊的全局“永远允许”、未显示实际目标、把界面选择当作绕过安全策略、授权时同时提交输入草稿

**结构化提问卡片（Structured Question Card）**:
代理在活动回合中需要澄清时暂停执行并替代普通输入区的卡片；单次承载 1–4 个问题，每题包含短标题、完整问题和 2–4 个带说明选项，多问题用页签切换，多选用 Space 切换，并始终提供支持多行的 Other 自由答案。Enter 提交后把真实问答写入回合记录并恢复同一回合，Esc 取消等待且不得生成默认答案。
_Avoid_: 把问题当作新回合结束、跳过必答项、自动选择、未提交答案进入模型上下文、提问时同时提交普通草稿

**计划审批卡片（Plan Approval Card）**:
Plan 模式完成探索后展示真实 Markdown 计划及计划文件路径，并暂停在审批卡片。用户可批准后切换到自动编辑、批准后逐项审阅、带反馈继续规划，或取消但保留计划；Ctrl+G 可在外部编辑器修改计划。批准、反馈和取消都延续同一会话，只有批准会退出 Plan 模式，且不得展示没有真实后端能力的云端入口。
_Avoid_: Plan 模式修改源码、生成计划后自动执行、审批选项与实际权限模式不一致、无效的 Ultraplan 按钮

**文件修改活动（File Change Activity）**:
Edit/Write 等工具在回合中展示真实目标路径、运行状态和由修改前后磁盘内容计算的新增/删除统计；默认呈现带语法及词级增删高亮的有限 diff 上下文，大结果折叠且可展开。Ctrl+O 提供完整工具记录，`/diff` 提供可在当前工作区总 diff、各回合和各文件间导航的交互查看器。
_Avoid_: 根据模型文本伪造成功或 diff、无限展开大文件、只显示“已修改”而不显示目标、把失败写成完成

**条件文件修改（Conditional File Mutation）**:
`Edit` 与 `Write` 携带读取时的内容版本或哈希，只有磁盘前置条件仍成立才以同目录原子替换落盘，并保留权限、编码和换行约定；发生并发变化时返回冲突并要求代理重新读取。_Avoid_: 静默覆盖用户或其他代理修改、模糊匹配到多个位置仍写入、留下半写文件、冲突后自动套用过期补丁

**工作区工具锁（Workspace Tool Lock）**:
资源独立且无副作用的 `Read`、`Glob`、`Grep` 可并行，`Edit`、`Write` 与 `run_command` 默认按 workspace 串行；只有独立 worktree 或工具声明并成功获取不相交资源锁时才允许写入并行。_Avoid_: 根据 shell 文本猜测只读、并行写同一工作区、锁冲突后继续执行、取消时丢失各调用结果

**用户配置（User Configuration）**:
适用于所有项目的 cc-harness 凭据、默认模型和 MCP 设置，位于用户主目录的 `.cc-harness/` 中。
_Avoid_: 项目配置、会话数据

**项目配置（Project Configuration）**:
只影响当前项目目录的模型覆盖、MCP 设置、策略、任务和会话数据；同名设置按约定覆盖用户配置。
_Avoid_: 用户配置、跨项目默认值

**项目指令（Project Instructions）**:
位于工作目录根部的 `CC-HARNESS.md`，记录代理在该项目中始终需要遵守、且无法仅从代码推导的约定；可由 `/init` 创建并由用户维护。
_Avoid_: CLAUDE.md、项目配置、自动生成的代码库目录清单

**Claude 配置兼容导入（Claude Configuration Compatibility Import）**:
用户显式请求时，将 `CLAUDE.md`、`.claude/skills/`、Hooks 和 Subagent 定义映射到 cc-harness 的原生指令与扩展契约；`CC-HARNESS.md` 及 cc-harness 命名空间保持权威，导入内容接受相同的权限与安全检查，Hook 不会因发现文件而静默执行。_Avoid_: 自动执行外部 Hook、把 Claude 私有格式作为唯一存储格式、导入时绕过权限、静默覆盖 cc-harness 原生配置

**本地扩展包（Local Extension Package）**:
从项目目录、本地路径或 Git 来源安装的统一 Skills、Hooks 与 Plugins 单元，声明版本、能力和权限并接受锁定与完整性校验；官方托管 Marketplace 不属于扩展协议稳定前的产品范围。_Avoid_: 未声明权限的可执行 Hook、无版本浮动安装、把插件商店当作扩展运行时、发现文件即自动执行

**发布摘要（Release Summary）**:
随 cc-harness 版本发布的用户可见变更记录；启动面板只展示最近三项，`/release-notes` 展示可选择的完整版本记录。
_Avoid_: Claude Code 发布说明、硬编码的占位新闻、Git 提交日志

**初始化向导（Setup Wizard）**:
首次运行且没有可用模型配置时，在原位终端会话中安全收集用户级连接信息的引导流程。
_Avoid_: 配置错误堆栈、自动复制项目密钥

**打印模式（Print Mode）**:
面向管道和自动化的非交互运行形态，只输出最终回答并用进程退出码表达结果。
_Avoid_: 无头 TUI、带 ANSI 动画的脚本输出

**无头事件协议（Headless Event Protocol）**:
脚本、CI、IDE 与 SDK 通过带版本的结构化 JSON/JSONL 契约调用和观察与 TUI 相同的 agent runtime，能够接收会话、文本、工具、权限、文件修改、用量、成本、错误和最终结果事件；已提交事件按稳定 ID 与单调序号至少交付一次，消费者使用 cursor 重放和去重，外部命令使用幂等键。_Avoid_: 仅返回最终文本、维护两套 agent 行为、无版本改变字段、把 ANSI 输出当作机器接口、声称跨进程恰好一次、重连后漏事件

**IDE 薄客户端（IDE Thin Client）**:
官方 VS Code 及后续 IDE 集成通过 Headless Event Protocol 连接本机同一 cc-harness runtime，只提供编辑器上下文、diff、导航、权限和任务交互；会话真相、模型调用与 agent loop 不进入插件。_Avoid_: 在 IDE 重写 agent、插件直接调用模型、终端与 IDE 使用不同权限或工具语义、关闭编辑器即丢失后台任务

**活动摘要（Activity Summary）**:
原位终端会话默认展示的简短执行过程，包括当前状态、工具名称、关键参数、耗时和结果摘要；完整 reasoning 与冗长工具输出属于详细模式。
_Avoid_: 完整思维链、未经折叠的工具输出

**回合记录（Turn Transcript）**:
一次已提交交互在应用会话记录中留下的可见生命周期，按 Claude Code 界面呈现唯一的用户消息、无内容的思考计时、助手 Markdown、工具活动和完成摘要；fullscreen renderer 将其投影到应用内视口，classic renderer 将其写入原生 scrollback；产品身份、数据和回答仍属于 cc-harness。
_Avoid_: 重复用户消息、裸流式文本、伪装成 Claude、显示隐藏思维链

**交互壳层（Interaction Chrome）**:
包围真实用户内容和代理内容的固定控制文案与状态标记，使用 Claude Code 经典界面的英文标签、快捷键提示和几何结构，不随对话语言本地化。
_Avoid_: 翻译 `Thought for` 等控制标签、改变用户或模型内容的语言

**已提交消息（Committed Prompt）**:
用户按 Enter 后由活动编辑内容转换成的单个全宽灰色 `❯` 消息块；它保留原始多行文字、粘贴标记和附件，并成为回合记录的起点。
_Avoid_: 同时保留编辑行并再次回显、青色重复消息、把未提交草稿写入回合记录

**可见思考区间（Visible Thinking Interval）**:
从消息提交到首个可见助手文本或工具活动之间的真实时间；超过 300ms 才显示等待态，达到 1 秒才在回合记录中保留 `Thought for Ns`，且不包含隐藏推理内容。
_Avoid_: 伪造思考时长、显示思维链、把工具执行时间计入思考区间

**回合耗时（Turn Elapsed Time）**:
从消息提交到最终结果、取消或失败的真实总时长，用于生成互斥的成功、打断或失败摘要。
_Avoid_: 固定时长、把取消标记为成功、仅计算模型生成时间

**回合异常状态（Turn Exception State）**:
活动回合被用户中断时保留已完成内容并以灰色 Interrupted 结束，Up 可恢复原始提示并回到中断前节点；工具拒绝附着真实原因且允许代理继续。可恢复 API 错误在临时区域显示真实类别、动态重试倒计时和次数并允许 Esc 终止；不可恢复错误提供可执行说明并以失败摘要结束，完整诊断只进入详细 transcript 或 debug 日志。
_Avoid_: 中断显示成功、静默重试、伪造 HTTP 状态或重置时间、默认倾倒堆栈、失败后丢失排队输入

**完成短语（Completion Flourish）**:
成功回合摘要中按会话和回合稳定选择的英文趣味动词，采用 `✻ {Verb} for Ns` 结构；词库属于 cc-harness，恢复和重绘不会改变既有选择。
_Avoid_: 每次重绘随机变化、对失败或取消使用成功动词、复制产品身份文案

**紧凑工具活动（Compact Tool Activity）**:
回合记录中以 `● Tool(key argument)` 和缩进的 `⎿ result summary` 表示真实工具生命周期；完整参数与输出保存在会话中并仅通过详细 transcript 展示。
_Avoid_: 默认倾倒 JSON、截断模型可见结果、隐藏工具错误、把完整输出从会话中删除

**副作用调用尝试（Side-Effecting Tool Attempt）**:
会修改文件、进程、网络远端或持久状态的工具调用在执行前持久化稳定 attempt ID，除非工具提供且验证幂等键，否则超时、断线或崩溃后不得自动重试，结果不确定时必须明确报告；只读调用可以按受控策略透明重试。_Avoid_: 把超时当作未执行、自动重复 shell/写入/提交/Hook、用新的 attempt ID 掩盖重复执行

**代理停滞（Agent Stall）**:
Agent 连续三次执行语义相同的失败操作，且文件、任务状态和验证证据均无可观察变化时进入 `stalled`，停止发起新调用并报告重复模式与解除阻塞所需信息。_Avoid_: 用不同措辞包装相同重试、耗尽总预算才停止、把模型自述当作进展、停滞后静默标记失败或成功

**临时回答区域（Transient Response Region）**:
首个文本到达后以 `●` 开始、可原位重绘 Markdown 的流式区域；工具边界和最终结果会把已完成内容单次提交到回合记录，不留下裸 token 或重复回答。
_Avoid_: 每个 token 形成永久历史、最终答案重复打印、没有内容的助手标记

**终端 Markdown 投影（Terminal Markdown Projection）**:
流式与最终回答共用的安全渲染规则，支持常见 Markdown、任务列表、表格和按语言高亮的代码块；窄屏表格转为纵向键值布局，CJK、emoji 与组合字符按终端 cell 宽度换行。URL 与真实文件路径生成可复制的 OSC 8 链接，路径显示可相对工作目录而目标使用规范化绝对路径；所有模型和工具内容在投影前剥离可操控终端的控制序列。
_Avoid_: 流式与最终排版不同、截断半个宽字符、模型注入光标/标题/伪链接控制码、把不存在路径做成可信链接

**活动回合消息队列（Active-Turn Message Queue）**:
代理仍在生成回复或调用工具时，输入框继续接受文字、粘贴内容、图片和附件；Enter 将完整消息加入可见的先进先出队列，当前回合结束后依次提交，Up 可取回最近一条排队消息编辑。Esc 中断当前回复或工具调用并保留已完成工作，Ctrl+C 按当前焦点清除草稿或取消活动操作。
_Avoid_: 忙碌时丢弃输入、只显示虚假的 queued 提示、丢失附件、把排队消息合并、无法取回编辑

**全屏终端渲染器（Fullscreen Terminal Renderer）**:
默认渲染路径，使用当前终端的 alternate screen 维护固定底部输入区和应用内回合视口；向上滚动时暂停 auto-follow，新输出不会抢回视口，并显示带未读计数的 `Jump to bottom`，由点击、Ctrl+End 或滚到底部恢复跟随。
_Avoid_: 另开窗口、输入框随流式输出跳动、用户阅读历史时强制滚回底部、伪造无法感知的滚动状态

**经典终端渲染器（Classic Terminal Renderer）**:
通过 `/tui default` 选择的兼容渲染路径，把已完成回合写入终端原生 scrollback，并仅原位重绘活动输入与状态；它不承诺 fullscreen 专属的应用内滚动、鼠标和浮动按钮能力。
_Avoid_: 两套业务会话语义、因切换渲染器丢失对话、声称 classic 具备无法实现的宿主滚动感知

**详细记录查看器（Transcript Viewer）**:
由 Ctrl+O 打开的 fullscreen 会话浏览面，展示真实完整工具参数、输出、耗时、错误和 diff，但继续排除隐藏思维链；支持搜索、匹配跳转、逐行/翻页/首尾/前后提示导航及快捷键帮助。`[` 可把完整展开记录临时写入原生 scrollback，`v` 可在外部编辑器打开，Esc、q 或 Ctrl+O 返回并保留浏览位置；`/focus` 仅改变投影视图，不删除底层事件。
_Avoid_: 从屏幕截图恢复、详细模式泄漏 reasoning、focus 模式删除会话数据、无法搜索长会话

**终端状态区（Terminal Status Area）**:
紧贴真实输入缓冲区下边框持续显示的无背景三行会话摘要；它随输入高度移动并定时刷新，fullscreen 中由布局器固定整个“输入框加状态区”栈，而状态区不能脱离输入框独立贴底。首行按存在性显示模型、项目、Git、会话名、会话时长和自定义短语，次行显示上下文，末行显示权限与可用的 agent 提示；所有值均来自当前真实状态。
_Avoid_: 硬编码截图文字、伪造上下文或权限状态、与输入框分离的底部 toolbar、填充背景色、单行通用 toolbar

**会话任务区（Session Task Area）**:
由 Ctrl+T 在输入框上方展开的动态区域，最多投影 5 条真实 pending、running、completed、failed 或 stopped 任务及耗时；`/tasks` 管理当前会话所有后台命令和子代理，`/agents` 或 `← for agents` 打开带稳定区分色的 Running/Library 面板。Ctrl+B 将当前前台 Bash 或子代理转为带 ID 和日志的后台任务，Ctrl+X Ctrl+K 双击确认终止全部后台子代理；后台代理只能使用已授予权限，需询问的调用自动拒绝并报告。
_Avoid_: 没有任务仍显示占位、伪造进度、后台权限弹窗阻塞主输入、任务完成后丢失结果、单次快捷键误杀全部代理

**证据化完成（Evidence-Backed Completion）**:
任务只有在验收条件绑定到特定代码快照上的真实测试、构建、检查或 diff 证据后才能标记成功，证据记录命令版本、工作目录和退出状态；未运行、失败或环境不可验证时只能是 `partial` 或 `blocked`。_Avoid_: 模型自行宣称完成、使用修改前测试结果、隐藏未运行验证、把工具启动成功当作验收通过

**验收条件完整性（Acceptance-Criteria Integrity）**:
既定验收条件不能由执行 Agent 静默删除、弱化或改写，不可行时只能提出带理由的变更，由用户或获得授权的协调代理确认并记录事件；自动新增检查不能降低原标准。_Avoid_: 为通过验证修改标准、把实现困难当作条件无效、无审计地替换验收条件、协调代理越权批准

**隔离代理工作区（Isolated Agent Workspace）**:
拥有写权限的并行 Subagent 默认在独立 Git worktree 中修改代码，由协调代理审查并合并；只读 Subagent 可以共享当前工作区，非 Git 项目中的写任务默认串行执行。_Avoid_: 多个写代理直接覆盖同一文件、把 worktree 当作安全 sandbox、自动合并未验证修改、在非 Git 目录伪造隔离保证

**最小权限代理委派（Least-Privilege Agent Delegation）**:
创建 Subagent 时显式下放父代理权限子集中的工具、目录、预算、模型和递归派生能力，所有事件记录 agent ID 与父子关系；子代理结论和修改经协调代理验证后才能合并。_Avoid_: 子代理继承全部会话权限、无限递归派生、共享未分配预算、无法归因的工具调用、自动合并未验证输出

**可恢复本地代理任务（Durable Local Agent Task）**:
在本机后台运行并持久化身份、状态、日志、权限范围和结果的命令或 Agent Team 工作项，TUI 断开后可重连、查询或显式终止；它属于本地 coding-agent 能力，不依赖云端远程控制服务。_Avoid_: 仅保存在当前进程内、终端关闭后伪报仍在运行、后台弹窗阻塞主会话、把远程托管平台纳入当前边界

**后台权限阻塞（Background Permission Block）**:
后台任务需要未授予权限时持久化为 `blocked_on_permission`，保存 worktree、日志、请求范围和未完成状态并停止消费模型预算；用户可在前台批准、拒绝、附加反馈或终止后恢复任务。_Avoid_: 后台自动提权、权限弹窗阻塞主会话、自动拒绝导致任务无谓失败、等待期间继续调用模型、重启后丢失请求

**可恢复记录（Resumable Transcript）**:
恢复项目会话所需的已提交用户消息、最终回答、工具调用和工具结果；保存前脱敏，且不包含原始 reasoning 或未提交草稿。
_Avoid_: 终端屏幕录制、完整调试日志、输入草稿

**不可变会话事件（Immutable Session Event）**:
按稳定序号追加的用户消息、模型输出、权限变化、工具 attempt 和结果事实，是 transcript、恢复、rewind 与上下文构建的唯一事实源；已提交事件不因压缩而改写或删除。_Avoid_: 将可变 `messages` 列表作为唯一真相、保存压缩后的投影覆盖原始历史、用新 attempt 掩盖结果未知的调用

**模型上下文投影（Model Context Projection）**:
每次模型调用前，由系统与项目指令、最新有效摘要、不可丢失状态、摘要后的事件和近期完整消息临时编译出的可重建视图；Snip 和 Prune 只作用于该投影。_Avoid_: 把投影视为 transcript、将投影裁剪写回事件日志、让不同前端分别维护上下文

**上下文保留优先级（Context Retention Priority）**:
预算依次保障系统与安全规则及当前指令、未完成任务和验收/权限/未知副作用/文件事实、近期对话与必要工具 schema及输出预留、相关项目记忆、已完成历史摘要和旧工具引用；超限时从最低级开始裁剪。_Avoid_: 用旧历史挤掉当前约束、记忆无限注入、不给模型输出留预算、按消息位置随机删除高权限状态

**版本化压缩摘要（Versioned Compaction Summary）**:
独立于对话消息的摘要 artifact，记录 schema、父摘要、覆盖事件范围、来源哈希、引用和 prompt/model 版本；确定性 reducer 保存约束、任务、权限、文件修改、未知调用与未解决错误，LLM 只补充语义叙述。_Avoid_: 把摘要伪装成 assistant 消息、无来源覆盖旧摘要、摘要失败后删除事件、只靠 LLM 决定不可丢失状态

**本地审计数据（Local Audit Data）**:
会话、记忆、trajectory、benchmark 及源代码相关记录默认仅保存在本机，不启用遥测或自动上传；远程提交必须由用户显式开启、先脱敏并在发送前展示数据范围。_Avoid_: 默认遥测、静默上传评测样本、把模型 API 请求误称为遥测、无法审查的批量提交

**项目隔离记忆（Project-Isolated Memory）**:
项目事实、代码、路径和决策默认只在所属项目中召回；全局记忆仅保存用户明确确认的通用偏好，任何跨项目共享都需显式启用。_Avoid_: 按相似度跨仓库泄漏、把项目代码写入全局 persona、未经确认推广局部约定、无法追溯记忆来源

**建议性自动记忆（Advisory Automatic Memory）**:
自动提取记忆携带来源、置信度和有效期，只能作为低权限建议上下文，冲突时降权并提示；只有用户确认后才能提升为持久项目规则，且任何记忆都不能覆盖当前指令或安全策略。_Avoid_: 让模型自写高权限规则、把外部注入持久化、冲突时静默选择旧记忆、无来源或无法删除的记忆

**级联遗忘（Cascading Forget）**:
“忘记”操作删除目标记忆及其 embedding、合并派生项、检索索引和缓存，并保存 tombstone 防止从同一来源重新提取；原始会话事件仅由独立的会话或项目数据删除操作清除。_Avoid_: 只删展示记录、删除后仍可向量召回、下一次维护重新生成、把忘记偏好等同于销毁审计历史

**记忆冲突序（Memory Conflict Order）**:
冲突按当前明确用户指令、已确认项目规则、自动提取记忆的权限顺序处理，旧事实标记 `superseded` 并保留来源；同权限且无法判定时不注入为事实并提示用户。_Avoid_: 最新记录无条件获胜、静默覆盖已确认规则、同时注入互斥事实、丢失冲突历史

**终端会话生命周期（Terminal Session Lifecycle）**:
从当前 shell 启动、持久化、清空、恢复、分支、重命名到退出的会话控制。Ctrl+D 或 `/exit` 在无草稿和活动任务时保存退出，否则先确认；`/clear` 保存旧会话并开始新对话，`/resume`、`--continue` 与 `--resume` 恢复，`/branch` 创建新会话 ID 且不改写来源，`/rename` 同步标题。退出 fullscreen 必须恢复调用前的终端缓冲区、光标、鼠标模式和标题，只有真实落盘成功才报告保存成功。
_Avoid_: 静默丢弃草稿或任务、恢复时生成新身份、分支改写原记录、保存失败仍退出并声称成功、破坏调用者终端状态

**会话检查点（Session Checkpoint）**:
每次用户消息提交前持久化的会话位置，以及由当前前台回合内置 Edit/Write 工具触及文件的修改前快照；每个会话保留最近 100 个。空输入时双 Esc 或 `/rewind` 按用户消息打开恢复菜单，可分别恢复代码、对话或两者，也可对选定区间做定向总结；恢复对话会把原提示送回输入编辑器。Bash、外部程序、并发会话、后台子代理和链接文件的改动不在可恢复承诺内，必须在执行恢复时明确报告跳过项。
_Avoid_: 把 checkpoint 当 Git、声称能撤销未跟踪副作用、只保存在进程内、恢复时静默覆盖无法验证的并发修改

**项目附件（Project Attachment）**:
用户通过 `@路径`、拖入路径或受支持的剪贴板操作明确加入本轮上下文的文件快照、目录索引或图片；受脱敏、类型、路径和上下文预算限制，图片作为可恢复的会话资产保存。
_Avoid_: 仅供显示的路径文字、无界递归目录内容

**终端输入编辑器（Terminal Prompt Editor）**:
承载真实草稿、附件和补全状态的多行编辑器：Enter 提交，Shift+Enter、Ctrl+J 或反斜杠加 Enter 换行；`/` 补全命令与 skill，`@` 补全项目文件，行首 `!` 进入受权限控制且输出进入会话的 shell 模式。大段粘贴折叠为保留完整内容的 Pasted text 芯片，剪贴板或拖入的图片/文件形成可定位、可删除、可排队和可恢复的附件芯片，并真实传入具备相应能力的模型。
_Avoid_: 只能显示不能输入的框、丢失芯片内容、附件只作为路径文字、补全抢键或重复提交、shell 模式绕过安全策略

**提示历史与建议（Prompt History and Suggestions）**:
按工作目录保存的已提交输入历史及独立草稿 stash；多行编辑器中的 Up/Down 先移动光标，到边界后才遍历历史，Ctrl+R 可在会话、项目和全部项目范围反向搜索，Ctrl+S 暂存或恢复带附件草稿，Ctrl+G 或 Ctrl+X Ctrl+E 往返外部编辑器。空闲输入框可异步生成与真实 Git/对话相关的灰色建议，由 Tab 或 Right 接受、输入任意文字即取消；建议可关闭，在无可复用缓存、Print 或 Plan 模式跳过，且不阻塞输入。
_Avoid_: 历史跨项目泄漏、建议冒充已输入内容、后台建议抢焦点、stash 丢失附件、搜索 Enter 后重复执行

**运行参数选择器（Runtime Setting Picker）**:
在不破坏当前草稿、附件和光标的情况下选择真实可用模型、effort、extended thinking、fast mode 与权限模式的菜单；Alt+P、Alt+T、Alt+O、Shift+Tab 及对应 slash command 立即作用于后续请求而不进入消息队列。选择器只展示 provider/模型支持的值，方向键或数字选择、Enter 确认、Esc 无副作用取消；会话级值随会话恢复，仅在用户明确要求时写为默认配置。
_Avoid_: 展示不可用能力、切换后改写旧记录、菜单提交普通草稿、把会话选择静默写成全局默认
