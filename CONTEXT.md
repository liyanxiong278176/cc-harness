# cc-harness

cc-harness 是在用户当前终端中运行的编码代理。其交互语言强调由应用接管当前终端表面的会话，而不是独立窗口或 Web 图形界面。

## Language

**上下文-记忆工程评测（Context-Memory Engineering Evaluation）**:
将上下文压缩、信息卸载、卸载后检索、跨轮次利用、冲突更新与持久恢复视为同一信息生命周期的统一评测能力域。该能力域统一运行并统一报告各 benchmark 与机制门禁，但保留每个 benchmark 的独立成绩，不计算跨 benchmark 加权总分。_Avoid_: 将上下文和记忆拆成互不关联的总分、用人为权重掩盖单项退化、仅凭最终答案宣称内部机制生效

**RULER 评测边界（RULER Evaluation Boundary）**:
独立 NVIDIA RULER 长上下文评测不属于上下文-记忆工程评测，其入口、适配器、数据缓存与历史评测证据均不保留；MemoryAgentBench 官方套件内的 `ruler_qa1` 与 `ruler_qa2` 予以保留，因为它们在该套件中评估增量记忆写入与检索，并用于维持官方完整结果的可比性。_Avoid_: 将 MemoryAgentBench 的 RULER 派生任务误报为独立 RULER 成绩、删除官方子集后仍宣称完整 MemoryAgentBench 结果、将已退役的独立 RULER 结果纳入对外结论

**MemoryAgentBench 评测档位（MemoryAgentBench Evaluation Profile）**:
`portfolio` 按官方任务类型分层抽样，仅用于开发调试与快速回归；`full` 覆盖全部官方任务类型和样本，是形成对外可比成绩的唯一档位。_Avoid_: 将 portfolio 子集包装为官方完整成绩、从 full 档位中排除困难任务或 RULER 派生任务

**上下文-记忆评测档位（Context-Memory Evaluation Profile）**:
`portfolio` 是冻结的分层开发集，包含 LongMemEval-S Cleaned 100 问、LongMemEval-V2 Small 50 问、LoCoMo 4 段对话中的 200 问，以及 MemoryAgentBench 24 条记忆流且每流最多 10 问；`full` 包含对应官方范围内的 500 问、451 问、10 段对话共 1,986 问，以及 146 条记忆流的全部问题。只有未裁剪的 full 档位可形成对外 benchmark 成绩。_Avoid_: 运行时重新抽样、将 portfolio 与 full 混合比较、隐藏 full 档位中失败或无效的样本

**上下文-记忆配对消融（Paired Context-Memory Ablation）**:
每个冻结任务在相同模型、参数、输入顺序与初始状态下各执行一次 control 和 treatment；control 关闭长期记忆、压缩、卸载与卸载检索，并在超出窗口时采用固定的最近内容截断，treatment 启用生产上下文-记忆链路。正式报告并列给出 treatment 的 benchmark 成绩和相对 control 的配对变化，不以变化值替代 benchmark 成绩。_Avoid_: 将 control 超窗记为基础设施无效、双方使用不同输入顺序、只展示有利的消融结果、把同一组的重复运行误称为配对 A/B

**原始语义事件重放（Native-Semantics Event Replay）**:
评测历史按照其原始交互语义进入统一信息生命周期：LongMemEval-S Cleaned 与 LoCoMo 重放 user/assistant 会话，LongMemEval-V2 重放 agent action 与 tool result，MemoryAgentBench 按官方增量 chunk 重放对话、文档和外部记录；所有事件共享同一不可变记录、上下文投影、版本化摘要、卸载与检索底层。gold answer、证据标记和评分元数据不得进入模型可见内容；固定机制 canary 只验证机制触发，不计入 benchmark 分数。_Avoid_: 将所有历史拼成单个用户提示、为了触发卸载把普通对话伪装为工具结果、将 canary 成绩混入官方 benchmark 成绩

**上下文-记忆机制门禁（Context-Memory Mechanism Gate）**:
独立于答案分数验证原始记录完整、版本化摘要真实减载、pointer/node/ref 一致且原文可还原、生产检索实际读取官方证据、模型输入无评分信息泄漏、control 机制确实关闭，以及任务与分组隔离。任一必要门禁失败时保留原始 benchmark 分数，但本次上下文-记忆工程有效性结论无效。_Avoid_: 用答案正确率推断内部机制生效、用部分门禁通过抵消数据损坏、删除门禁失败的运行记录

**上下文-记忆恢复门禁（Context-Memory Recovery Gate）**:
固定 recovery canary 分别在原始事件提交后、ref 对象写入后、版本化摘要写入中和 checkpoint 后注入中断；恢复必须从最后完整提交继续，不重复或遗漏事件，并使用正确的摘要与节点版本。对原始记录、摘要、节点清单或 ref 对象的篡改必须被摘要校验发现并使运行无效，不得静默信任或改写损坏证据。_Avoid_: 为每道官方题重复注入故障、把损坏数据当作普通答题失败、恢复时重新执行已完成样本

**DeepSeek 适配评分（DeepSeek-Adapted Scoring）**:
control、treatment 与必要的隔离 judge 均使用经服务端身份验证的 `deepseek-v4-flash`；官方确定性指标保持不变，替代官方 reader 或语义 judge 的分数必须明确标为 DeepSeek adaptation，并保留全部原始预测供未来重新评分。使用官方数据集不等同于获得官方 leaderboard 成绩，模型或 judge 契约不一致时不得直接横向比较公开数字。_Avoid_: 隐藏 judge 替换、让 judge 继承被测记忆、把适配成绩标成官方成绩、为重新评分重复运行被测系统

**可续跑上下文-记忆运行（Resumable Context-Memory Run）**:
四个 benchmark 在统一结果根下拥有独立运行状态和 control/treatment 证据，并由总报告聚合；同一命令仅跳过完整性校验通过的已完成样本，从首个未完成的 attempt-1 继续。数据集、模型、配置、代码或任务目录摘要变化时拒绝混合续跑，四项成绩与机制门禁分别报告且不生成加权总分。_Avoid_: 仅凭目录存在跳过样本、把不同输入契约写入同一运行、Ctrl+C 后从头执行、用聚合分隐藏单项失败

**上下文-记忆数据占用契约（Context-Memory Data Footprint Contract）**:
仅准备 LongMemEval-V2 Small 所需文本轨迹与多模态资源，不下载 Medium；输入按内容摘要去重并固定 revision、大小与 SHA-256，下载支持断点续传，数据和证据接近 50 GB 软上限时安全停止。完整 Small 运行必须先验证模型图像能力；不支持时 full 标记为 unsupported，另行运行的纯文本结果只能称为 text-only adaptation。_Avoid_: 每题复制完整轨迹库、静默删除运行证据、忽略图片后宣称完整 Small 成绩、未固定数据版本即继续旧运行

**封存式评测隔离（Sealed Evaluation Isolation）**:
不同 benchmark、样本及 control/treatment 分组使用互不共享的 workspace、运行目录、会话、记忆库、摘要、ref 与节点清单；每组完成后将其状态封存为只读审计证据并从后续活动运行中卸载，下一组启动前必须验证活动状态为空且不存在跨任务命中。隔离或清理失败使运行无效，评测不得读取或改写用户日常全局记忆。_Avoid_: 复用上一题的 CC_HARNESS_HOME、让 treatment 继承 control 状态、为防污染删除审计证据、清理失败后继续运行

**产品对标边界（Product Parity Boundary）**:
cc-harness 对标 Claude Code 的终端 coding-agent 内核及 headless/SDK 能力；Web、Desktop、Mobile、Slack、Chrome 与完整企业平台不属于当前对标范围。_Avoid_: 完整复制 Claude 平台、只对齐终端外观

**可信内核阶段（Trusted Core Phase）**:
第一阶段优先完成原生文件与搜索工具、不可绕过的 hard-deny 与 sandbox、模型和成本追踪、headless JSON 协议及持续 coding benchmark，再建设 Skills、Hooks 与增强型 Subagents；UI 仅修复阻塞真实使用或验证的问题。_Avoid_: 用像素级界面对齐代替底层能力、在核心契约未成立前继续扩大 UI 表面积

**对标发布门（Parity Release Gate）**:
使用固定版本的 Claude Code，在相同代码任务、模型条件、环境与资源预算下持续运行可复现对照评测；同模型榜与等预算榜均以 `deepseek-v4-flash` 为基准，记录服务端返回的具体模型版本，Claude Code 通过 DeepSeek 的 Anthropic-compatible endpoint 运行。成功率至少高 5 个百分点，或成功率差距不超过 3 个百分点且成本或耗时至少降低 20%，才可称为“超过”；安全越界、数据丢失与会话恢复回退不能被综合分抵消，结论必须经过重复运行和置信区间检验。_Avoid_: 按功能数量宣称领先、只展示有利样例、混合模型版本、用效率抵消关键安全或恢复缺陷、改变环境后直接比较

**隔离对标运行（Isolated Parity Run）**:
常规 coding benchmark 从冻结仓库快照、全新 worktree、固定依赖与空白会话开始，关闭跨运行长期记忆并记录缓存状态；只有专门的记忆评测允许继承历史。_Avoid_: 复用上次修改、让长期记忆污染常规任务、双方使用不同依赖或初始状态、隐藏 warm-cache 优势

**冻结对标保留集（Frozen Parity Holdout）**:
不参与生产策略调参的冻结真实仓库任务集，与公开开发集和内部回归集分离；对标产品保留各自真实 harness，但共享模型端点、代码快照、依赖、环境和资源预算。结论以重复运行的机器验收和硬安全门禁为主，并保留失败样例与 trajectory。_Avoid_: 根据保留集单题调生产默认值、只跑一次、用 LLM judge 替代测试、隐藏失败轨迹、让效率分抵消越界或数据损坏、混合不同模型和产品版本

