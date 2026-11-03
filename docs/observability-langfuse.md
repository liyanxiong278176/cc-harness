# Langfuse 运行手册与验收记录

更新：2026-10-26（Asia/Singapore）。用户已确认并授权实施；cc-harness 页面保持原状。

## 当前可用内容

- 本机界面：<http://localhost:3060>，项目为 `cc-harness-local`。
- 登录账号和随机密码在 `D:\agent_data\cc-harness-observability\deployment\compose.env` 的 `LANGFUSE_USER_EMAIL` / `LANGFUSE_USER_PASSWORD`；API 密钥在同目录及上级 `config.json`。这些文件不提交。
- 启用时间记录在 `config.json.enabled_at`。只导入其后创建的任务，旧任务不会因恢复服务而回填。
- Runtime 事实仍以原事件账本为准；Langfuse 展示观测、诊断、人工复核和实验；发布事实由 `eval/core` 保存。
- 普通成功抽查 10%，复杂成功抽查 50%，异常全选。无每日分析限额；跨进程分析并发为 1，认证或余额错误退避一小时，待补工作保留。

## 按类型排查

同一轨迹可以有多个问题，按九个能力域与具体问题命名。进入 Langfuse 的 Observations，限定 `metadata.project_id`，再按评分名、分类值和时间筛选；在任务详情中查看关联步骤及评分 comment 内的证据。

| 评分命名 | 含义 |
| --- | --- |
| `issue.current.<能力域>.<问题>` | 当前有效状态：`confirmed`、`suspected`、`dismissed` |
| `issue.rule.*` | 程序检查，有直接事实支持的已确认问题 |
| `issue.judge.*` | 模型提出的待核实线索，不参与发布否决 |
| `issue.human.*` | 人工判断及修订投影 |
| `coverage.rule.<能力域>` | 明确失败事件检查范围内的覆盖情况 |
| `coverage.judge.<能力域>` | 实际完成的语义检查覆盖情况 |
| `coverage.judge.complete` | 完整分析是否完成；未完成原因见 comment |
| `review.<能力域>.<问题>` | 在原生标注界面填写的人工复核评分 |

例如查上下文问题时，选择 `issue.current.context_management.<问题>` 为 `confirmed`；同一任务的工具问题同时通过 `issue.current.tools_and_protocols.<问题>` 检索。评分按名称区分来源，不依赖 UI 的 source 过滤。原生筛选语法与可选项以部署版本页面为准，未增加自定义两级树控件。

提供方明确报告的余额等错误会由程序直接归入 `operational_fitness`，例如 `issue.current.operational_fitness.provider_billing_error=confirmed`，不需要等待已经不可用的分析模型。规则升级只重查已经接入的新任务副本，不导入启用前历史。

`unchecked`、`checked_no_issue`、`issue_found` 分开记录；检查器的范围和版本在 comment 中。没有标签不表示已经排除问题。模型意见始终为待核实，发现线索的能力域不会同时显示为“未发现问题”。

人工复核时先选择具体证据步骤，再填写 `review.*` 的确认、驳回或待核实值，可补充理由。缺少具体问题配置时可先使用该能力域的 `review.<能力域>.other`。使用项目的原生 Annotation Queues 组织复核。对具体问题的自动判断会创建对应的复核配置。

开源版不含企业审计日志，因此 3060 端口前有透明代理：只为评分变更保存脱敏的请求意图和返回结果，不保存登录正文、Cookie 或鉴权头。同步器逐条读取成功修订，追加本地历史，再更新当前投影；同一轮询间隔内的多次修改也不会丢失。选中评分删除会形成撤回记录；批量管理删除不作为人工判定流程。

## 数据与启动

| 数据 | 实际位置 |
| --- | --- |
| 活动 Docker 数据盘 | `D:\DockerDesktop\local-appdata\Docker\wsl\disk\docker_data.vhdx` |
| SQLite 队列、判断历史、请求索引 | `D:\agent_data\cc-harness-observability\projects\` |
| 脱敏且内容寻址的轨迹资产 | `D:\agent_data\cc-harness-observability\assets\` |
| 评测夹具、试跑、core 证据 | `D:\agent_data\cc-harness-observability\evaluation\` |
| 冷备份及恢复抽检 | `D:\agent_data\cc-harness-observability\backups\` |

数据库、ClickHouse、Redis、对象存储和修订日志使用 D 盘 Docker 数据盘内的 named volumes。原 C 盘 VHDX 和最初迁移副本保留，不再用作本次活动数据盘。未配置自动按天删除轨迹。

从仓库根目录执行：

```powershell
# 启动 D 盘 Docker、Langfuse 与独立队列 worker
powershell -NoProfile -ExecutionPolicy Bypass -File scripts/start_observability.ps1

python scripts/manage_langfuse.py status
python scripts/manage_langfuse.py verify
python -m cc_harness.observability.worker --status

# 仅停止 Langfuse 服务，不删除任何卷
python scripts/manage_langfuse.py stop
```

已安装当前用户登录启动项；原 Docker 登录启动值备份在 `deployment/startup-before.json`。`verify` 检查活动 backend 的 `LOCALAPPDATA`、本机 `desktop-linux` context 和 D 盘文件，不能用一个旧 D 盘副本冒充当前存储。Windows 验证路径针对本机 64 位 Python / Docker Desktop；无法核实就拒绝启动部署操作。

Runtime 与独立 worker 都能采集和补传。SQLite 游标、稳定 observation/score ID、分析任务租约与分段缓存支持恢复。主任务不等待 Langfuse 写入或模型分析；停机时保留待补状态。独立 worker 使用同一模型配置的新 client，不继承 Agent 记忆或工具。

当前仓库的既有 Runtime 已登记到独立 worker，并按启用时间排除旧任务。已经运行的 cc-harness 后端需要正常重启一次，才能加载新增的 provider 输入采集；在此之前仅能从其已提交事实还原可见证据，缺失输入会明确标记，不能算完整分析。原有任务事实库未自动迁移。

模型请求的脱敏副本包含实际 provider 输入；完整可见事件及 offload 原文按内容摘要核对后展开。隐藏思考与凭据不复制到观测系统。长轨迹按步骤边界分段，单个超大事件明确标出片段，再进行分层全局复核。资产缺失、任意段失败或全局复核不足时保持不完整。分析调用用单独的 `analysis.<run_id>` 记录用量，提供方未报告的费用保持未知。

模型服务恢复后，可以等待后台退避结束，或显式触发待补分析：

```powershell
python -m cc_harness.observability.worker --retry-now --status
```

后台 worker 会继续处理已排队工作。没有运行后台 worker 时可用 `--once` 处理一轮，或不加该参数持续运行。

## 评测和发布门禁

当前候选版本为 **1.1.2**，包含 30 条 Golden 候选和 12 条流程保留任务。两组 Dataset 分开，保留集不会在每次推送时运行。每次任务使用新的原生项目目录；函数及机制的独立 oracle 在无网络、只读挂载候选源码的 Docker 容器中执行。可信父进程保留预期结果，候选以独立低权限子进程运行，不能靠打印一个 `passed=true` 伪造整体判定。Agent 本身运行在一次性本机项目中，并非完整 OS 沙箱；隔离类型在结果和 core manifest 中如实记录。

2026-10-26 按用户最新要求，评测范围仅为这 42 条 Agent 任务。仓库 pytest 套件不再由观测评测命令或本次安装的提交钩子触发，也不作为 Agent 发布判定的前置条件；普通单元测试文件不再进入 Agent 执行身份快照。Langfuse 核查未发现仓库 pytest 用例导入，当前版本 Dataset 实际为 Golden 30 条、holdout 12 条，核查记录在 D 盘 `deployment/agent-only-evaluation-audit.json`。任务内部的独立验收与轨迹证据继续保留。

```powershell
python -m eval.observability.runner prepare
python -m eval.observability.runner publish