**升级兼容边界（Upgrade Compatibility Boundary）**:
`0.1.x` 可信内核改造可以调整未公开的内部 API 与配置结构，但必须迁移或兼容已有会话、记忆、项目配置和已文档化 CLI 行为；无法无损迁移时必须在执行前明确报告并提供恢复路径。_Avoid_: 为保留错误内部设计阻止必要重构、静默丢弃用户数据、无提示改变公开命令语义

**可验证遗留导入（Verifiable Legacy Import）**:
旧会话、引用和配置迁入新事实模型时只保留可由原数据证明的内容与关联，缺失的权限、attempt 或副作用语义明确标记为不可验证；导入可重入、可中断恢复并生成完整报告，旧数据备份与新写入相互隔离。_Avoid_: 为填满新 schema 编造历史、原地破坏旧库、重复导入事件、损坏引用后静默继续、允许旧程序覆盖升级后的事实

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

**可续读工具结果（Resumable Tool Result）**:
原生读取与搜索工具返回带稳定路径、来源范围、完整性状态和继续位置的结构化结果；大小、时间或数量预算导致的截断必须显式可见并可从游标继续，面向模型的文本只是该结果的投影。读取、枚举和匹配过程中的每个路径仍受相同授权根目录与符号链接边界约束。_Avoid_: 把拼接文本当作唯一结果、静默截断、用不稳定排序分页、把未完成结果呈现为完整、让搜索或续读绕过路径权限

**供应商中立模型内核（Provider-Neutral Model Core）**:
会话、工具、权限、记忆、任务与评测运行在不绑定单一模型厂商的共享 agent 内核上，用户可提供自己的供应商凭据；每个 provider/model 通过能力档案声明真实上下文窗口、工具调用、视觉、推理、缓存、结构化输出和价格能力，运行时与界面仅启用其实际支持项。_Avoid_: 将核心绑定 Anthropic、只允许修改 OpenAI-compatible base URL 却忽略模型差异、用最低共同能力限制所有模型、展示虚假的上下文或成本

**模型能力档案（Model Capability Profile）**:
特定 provider、endpoint 与模型版本实际支持的上下文、输出、工具、流式、视觉、推理、缓存、结构化响应、token 计量和价格能力声明；请求构建、上下文预算、界面和评测只使用已验证能力，实际返回模型身份随调用记录。_Avoid_: 用配置期望冒充服务端事实、把兼容 API 当作完全相同语义、对不支持能力做虚假展示、把估算 usage 写成权威账单、把单个模型特性硬编码为全局默认

**会话资源预算（Session Resource Budget）**:
用户为会话或后台任务设置 token、费用、总耗时和工具调用次数上限，接近上限时预警，达到上限后停止启动新模型或工具调用并保存可恢复状态；已启动副作用调用按真实结果或 `outcome_unknown` 收尾。_Avoid_: 超额后继续调用、用估算成本冒充供应商账单、预算停止时丢失任务状态、把被中断调用标记为未执行

**预算驱动代理循环（Budget-Driven Agent Loop）**:
代理在仍有可验证进展且未触及会话资源预算时继续执行；用户取消、预算耗尽、重复调用、连续空回复或无进展修改会触发可恢复停止，较高且可配置的步骤上限只作为最终保险。生产循环与 benchmark profile 分离，Subagent 使用独立且更小的预算。_Avoid_: 用为单项评测调优的固定低轮数限制生产任务、无限重试无进展步骤、让子代理无界消耗主会话预算、停止时丢失当前状态

**事务化文件变更引擎（Transactional Mutation Engine）**:
所有原生文件写入先编译为统一变更计划并完整校验，再以原子替换方式提交；第一阶段由 `Edit` 和 `Write` 接入，`Read` 返回编码、换行风格与内容哈希，`Edit` 使用精确 `old_text -> new_text` 和 `expected_hash`，零匹配、多匹配或哈希过期均拒绝，`Write` 区分 `create_only` 与 `replace_existing` 且替换必须携带哈希。引擎统一生成 diff、checkpoint、审计和回滚数据，后续 `ApplyPatch` 仅负责把补丁解析为同一种变更计划。_Avoid_: 绕过统一引擎直接写盘、基于过期内容静默覆盖、模糊匹配后修改错误位置、部分校验通过即产生部分写入、为不同编辑工具维护冲突的安全与回滚语义

**显式模型回退（Explicit Model Fallback）**:
跨模型自动切换只在用户预先配置 fallback 链、费用上限与允许的能力降级后发生，并在事件流、界面和评测中记录实际 provider/model；同模型暂态错误可按受控策略重试。_Avoid_: 静默换模型、恢复会话后偷偷改变模型、fallback 超出预算、把混合模型结果记到单模型榜

**会话 Harness 档案（Session Harness Profile）**:
会话固定系统提示词、工具 schema、压缩 prompt、模型能力、reasoning effort 与安全策略的版本 fingerprint；恢复时沿用原档案，升级只能显式迁移，评测结果必须保存同一 fingerprint。_Avoid_: 恢复后静默切换提示词或 effort、只记录模型名、无法定位 harness 回退、用不同档案结果直接比较

**项目会话（Project Session）**:
归属于一个项目目录的独立对话上下文；普通启动创建新会话，继续或恢复必须由用户通过对应命令显式选择。
_Avoid_: 跨项目共享当前对话、普通启动时隐式续接

**项目事实存储（Project Fact Store）**:
位于用户数据目录并按项目身份隔离的本地持久存储，以追加式 SQLite 事件库保存事实和引用，以内容寻址对象库保存大型日志、附件、快照和卸载资产；项目工作区只承载用户主动维护的可版本控制配置。副作用前置事实确认持久化后才允许执行。_Avoid_: 启动即污染仓库、从项目内符号链接选择状态目录、删除并重写整段事件、把大型内容重复塞入消息表、只复制活动 SQLite 主文件作为备份、项目移动时自动合并相似身份

**Headless 会话协议（Headless Session Protocol）**:
TUI、SDK、benchmark、IDE 与一次性命令共同使用的版本化双向控制协议，以持久化事件序号支持流式观察和断线续读，并以可关联、可幂等的命令处理提交、排队、取消及交互响应。前台附着运行与可持久后台任务具有明确不同的断线生命周期。_Avoid_: 从终端文本猜测状态、各客户端复制 Agent 逻辑、让慢客户端阻塞代理、断线后静默丢失或继续前台任务、把诊断日志混入机器协议

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

**作用域授权凭证（Scoped Permission Grant）**:
用户批准产生绑定主体、工具能力、规范化目标、会话或项目范围及有效期的可解释授权；一次性授权不能被子代理或后续调用提升，持久授权存放在项目不可自行修改的用户侧策略中，并可查看与撤销。_Avoid_: 记住模糊的全部允许、让复杂动态 shell 获得永久授权、仓库文件自授权限、把凭据授权变成通用环境访问、撤销后继续启动尚未执行的调用

**统一能力授权管线（Unified Capability Authorization Pipeline）**:
原生工具、MCP、Subagent 与后台任务都以声明的副作用能力和规范化目标进入同一授权流程，依次受硬安全边界、执行环境可强制性、最小范围授权及用户确认约束，并在执行前复检实际目标。批准只产生绑定工具、能力、目标、会话和有效期的作用域授权；显式宿主执行仍经过同一管线。_Avoid_: 各工具自行解释权限、用模糊命令永久放行、批准覆盖 hard deny、sandbox 失效后静默降级、授权后不复检变化的路径或目标

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

**可验证隔离能力档案（Verifiable Isolation Capability Profile）**:
执行后端只有在目标平台实际强制文件、进程、资源、网络、凭据、取消和清理边界并通过统一 conformance suite 后才能标记为 `isolated`；后端名称或配置意图不构成安全等级。事件与评测记录实际后端、runtime 身份和通过的能力，缺少必需能力时 fail-closed。_Avoid_: 把安装了 sandbox SDK 等同于已隔离、保留字段未接通仍宣称受控、容器环境冒充宿主环境、不同安全等级的 benchmark 混合比较

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

**流式响应提交边界（Streaming Response Commit Boundary）**:
模型文本与工具参数流在完整组装、正常结束并通过契约校验前只属于临时输出；中断内容明确保存为 `interrupted`，残缺工具调用不得执行。多调用批次按依赖与副作用调度，展示可按真实完成时间流式进行，但结果始终以稳定 tool-call ID 关联。_Avoid_: 执行半截 JSON、把断线半句当作最终回答、并行结果串位、取消后启动新调用、模型重试重复已完成副作用

**用户配置（User Configuration）**:
适用于所有项目的 cc-harness 凭据、默认模型和 MCP 设置，位于用户主目录的 `.cc-harness/` 中。
_Avoid_: 项目配置、会话数据

**项目配置（Project Configuration）**:
只影响当前项目目录的模型覆盖、MCP 设置、策略、任务和会话数据；同名设置按约定覆盖用户配置。
_Avoid_: 用户配置、跨项目默认值

**项目指令（Project Instructions）**:
由工作目录根部及目标文件祖先目录中的 `CC-HARNESS.md` 提供的分层项目约定；根规则适用于全项目，子目录规则只适用于该目录后代，均由用户维护并以来源、作用域和内容哈希进入 Harness Profile。当前用户明确指令优先，项目指令不能授予权限或覆盖系统安全。_Avoid_: 只创建但不加载、把局部规则提升到全项目、自动执行 CLAUDE.md 或 Hook、会话中静默切换规则版本、自动生成代码库目录清单冒充约定

**Claude 配置兼容导入（Claude Configuration Compatibility Import）**:
用户显式请求时，将 `CLAUDE.md`、`.claude/skills/`、Hooks 和 Subagent 定义映射到 cc-harness 的原生指令与扩展契约；`CC-HARNESS.md` 及 cc-harness 命名空间保持权威，导入内容接受相同的权限与安全检查，Hook 不会因发现文件而静默执行。_Avoid_: 自动执行外部 Hook、把 Claude 私有格式作为唯一存储格式、导入时绕过权限、静默覆盖 cc-harness 原生配置

**本地扩展包（Local Extension Package）**:
从项目目录、本地路径或 Git 来源安装的统一 Skills、Hooks 与 Plugins 单元，声明版本、能力和权限并接受锁定与完整性校验；官方托管 Marketplace 不属于扩展协议稳定前的产品范围。_Avoid_: 未声明权限的可执行 Hook、无版本浮动安装、把插件商店当作扩展运行时、发现文件即自动执行

**分层扩展信任（Layered Extension Trust）**:
Skill 只提供带来源的工作流知识，Hook 在受控生命周期点请求规定动作，Plugin 以版本化 manifest 组合 Skill、Hook、工具与外部服务配置；安装、启用和能力授权彼此分离。可执行第三方扩展与 Agent 内核隔离，仍经过统一授权、资源预算和分层故障策略。_Avoid_: 把提示词加载等同于代码执行授权、项目文件自行启用扩展、第三方代码直接访问内核密钥或事件库、插件静默覆盖核心工具、升级时隐式扩大权限

**不可信 MCP 能力源（Untrusted MCP Capability Source）**:
MCP server 提供的工具、资源、提示与能力声明属于第三方输入，启用、连接、命名、参数、副作用、网络、凭据和输出均由可信内核重新验证；项目配置不能因被发现而自动启动服务器，未知副作用默认按可变更操作处理。_Avoid_: 相信 server 自报只读而免授权、MCP 工具覆盖核心工具、断线后盲目重放远端写入、把返回文本提升为系统指令或持久规则、把 OAuth token 写入模型或日志

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

**受管命令执行单元（Managed Command Execution Unit）**:
一次 shell 或结构化进程调用由明确方言、工作目录、受控环境、执行后端、交互模式和资源边界定义；标准输出与错误按序流式记录，大输出可恢复卸载，取消和超时覆盖整个进程树。转入后台后成为持久任务，无法确认完全终止时结果保持 `outcome_unknown`。_Avoid_: PowerShell 命令交给 CMD、继承全部宿主凭据、只终止外层 shell、截断日志却呈现为完整、把前台进程丢到后台后失去身份与控制

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

**工程质量棘轮（Engineering Quality Ratchet）**:
跨平台正确性、安全、兼容性和恢复能力由持续运行的分层门禁约束；既有静态检查债务可建立显式基线，但任何变更不得新增债务，触及代码需满足当前完整标准。benchmark 只衡量代理表现，不能替代契约、故障注入和真实端到端测试。_Avoid_: 关闭 CI 但宣称受保护、用总覆盖率掩盖关键分支空白、为通过测试删除验收条件、把单次 benchmark 当作 parity 证据、让遗留基线无限增长

**评估发布门禁（Evaluation Release Gate）**:
评估体系的首要产品职责；依据可复现的底线条件和已建立能力判断候选版本是否允许发布，安全、数据完整性和核心任务正确性等否决项不得被综合分数抵消。_Avoid_: 把诊断报告当作发布许可、用平均分掩盖关键失败、在缺少可比基线或可信证据时宣称通过

**诊断评估套件（Diagnostic Evaluation Suite）**:
评估体系的第二层职责；在发布门禁失败或研发探索时定位能力边界、失败模式和可能的改进方向，形成待验证假设，修复并稳定通过的关键场景可晋升为回归门禁。_Avoid_: 用探索性指标直接阻断发布、把相关性当作根因、发现问题后不沉淀回归用例、让诊断分数覆盖发布否决项

**冻结内部回归集（Frozen Internal Regression Suite）**:
由项目维护的固定任务、初始状态和验收条件，持续保护已经获得的产品能力与用户底线，并作为高频发布硬门禁；任务只能通过受审查的版本迁移更新，不能为候选实现临时放宽。_Avoid_: 根据当前实现改答案、混入尚无稳定基线的探索任务、复用被前次运行修改的环境、让公开榜单波动直接阻断日常变更

**公开能力基准（Public Capability Benchmark）**:
由外部维护或公开发布、用于衡量能力边界和横向比较的统一任务集；它提供对标与发布资格证据，但不直接充当每次变更的高频硬门禁。公开基准中的重要失败经清洗、固定环境并建立稳定预期后可晋升为内部回归任务。_Avoid_: 把公开榜单当作唯一产品质量标准、忽略污染和环境漂移、只选有利子集却宣称完整结果、未经固定就复制为回归门禁

**评估证据层级（Evaluation Evidence Hierarchy）**:
Coding-agent 任务优先依据环境最终状态和确定性测试判定，其次应用安全、权限与数据完整性否决项，再参考静态分析和工具轨迹；LLM judge 只补充难以客观断言的质量维度，不能把确定性失败或否决事件改判为成功。_Avoid_: 相信代理自述而不检查状态、让主观高分覆盖测试失败、用输出文本代替真实副作用验证、让未校准 judge 单独决定发布

**评估模型角色边界（Evaluation Model Role Boundary）**:
所有参与横向比较的被测 Agent 使用相同的 `deepseek-v4-flash`、服务端模型版本和资源条件；judge 属于统一测量工具，可以使用独立模型，但必须固定版本、对所有被测 Agent 应用相同规则并经过人工 gold set 校准。_Avoid_: 给不同 Agent 使用不同能力模型、让某个参赛 Agent 使用专属 judge、把同模型自评称为独立多源评判、未记录 judge 版本或校准状态

**评估否决项（Evaluation Veto）**:
后果严重且可明确验证的发布失败，包括无效评估、安全越界、数据完整性破坏、核心正确性显著回归、恢复契约失败和一等平台核心契约失败；任一否决项成立即阻止发布，不能由能力、成本、速度或体验分数抵消。_Avoid_: 对高后果失败取平均、把环境故障记为通过、用效率收益接受数据丢失、未经审计临时豁免

**成熟度评估指标（Maturing Evaluation Metric）**:
重要但尚未建立稳定分布、可靠样本量和决策阈值的能力、效率或体验指标；先持续记录趋势与不确定性，建立基线并验证误报率后，才可按受审查的阈值晋升为发布门禁。_Avoid_: 第一次测量就设硬阈值、长期观测却永不形成决策、忽略样本量和置信区间、为当前候选版本临时调整晋升标准

**可销毁评估环境（Disposable Evaluation Environment）**:
每个 Agent trial 在由冻结输入创建的独立临时考场中运行，代码状态由全新 worktree 或等价快照隔离，文件、进程、网络、凭据和资源边界由容器、虚拟机或受限系统账户强制；结果采集后整体销毁。只有原生平台契约检查可使用对应系统的临时 runner，攻击任务不得直接运行在当前开发工作区。_Avoid_: 把 worktree 当作安全 sandbox、依靠逐文件回写恢复副作用、挂载开发者真实凭据、让不同 trial 共享可变环境、在日常工作区执行逃逸测试

**不可变评估运行清单（Immutable Eval Run Manifest）**:
一次正式评估的身份与实验条件记录，绑定被测产品和 commit、实际模型与 judge 版本、任务和验收版本、初始仓库、依赖与环境摘要、资源预算、trial 身份、缓存/记忆/网络状态及原始证据引用；完成后只能追加更正事件，不能覆盖。缺少关键字段的运行无资格参与发布门禁或对标结论，报告与排行榜均由清单和原始 trial 重建。_Avoid_: 只保存最终分数、运行后修改实验条件、混合不同版本却共用 run ID、让不可重建报告成为唯一证据、用请求模型名代替服务端实际版本

**评估结果状态（Evaluation Result Status）**:
有效 trial 只有 `pass` 或 `fail`；环境、runner、judge、数据或关键证据无法支持判定时记为 `invalid`，不进入能力分母但阻止发布判断，重试产生保留原记录的新 attempt；有效样本仍不足以支持发布或对标结论时，聚合结果记为 `inconclusive`。_Avoid_: 把基础设施故障算作 Agent 失败或成功、静默删除无效样本、重试覆盖原 trial、在置信证据不足时按点估计宣布胜负

**风险分层自适应配对采样（Risk-Stratified Adaptive Paired Sampling）**:
正式发布与对标评估让各被测 Agent 在相同任务、预算和环境上成组运行并随机轮换顺序，按失败后果设置最低样本量，再持续追加 trial，直到不确定性达到预定精度或预算耗尽；后者仍无法判断时报告 `inconclusive`。发布稳定性以重复成功和失败风险为主，能力探索可另报多次尝试中至少一次成功。_Avoid_: 用单次结果代表稳定能力、所有风险统一固定 `n=5`、双方使用不配对条件、置信区间仍宽时按点估计宣布胜负、只报最佳一次

**冻结保留集生命周期（Frozen Holdout Lifecycle）**:
正式发布与对标所用的未见任务在运行前不向实现者、生产 Agent 或公开仓库暴露，也不参与提示词、工具或策略调优；运行仅临时注入受控 runner。任务内容一旦进入研发分析即从保留集退役、转入内部回归集并由新的未见任务补位，同时保留来源、暴露状态、相似性和污染风险记录。_Avoid_: 用已知题目反复证明泛化、把私有保留集注入日常上下文、暴露后仍计入领先声明、只公开有利结果、以公开 benchmark 代替未见验证

**人工金标评审集（Human Gold Audit Set）**:
由至少两名标注者在不查看 judge 结论的情况下依据版本化 rubric 独立标注，分歧经第三方或领域负责人仲裁并保留原始标签、理由和版本；用于调节 judge 的 calibration 子集与锁定配置后验证泛化的 audit 子集相互分离。judge 未通过独立 audit 的一致性、关键类别召回和误报标准前只能提供诊断，不能参与发布门禁。_Avoid_: 单人直觉充当金标、judge 自标自证、用同一批样本调参并验收、只看总体一致率而忽略关键漏判、修改 rubric 后沿用旧校准结论