# 日常完整回归：30 条 Golden，随后检查已批准基线
python -m eval.observability.gates full --experiment daily-001

# 阶段/发布候选：显式加入 12 条保留任务
python -m eval.observability.gates release --experiment release-001

# 单条真实任务；每次用新的 experiment 名称
python -m eval.observability.runner case --task outcome.01 --experiment smoke-001

# 服务故障后的实验关联补传
python -m eval.observability.runner publish-results --experiment release-001
```

每次运行保留初始夹具、保护文件摘要、事件、独立结果、用量与控制动作。产品与 Agent 评测器代码（含未跟踪 Python 文件）、依赖、有效环境、层叠模型/MCP/policy 配置与数据集版本均绑定到执行身份；快照不包含凭据明文。代码或配置变化后旧证据不能放行。

`normalize` 将现有结果导入 `eval/core`，结果变化时保留新的不可变证据修订；`check` 验证 core 判定内容摘要及当前身份。失败、环境无效、必要证据缺失都不放行；模型疑似判断不能代替独立结果。

服务端为 v4，实验通过带 experiment 属性的 OTLP 根步骤关联原任务轨迹；不使用退役的 Dataset Run Item 写入口。实验关联和独立结果评分分别使用稳定身份，补传不会创建新的 Agent attempt。旧版 42 次尝试中有完整结果的 41 条可关联实验，另 1 条缺少完整结果的尝试保留为无效证据。

```powershell
python -m eval.observability.gates normalize --experiment release-001 --include-holdout
python -m eval.observability.gates check --experiment release-001
```

首次基线必须有完整有效的通过结果，再由审查者明确批准；程序不会把首次尝试或上一次运行自动设为基线：

```powershell
python -m eval.observability.gates approve-baseline --experiment release-001 --reviewer YOUR_NAME
```

默认 `pre-push` 运行本地 Ruff 和核心回归，关闭真实 LLM 与观测服务，不进行付费 Agent 评估。既有用户 hook 保留。更新本地 hook：

```powershell
python scripts/install_observability_hooks.py
```

需要每次推送都运行 30 条 Golden Agent 任务时，显式使用 `python scripts/install_observability_hooks.py --agent-eval-on-push`，该模式会调用配置的模型并产生费用。手动 `gates full` 仍运行 Golden；阶段或发布命令运行全部 42 条。这些付费评估不会被本地回归替代为“已通过”，基线审批规则保持独立。

显式发布包装器会再次校验证据，只有通过才执行后面的发布命令：

```powershell
python -m eval.observability.gates publish --experiment release-001 --command YOUR_RELEASE_COMMAND
```

本轮没有执行远程推送或实际发布。项目没有新增可绕过门禁的自动发布工作流。

## 验收记录与未完成项

已核实：七个服务启动；实际观测和 Dataset API 可读；同一受控 Trace 同时记录工具已确认问题与上下文待核实线索；D 盘 backend 与数据盘写入；六个持久卷冷备份、逐文件恢复比较以及最新恢复后的 Postgres 读取四个版本化 Dataset；原生评分“确认 → 驳回”的两次修订、当前投影与原模型意见均保留；离线队列重启补传、重复幂等、凭据脱敏、模型证据引用、覆盖状态和 oracle 正反例有自动测试。

完整旧版试跑 `candidate-20261025-v1` 共尝试 42 条：**12 条任务失败，30 条无效**。无效中 29 条为提供方 `provider_billing_error`（HTTP 402），另 1 条为初版采集器故障未产出完整结果。原始 `result.json` 与修正前分类保留，环境重分类记录为新文件，Langfuse 评分按稳定 ID 修正。该开发过程跨了多个代码版本，不能作为发布基线，也不能当作当前修正版本的通过率。

2026-10-26 09:25（Asia/Singapore）的独立短请求仍返回 HTTP 402。用户充值后，10:09 的短请求成功，已解除分析退避并恢复真实任务与待补分析。试跑发现并修正了以下接入边界，生产完成门禁和独立评分标准没有放宽：

- 公共测试命令后的英文说明被完成契约解析器吸入命令。现在命令单独成句，并用全部 42 项任务调用生产解析器验证精确匹配。
- 审批拒绝任务曾误把被拒绝的目标列为必需产物；增量构建任务曾误把模板文件名列为必需产物。任务措辞已修正，并增加生产产物契约回归。
- 输入采集包装器的方法名应为 Runtime 实际调用的 `execute_segment`。已修正，并验证新旧任务时间边界、跨线程上下文、请求资产与索引、回调透传和异常恢复。修正后的真实失败请求已保存在 D 盘，轨迹含完整脱敏请求，未报告缺失输入。

`acceptance-v110-20261026-0210` 的 8 份原始结果、`pilot-v111-20261026-0220` 的试跑及各中断记录均保留。`acceptance-v112-20261026-0230` 在输入采集修正前产生 **1 条通过、10 条因提供方欠费无效**，随后停止；这些记录不代表当前最终源码的完整验收。

10:33 的独立短请求再次返回 HTTP 402。10:34 查询同一 API Key 对应的官方账户余额接口，返回 `is_available=false`、CNY `-0.06`、USD `0.00`。最新健康及余额记录位于 D 盘 `deployment/provider-health.json`、`provider-health-history.jsonl` 和 `provider-balance.json`。修正输入采集后的 `pilot-capture-20261026-0233` 也因欠费无效；分析已进入一小时退避，原始证据和待补任务保留。

因此 **当前最终源码下 v1.1.2 的完整 42 条真实试跑、完整语义分析验收及基线审查仍未完成**。已完成的旧轨迹分析中，缺少源资产或有弃权的结果明确标记为不完整。暂不宣称评测集已冻结或发布验证已通过。服务恢复后使用新的 experiment，先确认实际请求资产可读，再运行全套评测；不要复用上述批次作为通过基线。

本轮修正提交为 `a86dd06`。输入补传后已在 Langfuse 的实际 generation 中核实 34697 字节输入，证据记录为 `deployment/provider-capture-verification.json`。本轮冷备份 `backups/20261026T024115Z` 的恢复抽检通过：六个卷全部恢复并逐文件比较，隔离 Postgres 可读，包含八个版本化 Dataset；恢复结果保存在该目录的 `restore-drill.json`。

此前仓库工程测试日志仅在 D 盘本地保存，未导入 Langfuse，不计入 42 条 Agent 任务。用户已明确取消这部分测试执行及其门禁依赖。历史日志和旧 `quick.json` 只保留作历史记录，不作为当前 Agent 验收条件。

## 备份与恢复抽检

```powershell
python scripts/manage_langfuse.py backup
python scripts/manage_langfuse.py restore-test --backup D:\agent_data\cc-harness-observability\backups\BACKUP_NAME
```

冷备份会短暂停止 Langfuse 写入，保存所有卷、配置、SQLite 队列快照、脱敏资产和已完成评测结果，随后恢复服务。运行中的 Agent 工作目录不作为完整一致快照备份。恢复抽检创建独立临时卷，校验归档摘要、比较恢复文件并启动独立 Postgres 查询，不覆盖活动卷。备份与活动数据同在 D 盘，不具备磁盘损坏后的异地恢复能力。

设计依据：[总体设计](design/2026-10-25-langfuse-observability-evaluation.md)、[评测集设计](design/2026-10-25-langfuse-evaluation-dataset.md)、[ADR-0119](adr/0119-use-langfuse-for-agent-observability-and-evaluation.md)。