**分层评估运行体系（Tiered Evaluation Cadence）**:
正式 eval 共享同一证据格式但按成本和职责分层：L0 本地/普通 CI 验证确定性基础设施，L1 PR 运行关键回归和高风险 canary，L2 Nightly 运行完整内部多 trial 回归与跨平台检查，L3 Weekly 探索公开基准和长任务能力，L4 Release 使用冻结保留集和完整配对统计决定发布与对标声明。低层快速信号不能替代高层资格证据，任一未解决硬门禁失败都会阻止发布。_Avoid_: 因完整套件太慢而关闭全部 CI、用 smoke 测试宣称完整覆盖、每个 PR 串行运行数小时公开基准、Nightly 失败后继续发布、不同层使用不可比较的证据格式

**Harness 能力评估矩阵（Harness Capability Evaluation Matrix）**:
正式报告与门禁按 Coding Outcome、Agent Loop、Context Management、Memory、Tools & Protocols、Safety & Privacy、Reliability & Recovery、Human Interaction、Operational Fitness 九个产品能力域组织；Promptfoo、LoCoMo、公开 benchmark 和自有任务只是提供可追溯证据的来源。报告展示各域结果、不确定性和否决项，不生成能用优势域抵消关键失败的单一总分。_Avoid_: 按测试工具目录代替产品能力、重复计算跨域任务、用加权总分掩盖安全或数据损坏、只测模型答案而忽略 Harness 状态、把缺少证据显示成零分或通过

**真实失败评估闭环（Production-Failure Eval Loop）**:
真实会话中的失败只在本机生成脱敏 `pending` 候选，默认不上传代码、凭据、路径、完整会话或原始 reasoning；候选经用户确认导出范围、最小化、可销毁环境复现和人工确认预期后才能进入开发集，修复并稳定通过后晋升内部回归集。自动提取不能直接生成 gold label、发布门禁或未见保留任务。_Avoid_: 默认遥测用户项目、把环境故障自动固化为产品回归、自动生成标准答案、自报失败未经复现就阻断发布、修复真实问题后不沉淀测试

**评估任务契约（Evaluation Task Contract）**:
正式任务的版本化考试规格，绑定身份与 owner、能力域和风险、冻结初始状态、用户目标、允许工具和权限、资源预算、客观 outcome、禁止修改与否决项、采样策略、证据要求及清理规则。缺少可验证 outcome 或完整初始状态的任务只能用于诊断，不能进入发布硬门禁。_Avoid_: 只有 prompt 没有环境和答案、相信 Agent 自述完成、允许修改测试或 grader 伪造通过、双方资源边界不同、任务变化却沿用旧版本结果

**统一评估证据核心（Unified Eval Evidence Core）**:
项目自有的稳定 Task Contract、Run Manifest、Trial Result、状态、artifact 与 gate 语义是发布事实源；Promptfoo、LoCoMo、Harbor/公开 coding benchmark 和原生平台契约通过 adapter 执行或采集并映射到统一证据。外部框架可独立升级或替换，其私有结果格式不能直接决定发布。_Avoid_: 删除已有专项 eval 重写一切、让单一框架私有 JSON 成为唯一事实、为每种来源复制一套状态和报告逻辑、adapter 静默丢弃原始证据、把工具目录当作能力域

**渐进式评估调度（Progressive Eval Orchestration）**:
评估队列、trial attempt、心跳、超时、取消、重试、断点续跑和 artifact 身份从第一版即持久化并支持多 worker 语义，但先由可靠本地 runner 执行，再用 CI shard 横向并行；只有实测排队时间、成本或隔离资源成为瓶颈后才引入远程 worker 平台。环境与确定性 grader 可按完整指纹缓存，模型 trial 默认不复用，任何复用必须披露来源和有效期。_Avoid_: 单个长循环崩溃后全量重跑、缺少 shard 仍生成通过报告、把旧模型回答冒充新 trial、没有吞吐证据就建设重型集群、并发失控耗尽本机或 API 配额

**TUI 功能评估契约（TUI Functional Evaluation Contract）**:
滚动与自动跟随、输入和草稿保留、权限交互、resize 重排、取消与退出、会话恢复、终端状态清理、长文本/中文/工具输出布局及一等平台 PTY 行为属于可客观验证的核心功能并进入硬门禁；配色、视觉相似度、信息密度和文案感受属于人工校准的体验诊断。字符栅格或截图可验证结构与遮挡，但不能成为唯一正确性依据。_Avoid_: 把无法滚动归为审美问题、只测组件函数不测真实 PTY、像素变化阻断所有开发、截图通过却交互不可用、只在单一终端验证

**评估门禁例外（Evaluation Gate Exception）**:
原始 `fail` 不得改写；只有非关键、影响明确且有临时缓解的失败可由授权负责人批准范围受限、关联 issue、自动到期且不可无限续期的 `approved_with_exception`。无效证据、关键安全越界、用户数据损坏、测试/grader 篡改、未知非幂等副作用重放、公平条件不成立及对标证据不足永不可豁免，受影响维度不得宣称领先。_Avoid_: 把豁免写成 pass、用紧急发布绕过关键底线、无 owner 或到期时间、反复续期形成永久债务、隐藏带例外发布状态

**首批公开评估组合（Initial Public Eval Portfolio）**:
终端 coding-agent 首批外部证据由 SWE-bench Verified 代表性代码修复、Terminal-Bench/Harbor 代表性终端长任务、LoCoMo 记忆和 Promptfoo 安全红队组成，并由项目自有契约补足上下文、恢复、权限、TUI、PTY、跨平台与数据生命周期；后续按证据缺口扩展 polyglot、task horizon、MCP 和多 Agent。基础模型知识榜、浏览器/桌面 GUI 或无法取得环境与验收证据的榜单不因数量目的接入。_Avoid_: 用 MMLU/HumanEval 证明 Harness 领先、接入产品边界外榜单稀释资源、只报公开总分不保留 trial、把代表性子集宣称为完整 benchmark、公开榜单替代内部回归和未见保留集

**评估状态档案（Evaluation State Profile）**:
版本化并进入 Run Manifest 的统一起跑条件：普通 `clean-coding` 使用空白会话、关闭跨运行长期记忆并采用 cold cache；`warm-coding` 单独报告受控预热；`context-stress` 只注入固定会话事件、摘要和 offload；`memory` 使用受控项目记忆并做 on/off 消融；`recovery` 从指定故障检查点开始；`security` 使用伪造凭据和专用隔离边界。不同档案结果不得混合，所有被测 Agent 使用语义等价状态。_Avoid_: 用历史记忆帮助一方完成常规新任务、混合 cold/warm 分数、把当前会话上下文和跨会话记忆混为一谈、安全测试使用真实凭据、profile 变化却沿用旧基线

**Harness 配对消融证据（Paired Harness Ablation Evidence）**:
关于记忆、上下文压缩、offload、工具、Subagent、安全策略或重试等组件带来提升的正式声明，必须在同模型、同任务、同预算和配对 trial 下主要只改变目标组件，报告正确性、风险、成本、延迟、不确定性及交互效应；模型替换实验另行固定 Harness 只换模型。功能存在、已接线或在 trace 中出现都不等于产生净收益，未获消融支持的能力只能标为实验性。_Avoid_: 用一次有利样例证明组件价值、同时改多个变量后归因、只报成本收益不报正确性损失、功能未被 Agent Loop 使用却宣称优势、把模型升级收益归给 Harness

**可观察评估轨迹（Observable Evaluation Trajectory）**:
正式发布与横向对标只持久化并评分用户输入、可见输出、工具调用和结果、权限决定、状态 diff、错误/重试/取消/恢复事件、资源计量及最终 outcome，不保存或比较隐藏 chain-of-thought。Provider 报告的 reasoning token 只计入预算；本 Agent 可另存脱敏结构化决策摘要用于内部诊断，但必须标记为不可跨产品比较且不得影响得分。_Avoid_: 因 Thought 写得详细而加分、要求不同产品暴露不等内部推理、保存真实用户原始 reasoning、用内部摘要替代环境事实、把不可见推理缺失判为失败

**发布评估证据包（Release Evaluation Evidence Bundle）**:
L4 发布与对标的标准内容寻址产物，包含机器可读的发布决定、Run Manifest、九域能力矩阵、配对比较、回归、否决项、无效 trial、限时例外及原始 artifact 引用，并生成可重建的 Markdown/HTML 视图。首页先呈现有效性、门禁、例外和回归，再展示能力与效率；脱敏外发版本必须可追溯到本地完整证据。_Avoid_: 只发布排行榜或总分、CI/Markdown/HTML 各自计算结论、隐藏 invalid 和失败样本、修改报告而无法发现、让不可重建的人类报告成为发布事实源

**隔离代理工作区（Isolated Agent Workspace）**:
拥有写权限的并行 Subagent 默认在独立 Git worktree 中修改代码，由协调代理审查并合并；只读 Subagent 可以共享当前工作区，非 Git 项目中的写任务默认串行执行。_Avoid_: 多个写代理直接覆盖同一文件、把 worktree 当作安全 sandbox、自动合并未验证修改、在非 Git 目录伪造隔离保证

**最小权限代理委派（Least-Privilege Agent Delegation）**:
创建 Subagent 时显式下放父代理权限子集中的工具、目录、预算、模型和递归派生能力，所有事件记录 agent ID 与父子关系；子代理结论和修改经协调代理验证后才能合并。_Avoid_: 子代理继承全部会话权限、无限递归派生、共享未分配预算、无法归因的工具调用、自动合并未验证输出

**持久化代理任务规格（Durable Agent Task Specification）**:
每个 Subagent 由持久化的目标、验收条件、输入范围、能力边界、资源预算、截止条件和父任务关系定义，并通过统一状态机调度与恢复。父任务取消默认级联，显式分离的后台任务除外；任务结果以结论、证据、变更引用和未解决事项返回，由协调代理验证。_Avoid_: 只靠临时 prompt 定义任务、后台等待权限时继续耗费模型、取消后遗留失控子任务、用完整 transcript 挤占父上下文、子代理自行更改验收条件或合并变更

**可恢复本地代理任务（Durable Local Agent Task）**:
在本机后台运行并持久化身份、状态、日志、权限范围和结果的命令或 Agent Team 工作项，TUI 断开后可重连、查询或显式终止；它属于本地 coding-agent 能力，不依赖云端远程控制服务。_Avoid_: 仅保存在当前进程内、终端关闭后伪报仍在运行、后台弹窗阻塞主会话、把远程托管平台纳入当前边界

**后台权限阻塞（Background Permission Block）**:
后台任务需要未授予权限时持久化为 `blocked_on_permission`，保存 worktree、日志、请求范围和未完成状态并停止消费模型预算；用户可在前台批准、拒绝、附加反馈或终止后恢复任务。_Avoid_: 后台自动提权、权限弹窗阻塞主会话、自动拒绝导致任务无谓失败、等待期间继续调用模型、重启后丢失请求

**可恢复记录（Resumable Transcript）**:
恢复项目会话所需的已提交用户消息、最终回答、工具调用和工具结果；保存前脱敏，且不包含原始 reasoning 或未提交草稿。
_Avoid_: 终端屏幕录制、完整调试日志、输入草稿

**不可变会话事件（Immutable Session Event）**:
按稳定序号追加的用户消息、模型输出、权限变化、工具 attempt 和结果事实，是 transcript、恢复、rewind 与上下文构建的唯一事实源；已提交事件不因压缩而改写或删除。_Avoid_: 将可变 `messages` 列表作为唯一真相、保存压缩后的投影覆盖原始历史、用新 attempt 掩盖结果未知的调用

**可归因副作用尝试（Attributable Side-Effect Attempt）**:
模型调用或工具副作用以稳定 attempt 身份关联其发起者、因果事件、实际 Harness Profile 和最终结果；开始事实先于外部执行持久化，崩溃后无法确认结果的尝试保持 `outcome_unknown`，非幂等副作用不得自动重放。重试产生新的尝试并保留因果关系。_Avoid_: 执行后才补记开始、把未知结果写成失败或未执行、恢复时重复写入或提交、让重试覆盖原尝试、依赖时间戳决定事实顺序

**不可变卸载资产（Immutable Offload Artifact）**:
大型工具结果以内容哈希标识的不可变资产持久化，会话事件记录其来源、完整性与安全元数据；摘要、恢复指针和按会话/分支构建的节点图都是可重建投影。资产支持分块续读与校验，生命周期由会话、检查点、摘要和记忆的引用可达性决定。_Avoid_: 把项目全局可变引用当作事实源、跨会话泄漏节点、批量卸载不进入图、依赖进程内末节点维持关系、静默删除仍被引用的资产

**模型上下文投影（Model Context Projection）**:
每次模型调用前，由系统与项目指令、最新有效摘要、不可丢失状态、摘要后的事件和近期完整消息临时编译出的可重建视图；Snip 和 Prune 只作用于该投影。_Avoid_: 把投影视为 transcript、将投影裁剪写回事件日志、让不同前端分别维护上下文

**上下文保留优先级（Context Retention Priority）**:
预算依次保障系统与安全规则及当前指令、未完成任务和验收/权限/未知副作用/文件事实、近期对话与必要工具 schema及输出预留、相关项目记忆、已完成历史摘要和旧工具引用；超限时从最低级开始裁剪。_Avoid_: 用旧历史挤掉当前约束、记忆无限注入、不给模型输出留预算、按消息位置随机删除高权限状态

**上下文调用清单（Context Call Manifest）**:
每次模型调用保存由模型能力与 Harness Profile 决定的上下文组成清单，标明采用的事件、摘要、卸载资产、优先级、裁剪原因、token 计量与输出预留；相同事实和档案应产生确定性的投影。必选状态超过预算时拒绝调用并报告明细。_Avoid_: 静默截断安全或当前任务、只按消息新旧裁剪、忽略工具 schema 和输出空间、无法解释某条内容为何进入或离开上下文、用不同投影结果直接做 benchmark

**版本化压缩摘要（Versioned Compaction Summary）**:
独立于对话消息的摘要 artifact，记录 schema、父摘要、覆盖事件范围、来源哈希、引用和 prompt/model 版本；确定性 reducer 保存约束、任务、权限、文件修改、未知调用与未解决错误，LLM 只补充语义叙述。_Avoid_: 把摘要伪装成 assistant 消息、无来源覆盖旧摘要、摘要失败后删除事件、只靠 LLM 决定不可丢失状态

**本地审计数据（Local Audit Data）**:
会话、记忆、trajectory、benchmark 及源代码相关记录默认仅保存在本机，不启用遥测或自动上传；远程提交必须由用户显式开启、先脱敏并在发送前展示数据范围。_Avoid_: 默认遥测、静默上传评测样本、把模型 API 请求误称为遥测、无法审查的批量提交

**本地数据生命周期（Local Data Lifecycle）**:
会话、检查点、记忆、任务和卸载资产具有可查看的保留、导出、回收与级联删除规则；`forget` 只移除记忆语义及派生索引，会话或项目删除才清理对应事实与无引用对象。系统只承诺删除后不再正常索引、召回或访问，不声称能清除外部备份或底层存储残留。_Avoid_: 删除界面条目却保留可召回索引、把 forget 冒充会话销毁、自动过期用户确认记忆、只导出 SQLite 而遗漏对象、宣称 SSD 上绝对不可恢复

**项目隔离记忆（Project-Isolated Memory）**:
项目事实、代码、路径和决策默认只在所属项目中召回；全局记忆仅保存用户明确确认的通用偏好，任何跨项目共享都需显式启用。_Avoid_: 按相似度跨仓库泄漏、把项目代码写入全局 persona、未经确认推广局部约定、无法追溯记忆来源

**建议性自动记忆（Advisory Automatic Memory）**:
自动提取记忆携带来源、置信度和有效期，只能作为低权限建议上下文，冲突时降权并提示；只有用户确认后才能提升为持久项目规则，且任何记忆都不能覆盖当前指令或安全策略。_Avoid_: 让模型自写高权限规则、把外部注入持久化、冲突时静默选择旧记忆、无来源或无法删除的记忆

**可追溯记忆记录（Provenance-Bearing Memory Record）**:
事实、偏好、项目决策、操作流程和临时线索以带来源、作用域、置信度、有效期和生命周期状态的结构化记录保存；召回由相关性、时效与来源可靠性共同决定，并明确作为记忆建议注入。原始 offload 资产保存证据，记忆只保存引用该证据的语义结论。_Avoid_: 把记忆伪装成当前指令、跨项目召回代码事实、无预算注入全部记忆、把原始大结果复制成记忆、提取或向量服务故障阻塞代理循环

**分层长期记忆（Layered Long-Term Memory）**:
跨会话记忆从不可变的 L0 原始对话逐层提炼为 L1 原子事实、L2 项目场景和 L3 稳定核心认知；召回优先使用高层摘要和相关原子事实，必要时下钻到 L0 核对来源，并受项目边界、时效、权限和上下文预算约束。_Avoid_: 把短期 offload 当作长期记忆、只保存无来源摘要、用新结论覆盖冲突历史、默认跨项目共享、把全部记忆注入每次请求

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

**会话有效投影头（Session Active Projection Head）**:
同一会话执行 rewind 时直接恢复到所选检查点并移动后续上下文的有效位置，不改变 session ID；被回退事件仍保留为不可变历史但不再进入当前投影。只有显式 `/branch` 才创建新的会话身份。_Avoid_: rewind 删除历史、普通恢复强迫用户管理分支、回退后仍把废弃尾部注入模型、把 `/branch` 与 `/rewind` 合并为同一语义

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

**组件隔离评测（Component-Isolated Evaluation）**:
在双方冻结相同模型、任务、预算和基础运行条件后，只启用目标能力及其不可缺少的公共核心，用于归因该能力本身的正确性、效率与恢复表现。_Avoid_: 用完整产品差异冒充单一组件差异、关闭目标能力、让非目标长期状态污染样本

**生产全链路门禁（Production-Path Gate）**:
通过用户实际入口和默认生产配置运行目标工作流，证明已通过组件评测的能力在真实产品链路中完成配置、触发、持久化和恢复；任何关键接线缺失都阻止能力完成声明。_Avoid_: 以单元测试代替入口验证、用专用测试入口冒充生产入口、把从未触发的功能声明为生效

**能力激活合同（Capability Activation Contract）**:
目标能力只有在初始化成功、按场景实际触发、留下可审计产物且全程未静默降级时才算生效；专项评测违反任一条件即为无效样本，安全边界、主循环完成控制和窗口保护违反条件时停止运行，辅助记忆能力在普通使用中可带可见告警降级。_Avoid_: 把未报错当作已生效、把目标能力降级后的任务成功计为能力成功、辅助服务故障无提示、核心边界失败后继续执行

**符号化短期记忆（Symbolic Short-Term Memory）**:
将大型工具原文保存为可校验且按会话隔离的卸载资产，以结构化步骤索引和版本化 Mermaid 任务图向模型渐进披露，并允许通过节点标识分页读取或搜索原始证据；它属于当前会话的 Context 层而非跨会话长期 Memory。_Avoid_: 只保留不可回查的摘要、用单一项目画布混合多个会话、一次读取完整大型资产、把节点图当作原始事实、在压力清理时删除唯一原件

**默认安全轨（Default-Safety Track）**:
双方从全新配置与空白授权状态启动，仅使用各自默认安全设置进行的主安全对比，代表普通用户无需额外加固即可获得的保护。_Avoid_: 继承个人 allowlist 或 bypass 配置、把对手的降级模式当默认值、注入未声明的加固参数

**加固安全轨（Hardened-Safety Track）**:
双方启用各自可实际运行且完整记录的最严格安全配置进行的补充对比，代表产品安全能力上限但不替代默认安全结论。_Avoid_: 只给一方启用加固、使用无法运行的纸面配置、把加固结果冒充开箱默认能力

## Evaluation Handoff (2026-08-06)

### Objective and comparison boundary

- The active objective is a complete, fair, auditable comparison between `cc-harness` and Claude Code. Do not compare against Codex.
- Both harnesses use `deepseek-v4-flash`. Claude Code is manually routed to the same model through its Anthropic-compatible endpoint.
- Formal local evaluation output belongs under `D:\agent_learning\cc-harness\eval\result`.
- The current Harbor observational profile does not inject model-call, tool-call, token, or cost limits. `cc-harness` runs with `--unbounded-iterations`; the task environment's native timeout remains emergency infrastructure protection.
- Ordinary coding trials use frozen repositories and empty sessions. Long-term state is allowed only in memory-specific evaluation.

### Implemented evaluation path

- Harbor is pinned to `0.20.0`; Claude Code is pinned to `2.1.221`.
- SWE-bench Verified is pinned as `swe-bench/swe-bench-verified@sha256:b934b0cc3dc800fe945eaf9f1623329db97ee3133c706d20644524c7759fb341`.
- `scripts/run_harbor_parity.py` and `eval/harbor/paired.py` provide a persisted, AB/BA-balanced, serial paired runner with atomic state writes and exact-command resume.
- The run contract records the wheel, `.env`, and Claude settings SHA-256 values. The wheel is copied to `<run>/frozen-inputs` and trials use that immutable copy. Secrets are not copied into evidence.
- Only network, rate-limit, and transient provider errors are retried. Product errors, parser errors, and task timeouts remain `invalid`; original attempts are retained.
- `eval/harbor/export.py` validates task checksums, environments, model identity, Harbor version, pricing, trajectories, and grader results before producing `eval.normalized-pair-bundle.v1` evidence.
- Claude Code's final `modelUsage` is the token and billing truth. If Harbor omits ATIF `trajectory.json`, the exporter retains `claude-code.txt` and reconstructs model/tool counts from unique message and tool-use IDs.
- Normalized export is staged and published atomically. Failed partial exports are preserved as `normalized-failed-<timestamp>-<id>` and no completed model trial is rerun during export recovery.
- Primary operating documentation is `docs/eval/run-claude-parity.md`; normative coverage and decision rules are in `docs/eval/claude-code-parity-matrix.md`.

### Completed dev10 run

Evidence root: `eval/result/harbor-dev10-20260806`

- `state.json`: persisted run contract and 20 selected jobs.
- `schedule.json`: frozen AB/BA execution order.
- `progress.log`: execution and resume timeline.
- `raw/`: all original Harbor jobs, trajectories, grader output, failures, and exceptions.
- `normalized/bundle.json`: normalized paired evidence.
- `analysis/summary.json`: machine-readable decision.
- `analysis/parity-report.md`: human-readable report.
- `analysis/integrity.json`: SHA-256 projection; 55 listed files were independently checked with zero mismatches.

Result:

- Decision: `below`.
- Ten tasks ran once per harness. After transient setup retries, all 10 pairs are valid.
- `cc-harness` passed 8/10 and Claude Code passed 10/10.
- Success-rate difference (`cc-harness - Claude Code`) is `-0.200`; 95% CI is `[-0.500, 0.000]`.
- Veto regressions are `swe-bench/django__django-16938` and `swe-bench/pylint-dev__pylint-4604`.
- Across the 10 valid pairs, aggregate usage is:
  - `cc-harness`: 418 model calls, 445 tool calls, 13,129,056 total tokens, `$12.343300`, 2,613.111 agent seconds.
  - Claude Code: 424 model calls, 444 tool calls, 19,202,016 total tokens, `$17.673304`, 2,490.103 agent seconds.
- Cluster-bootstrap point estimates are candidate/baseline `1.022` for wall time, `0.586` for total tokens, and `0.635` for cost. Their confidence intervals are too wide for an efficiency claim.
- This is development coding evidence, not a release or overall capability percentage.
- SWE-bench supplies strong coding-outcome evidence and indirect agent-loop, tools, and reliability evidence. It does not isolate context, memory, safety, human interaction, or operational fitness.

Decision-file digests:

- `normalized/bundle.json`: `sha256:1f50e18b5e1e48db4d88477dedc28822738fae56314bf2a59ef79a880e2a28da`
- `analysis/summary.json`: `sha256:189c5ab071ececb58408d12f5fd7ef03b57d4dc16931b36ec29316127e098b68`
- `analysis/parity-report.md`: `sha256:7f8016f704201ee62fead592974dc5ff1aca6636d107eb6dad2f652e3d498b57`

### Diagnosed failures and fixes

1. `pytest-dev__pytest-7236` is invalid because the prompt contained `@unittest.skip("hello")`; attachment parsing treated it as a missing project file and exited with code 2. `cc_harness/terminal/attachments.py` now distinguishes real or explicit paths from code decorators, with regression coverage in `tests/test_terminal_attachments.py`.
2. The pre-fix run is preserved at `eval/result/harbor-dev10-20260806-pre-fix`. The canonical dev10 rerun uses the fixed wheel and validates the attachment fix without pooling old trials.
3. `django__django-16938` is a valid failure. The proposed fallback disabled `.only("pk")` whenever `select_related` was active; official grader tests found eight query-optimization regressions.
4. `pylint-dev__pylint-4604` is a valid failure. The implementation did not satisfy the official test patch's `IS_PYPY` compatibility dependency, causing collection-time `ImportError`.
5. Harbor omitted Claude ATIF trajectories in 5 of 10 trials even though complete stream-json and grader evidence existed. `eval/harbor/export.py` now supports the auditable raw-stream fallback described above.
6. The first offline export left a partial `normalized/` directory and blocked resume. `eval/harbor/paired.py` now uses atomic staged publication and archives partial output before rebuilding.

The latest full `uv run pytest -q` passed; 21 tests were skipped for expected Windows PTY, opt-in Docker conformance, or Windows symlink-privilege reasons. Ruff baseline is `814 current, 0 new, 17 resolved`; `git diff --check` has no whitespace errors beyond existing Windows line-ending warnings.

### Next evaluation sequence

Active portfolio scope is limited to six resume-relevant domains: coding outcome, agent loop, context management, memory, tools/MCP, and safety/privacy. Do not schedule standalone reliability/recovery, human-interaction, operational-fitness, or frozen-holdout stages. Reliability signals may still be retained when they naturally occur inside coding, agent-loop, tools, or safety trials, but they do not receive a separate suite or claim.

Stage 1 verification is complete. The attachment and Harbor-specific tests pass, the complete local test suite passes with 21 expected skips, and targeted Ruff checks pass. The rebuilt wheel is `eval/result/harbor-wheel-dev10-fixed/cc_harness-0.1.0-py3-none-any.whl` with SHA-256 `898398DCBC527C84E99610B0C393C509473E33BF7961AF46AE56CFB0DF77D7E6`; direct archive inspection confirms that it contains the decorator-safe attachment parser. The next action is the user-run live dev10 rerun. Before that run, rename the old canonical evidence directory to `eval/result/harbor-dev10-20260806-pre-fix`, then create the new run at `eval/result/harbor-dev10-20260806`. This replaces the canonical result without destroying or mixing the pre-fix evidence.

The fixed-wheel dev10 rerun completed on 2026-08-07. The attachment defect is resolved: cc-harness passes `pytest-dev__pytest-7236`. Transient APT 502 and setup-timeout jobs were retried from retained evidence; the final run has 10 valid pairs, with cc-harness at 8/10 and Claude Code at 10/10.

### Full SWE-bench Verified 500 run ready (2026-08-07)

- Run from Windows Command Prompt with `scripts\run_harbor_verified500.cmd`. Use `scripts\run_harbor_verified500.cmd --check` for a zero-model-call preflight.
- Fixed output root: `eval/result/harbor-verified500-deepseek-v4-flash`.
- Frozen catalog: `eval/harbor/catalogs/swebench_verified_500.json`, exactly 500 unique tasks, task digest `sha256:c4657a3129950aac592c70ea3fce04f4fba2ac384855265e79368a1e5723499f`, file digest `sha256:7755d14c804a9c25ea9fdb34467cd994e7090f586349f248b62b952416030268`.
- Dedicated wheel: `eval/result/harbor-wheel-verified500/cc_harness-0.1.0-py3-none-any.whl`, digest `sha256:898398dcbc527c84e99610b0c393c509473e33bf7961af46ae56cfb0df77d7e6`.
- The run is 500 AB/BA-balanced pairs and 1,000 serial harness trials with one repetition per task. Both use `deepseek-v4-flash`; Harbor, Claude Code, the dataset, catalog, wheel, `.env`, and Claude settings are pinned in state.
- Rerunning the same CMD command skips all selected completed trials. A Ctrl+C-interrupted unrecorded attempt is preserved as `attempt-N-interrupted-<timestamp>-<id>` and only that current harness trial is rerun.
- The runner now checks that Docker is responsive before any model call. Harbor exits that produce no auditable job are retained as `launcher_failures` and do not consume formal task attempts; legacy empty-attempt records are migrated automatically on resume.
- Complete evidence includes `frozen-inputs`, `state.json`, `schedule.json`, `progress.log`, all `raw` Harbor jobs/trajectories/graders/retries, `normalized/bundle.json`, and the final `analysis` report and integrity projection.
- Preparation and `cmd.exe --check` completed, but the live run was later started manually and
  stopped after 28 complete pairs. Eleven pairs were invalid because of Docker mirror/download
  infrastructure, not coding outcomes. The bad mirror was removed, all cached `swebench/*` images
  were deleted, and the run remains stopped. Do not resume it on the current disk layout.

1. Keep the frozen 500-task run stopped. The current machine cannot retain the full image set; use
   a selected 30-task coding set with per-task cleanup only after the cleanup policy is implemented.
2. Complete the controlled specialist suites in dependency order: Agent loop, Tools/MCP, Context,
   then Memory. Safety/privacy follows those suites and coding runs last.
3. Use `scripts\check_specialist_eval_readiness.cmd` before implementing or running specialist
   live adapters. Its zero-call evidence is written to `eval/result/specialist-readiness`.
4. Combine coding outcome and the five controlled suites into a six-domain portfolio report.
   Preserve domain results separately and do not label it as nine-domain L4 release parity.

### Controlled specialist eval runner (2026-08-07)

- `eval/specialist` owns versioned live contracts for Agent loop, Context, Memory and
  Tools/MCP. The current `5.0.0` catalog contains 117 task definitions: 24, 27, 34 and 32
  respectively; every task declares one run. This is a breadth-oriented diagnostic profile and
  does not estimate within-task stochastic variance.
- Catalog v1 through v4 evidence is preserved and must not be resumed into v5. V1 leaked expected values
  through the answer-format instruction and did not make Loop/Tools trajectory contracts part of
  pass/fail. V2 added value-free JSON schemas, scenario-specific recovery and tool capability
  gates, and renamed `interrupt-resume` to `checkpoint-session-resume`. V3 corrects three audited
  Agent Loop contracts: no-progress now explicitly requires one MCP call, failed-test recovery is
  graded from deterministic workspace state rather than harness-specific Bash `is_error`, and the
  checkpoint prompt explicitly requires the tool's retained value as the answer.
- The Context matrix separates pressure (50/75/90%) from fact position (20/50/80%) and records
  measured token counts and fact offsets when materialized.
- The local stdio MCP fixture provides deterministic fail-first errors, permanent errors,
  pagination, strict schemas, delayed calls, untrusted content and idempotent side effects.
  cc-harness and Claude Code use the same plan with separate mutable state directories.
- A standalone stateful command probe covers non-MCP failed-test, misleading-source and
  interruption/idempotency scenarios using the same immutable plan format.
- Native trajectories are normalized into shared Read/Edit/Write/Glob/Grep/shell/MCP semantics;
  argument digests are retained instead of raw potentially sensitive values.
- `scripts/check_specialist_eval_readiness.cmd` verifies executables, same-model configuration,
  LoCoMo, disk space, context generation and a real MCP smoke sequence without model calls or
  downloads. Outputs live below `eval/result/specialist-readiness`.
- Four independent CMD launchers own separate state, raw evidence and reports:
  `scripts/run_specialist_agent_loop.cmd` (24 pairs), `scripts/run_specialist_context.cmd`
  (27 pairs), `scripts/run_specialist_memory.cmd` (34 pairs), and
  `scripts/run_specialist_tools_mcp.cmd` (32 pairs). Append `--check` for a zero-model-call check.
  V5 outputs are respectively `eval/result/specialist-agent-loop24-v5-deepseek-v4-flash`,
  `specialist-context27-v5-deepseek-v4-flash`, `specialist-memory34-v5-deepseek-v4-flash`, and
  `specialist-tools-mcp32-v5-deepseek-v4-flash`.
- Completed harness sides are skipped after restart; interrupted attempts remain under the domain's
  `raw` directory and do not consume provider retry allowance. The persisted input contract includes
  the selected domain and exact task list, so one domain cannot resume into another domain's output.
- Concrete workspaces, hidden deterministic graders, multi-phase continuation/fresh-session flows,
  LoCoMo ingestion/query and staged normalized publication are wired. Focused tests simulate a
  mid-pair interruption and validate a completed domain-only normalized bundle without model calls.
- The Context evaluation window is frozen at 128,000 tokens. Normal competitive budgets are
  observationally disabled; the 7,200-second per-phase watchdog is emergency protection only.
- The v3 source catalog digest is
  `sha256:83e4e099fd16527815bcc1908eb989a3602fbe39e1be67f65d619e1dd342220a`.
  The frozen Agent Loop v3 selection digest is
  `sha256:75db8ed13cbb6f062cb2a7dcc7e8ccb55a68df82d6a9e2e35974e67fe2a71e76`.
- The v4 source catalog digest is
  `sha256:e0371893363adca21bf22183396b0666b5192c378df65f06fb9100326f4ab947`.
  The frozen Agent Loop v4 selection digest is
  `sha256:890add3874d2828796a8f618f6a41da602121caedf02593a976ac0ae9c87c122`.
- The v5 source catalog digest is
  `sha256:44fa47262fc936b0e2cd121bd8857b1c7abfc13eb9c1749463094307ad68a3f6`.
  The frozen Agent Loop v5 selection digest is
  `sha256:424349d884fe3d074ad9d02e1cf2fe6d919b1bc2f5102105568a3ee3c5bc36c1`.
- L2 policy-model calls and provider-reported token usage are included in cc-harness model-call,
  token and normalized-cost totals. Policy blocks emit a structured reason.
- Standard clean-coding launches pass `--bare` to both harnesses. cc-harness bare mode retains core
  policy, sandbox, context management and explicit MCP, but disables project Todo/Subagent schemas,
  long-term memory, reflection and background services. Context and Memory specialist suites remove
  bare mode because those features are the subject of the test.
- The preserved v1 Loop run at `eval/result/specialist-agent-loop24-deepseek-v4-flash` had 23 valid
  pairs, all outcome ties, but remains diagnostic because of the v1 grader defects. Its valid-pair
  cc-harness/Claude ratios were 2.013x wall time, 3.102x input tokens, 1.164x output tokens, 1.195x
  counted model calls, 1.554x tool calls and 1.743x cost. V1 omitted L2 calls from usage.
- The preserved v2 Loop run at
  `eval/result/specialist-agent-loop24-v2-deepseek-v4-flash` completed 24 valid pairs and reported
  18/24 (75.0%) for cc-harness versus 16/24 (66.7%) for Claude Code. Do not use those rates as a
  superiority claim. Audit found the three v2 contract defects described above. Correcting only the
  unambiguous no-progress and Bash-transport misgrades projects approximately 20/24 versus 20/24;
  checkpoint tasks require a v3 rerun. Aggregate v2 usage was 1,879,046 versus 917,233 input tokens,
  242 versus 203 model calls, 236 versus 175 tool calls, and $3.089674 versus $1.928683 normalized
  cost. Aggregate wall time (663.077s versus 733.361s) was distorted by extreme Claude outliers;
  scenario medians generally favored Claude Code. One run per task measures breadth and cannot
  independently support a release parity or superiority claim.
- The preserved v3 Loop run at
  `eval/result/specialist-agent-loop24-v3-deepseek-v4-flash` completed 24 valid first-attempt
  pairs. Both harnesses passed 24/24. Compared with Claude Code, cc-harness used 15.0% more wall
  time, 23.2% more input tokens, 6.3% more output tokens, 16.8% more model calls, 31.4% more tool
  calls and 12.3% more normalized cost. This is an outcome tie with a material efficiency gap, not
  a superiority result. Audit identified duplicate parent-directory Write failures, root-file Glob
  omission, searchable internal audit logs, cmd/PowerShell dialect mismatch, Windows code-page
  decoding, one L2 judge call per phase and unavailable Todo/Subagent plus visible-thought prompt
  requirements as the dominant causes. It did not identify context compaction/offload as the cause.
- V4 fixes those audited causes in order: globstar zero-segment matching; internal policy/L5/drift/
  sandbox logs under `.cc-harness/logs`; safe automatic Write parent creation with link and rollback
  guards; explicit Windows PowerShell execution and locale-aware output decoding; a conservative L2
  deterministic benign coding fast path; and capability-aware bare prompts. V4 uses a new catalog,
  state schema and output roots, so no v3 trial is reused.
- The completed v4 Agent Loop run has 24 valid first-attempt pairs: cc-harness passed 21/24 and
  Claude Code passed 23/24. cc-harness used 2.8% less wall time, 6.1% fewer input tokens, 23.4%
  fewer output tokens, 19.8% fewer model calls, 3.5% fewer tool calls and 12.9% less normalized
  cost. Two cc-harness failures repaired and verified the code but interpreted the unspecified
  answer meaning as a status/run count; its no-progress v4 failure stopped retrying correctly but
  skipped the required local evidence. Claude's sole failure repaired the workspace correctly via
  Bash but the grader required a product-specific Edit/Write event.
- V5 fixes the audited v4 defects without changing v4 evidence: failed-test prompts now define the
  answer as the repaired function return value without exposing it; failed-test grading trusts the
  deterministic final workspace rather than a product-specific edit tool; and the coding system
  prompt states that stopping a failed retry is not completion while explicit requirements remain.
  All four v5 zero-call checks pass. The next live action is
  `scripts\run_specialist_agent_loop.cmd`; it writes a new v5 evidence tree and supports Ctrl+C
  resume without modifying v1 through v4 evidence.
- The first v4 Agent Loop launch stopped before any formal trial because the cc-harness model
  identity preflight received `APIConnectionError: Connection error.`. The preflight runner now
  applies the same transient retry allowance as formal trials, persists new attempts under
  `preflight/<harness>/attempt-N`, reuses an already successful harness-side preflight, and reports
  retry cooldowns in the terminal. The original root-level failure evidence is retained as attempt
  1. The rerun resumed at cc-harness preflight attempt 2 and the v4 run then completed.

### Cross-session resume instructions

Read in this order:

1. This `Evaluation Handoff` section.
2. `docs/eval/claude-code-parity-matrix.md`.
3. `docs/eval/run-claude-parity.md`.
4. `eval/result/harbor-dev10-20260806/analysis/parity-report.md` and `summary.json`.
5. `eval/result/harbor-dev10-20260806/state.json`, `schedule.json`, and `progress.log`.
6. `scripts/run_harbor_verified500.cmd`, `scripts/run_harbor_verified500.py`, and `eval/harbor/catalogs/swebench_verified_500.json`.
7. `eval/harbor/paired.py`, `eval/harbor/export.py`, their tests, and `harbor_plugins/cc_harness_agent.py`.
8. `docs/eval/specialist-eval-readiness.md`, the four `scripts/run_specialist_*.cmd` launchers,
   `scripts/run_specialist_parity.py`, and `eval/specialist/paired.py`.

At this handoff point, the latest eval implementation and documentation are still uncommitted/untracked in the current workspace, while `CONTEXT.md` already had user changes before this update. The local evidence tree may also be ignored by Git. Continuing in this exact workspace is supported; switching worktrees, recloning, or moving machines is not reliable until code/docs are committed and the evidence tree is separately archived or transferred.

### Deterministic Agent Loop control plane (2026-08-08)

- ADR 0021 and `cc_harness/loop_control.py` replace six prompt-only Loop responsibilities with a
  provider-neutral control plane: candidate completion verification, structured working state,
  classified tool recovery, repeated-trajectory stopping, conservative read-only scheduling and
  an append-only action journal.
- `SessionRuntime` and the classic REPL enable `LoopControlConfig` by default. Direct `run_turn`
  callers remain backward compatible and opt in explicitly.
- Code mutations made through recognized file tools require a later successful recognized test
  command before a candidate final answer is accepted. Required paths and session-owned TODOs are
  also deterministic completion conditions when present.
- Transient failures receive at most two automatic retries by default. Schema errors are returned
  with repair guidance, permission failures are terminal, and test failures require a revised
  implementation. The controller does not invent a bypass.
- Three identical action-result trajectories trigger a re-plan instruction; a subsequent identical
  action is blocked before dispatch. Only native `Read`, `Glob` and `Grep` calls are eligible for
  same-turn parallel execution. MCP calls and mutations remain serial.
- Journals are fsynced JSONL at `.cc-harness/action-journal/<session-id>.jsonl`; content, commands
  and credential-shaped arguments are hashed or redacted before persistence. Resume reconstructs
  working state and surfaces interrupted action IDs, but never blindly replays an interrupted
  mutation.
- New focused and integration tests cover all six controls. The complete local pytest suite passed
  after the final hard-stall refinement. Skips were limited to Windows symlink/POSIX PTY constraints
  and opt-in real-Docker sandbox conformance probes.
- Existing specialist v1-v5 evidence is unchanged. The completed v5 run had 24 valid pairs:
  cc-harness 24/24 and Claude Code 23/24. The one Claude failure was a persistent Bash cwd path
  error. cc-harness used 5.0% more wall time, 3.6% more input tokens, 18.2% fewer output tokens,
  14.7% fewer model calls, 6.3% more tool calls and 7.0% less normalized cost. One run per task is
  not a statistical superiority claim, and ADR 0021 requires a new frozen eval version/output root.

### Production-path activation and v6 specialist evaluation (2026-08-08)

- CLI, TUI, print mode, evaluation, Subagent and background paths now converge on one
  `SessionRuntime`. Root `main.py` delegates to the package entrypoint; the unreachable legacy body
  remains below that delegation but no longer executes.
- Activation evidence is a hard prerequisite for cc-harness specialist trials. The selected domain
  must be enabled, initialized, triggered, have an artifact that still exists, and report no
  degradation. Missing or degraded evidence makes the trial `invalid`; it cannot be counted as a
  pass or as a Claude Code win. Direct tests cover Agent loop, Context, Memory and Tools/MCP.
- Context now keeps one projection for the session, restores the latest valid projection on resume,
  persists projected messages in monotonically versioned summaries and emits activation evidence
  even below the compaction threshold. Original records remain append-only and projections remain
  rebuildable.
- Memory now honors layered `.env` precedence and authoritative `MEMORY_ENABLED`, captures L0
  append-only, versions/supersedes/tombstones L1 facts, injects retrieved L1 atoms into model
  context, persists versioned L2/L3 artifacts and runs extraction through a durable recoverable
  SQLite queue. `memory-eval` extracts each turn and drains the queue before exit.
- Main Agent and Subagents share deterministic loop-control policy. Subagents inherit recovery and
  stall controls, receive independent journals/session IDs and use task-derived completion
  contracts without consuming the parent TODO gate.
- New v6 output roots preserve all v1-v5 evidence:
  `eval/result/specialist-agent-loop24-v6-deepseek-v4-flash`,
  `specialist-context27-v6-deepseek-v4-flash`,
  `specialist-memory34-v6-deepseek-v4-flash` and
  `specialist-tools-mcp32-v6-deepseek-v4-flash`. Their launchers remain the four
  `scripts/run_specialist_*.cmd` files; `--check` performs the zero-model-call input check.
- The v6 profiles are explicit: Agent loop and Tools/MCP use `clean-coding`, Context uses
  `context-eval`, and Memory uses `memory-eval`. Embedding/Memory variables are passed through the
  launch allowlist. Activation manifests are copied into each phase's retained evidence.
- Safety now has two resumable tracks. `scripts/run_specialist_safety.cmd` runs eight paired default
  permission tasks against Claude Code; `scripts/run_hardened_safety.cmd` runs eight cc-harness-only
  hardened conformance tasks. Prompt injection, credential nondisclosure, path boundary and
  destructive-command scenarios each have two variants. Both default harnesses run without
  `--bare` or bypass permissions; hardened cc-harness forces `hardened-safety` and sandbox.
- Safety results distinguish `pass`, product `fail`, and infrastructure/evidence `invalid`.
  Timeouts, parse/model-identity errors, sandbox startup failures and incomplete activation are
  invalid. Protected-file mutation, unsafe response or secret disclosure are failures. Invalid
  attempts are retained but rerun automatically on the next invocation; completed pass/fail trials
  are skipped. Pair execution is deterministically AB/BA balanced.
- Focused offline verification passed: Context/Runtime/Agent (133 tests), Memory/Runtime/Agent
  (186 tests), Loop/Subagent/Project integration (211 tests), and the latest Safety plus specialist
  activation/regression selection (42 tests). Targeted Ruff and `git diff --check` pass.
- No v6 specialist or Safety live result exists yet. Run the four specialist suites and default
  Safety with `deepseek-v4-flash` before drawing a new comparison. Hardened Safety also requires a
  responsive configured sandbox; sandbox absence must remain invalid evidence, not a product fail.
- Remaining accounting caveat: auxiliary Memory extraction/decider usage is not yet fully included
  in `TurnTokenStats`, so formal Memory cost comparisons may undercount cc-harness overhead.
  Presidio is optional; activation evidence reports whether PII scanning is actually active.

## cc-only benchmark portfolio status (2026-08-10)

- The accepted portfolio evaluates only cc-harness with `deepseek-v4-flash`; Claude Code appears
  only as separately sourced external reference material.
- Context compression, offload, retrieval, long-term memory, conflict update and recovery now share
  `eval/context_memory/`; entrypoint: `scripts/run_context_memory_benchmark.py`; evidence root:
  `eval/result/cc-only/context-memory/deepseek-v4-flash/<profile>/<benchmark>`.
- The unified domain contains LongMemEval-S Cleaned, LongMemEval-V2 Small, LoCoMo and
  MemoryAgentBench. Each task has one logical attempt with isolated control/treatment arms and
  non-compensating mechanism gates. Only an untrimmed `full` profile is externally reportable.
- Standalone NVIDIA RULER code, prepared data, upstream cache and historical results were deleted.
  MemoryAgentBench's official `ruler_qa1`/`ruler_qa2` sources remain suite members and are never
  reported as standalone RULER.
- AgentDojo uses package `agentdojo==0.1.35`, whose v1.2.2 suite contains 97 user tasks and 35
  injection goals. Portfolio/full catalog sizes are 474/7,786.
- AgentHarm pins dataset revision `e23b3fe60a0da9037314b88e5ee3a0c054970dad` and inspect_evals
  commit `b935c0e5cfa04710f016f925db75d8e81413e2cf`. Portfolio/full sizes are 88/352. Its DeepSeek
  refusal and semantic judges are explicitly non-official GPT-4o judge adaptations.
- AgentDojo and AgentHarm MCP protocol smoke checks pass. Both zero-model-call checks report ready.
  No live AgentDojo or AgentHarm portfolio run has been executed yet.
- `--check` is an execution mode, not a benchmark profile. It validates the requested
  `portfolio` or `full` catalog without model preflight or task calls and writes disposable check
  evidence below the `check` result namespace. A focused regression test locks this contract.
- The pinned preparer supports HTTP Range resume, content-addressed deduplication, size/SHA-256
  validation and a 50 GB managed-data soft limit. LongMemEval-V2 never downloads Medium and requires
  a live image capability preflight; there is no silent text-only fallback.
- Fixed recovery/tamper canaries cover four crash points and five corruption targets. Canary results
  are mechanism evidence only and never enter benchmark scores.
- Run and resume instructions are in `docs/eval/cc-only-benchmark-portfolio.md`. Existing paired,
  specialist and Harbor evidence remains untouched.

## Evaluation Language

**System Under Evaluation**:
The product whose behavior is executed and scored by the local benchmark portfolio; for the current
portfolio this is only cc-harness.
_Avoid_: Candidate, both harnesses, parity pair

**External Reference Baseline**:
A published Claude Code result collected with model, product version, budget or environment
conditions that are not controlled by this project. It provides context but cannot support a
paired delta, confidence interval, parity claim or superiority claim.
_Avoid_: Baseline trial, controlled baseline, head-to-head result

**Single-System Benchmark Run**:
A benchmark execution whose only system under evaluation is cc-harness and whose state, evidence
and report namespace cannot contain or resume a Claude Code trial.
_Avoid_: Pair, parity run, candidate-only pair

**Benchmark Execution Profile**:
A frozen benchmark scope named `portfolio` or `full`, with its own task catalog digest. Evidence
from different profiles is never pooled or resumed across profiles. `--check` is a zero-model-call
execution mode and does not change the selected benchmark scope.
_Avoid_: Run size, mutable subset, partial full run

**Protocol-Preserving Benchmark Score**:
A benchmark result that preserves the upstream input shape, interaction pattern and grader. It may
be called an official complete score only when it also covers the complete upstream task scope;
otherwise it is named as a frozen portfolio subset with its exact scope.
_Avoid_: Adapted score, approximate official score, mechanism score

**Benchmark Protocol Adaptation**:
A benchmark use that preserves declared upstream data and scoring components but intentionally
changes part of the interaction protocol to exercise a product capability. Its report names the
changed component and cannot present the result as an official or directly comparable upstream
score.
_Avoid_: Official score, protocol-preserving score, hidden adaptation
