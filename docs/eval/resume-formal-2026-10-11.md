# cc-harness 正式扩测报告（2026-10-11）

## 结论

完成 100 个不同编程任务的 600 次完整 Agent trial、100 段构造历史的 400 个查询/3,600 次真实模型回答、10 个中断边界的 200 次真实进程故障注入，以及 300 个攻击与 300 个正常组件用例。全部原始记录保留，无重复 trial ID。

分层记忆相较仅近期历史的准确率由 16.5% 提升至 42.0%，输入 Token 下降 57.3%；完整历史准确率为 100%，因此不能声称分层记忆提高了完整历史条件下的准确率。上下文压缩整体完成率由 91.3% 降至 28.0%；虽然总 Token 下降 42.8%，失败后的提前停止会减少用量，不能把该整体减少值写作有效效率提升。预先分配的 Snip 子组表现单独见下表。

这些是内部构造场景的数据，尚未使用真实用户需求、线上会话或生产告警，不能表述为实际业务效果。编程样例主要为短规则与算法修复，含两个模块、真实文件读取/修改和独立测试；未覆盖大型真实仓库、模型补充测试文件、复杂长期需求或容器服务任务。

工具集合仅包含 Read、Write 和固定验证命令；未评测完整生产工具集合下的恢复手段。此次没有测最大可承载会话轮数、最长任务链路或默认窗口容量，不能填入这类简历数值。

## 编程与上下文

使用生产 RunStore、RunCoordinator、ReActKernel、DurableModelAdapter、RunWorker、AgentCapabilityRuntime 和 MutationEngine。模型调用 Read、Write、固定 run_command，真实改动两个 Python 模块；外部验证器不可由模型修改。通过标准为运行时 completed、两个模块实际发生改动且独立检查通过。没有注入自动通过的 completion verifier。

本地 run_command 适配器将进程实际执行记录为 succeeded 工具观察，exit_code 和测试结果原样保留，并由外部验证器决定最终通过。Runtime completed 本身不保证断言通过；出现这种不一致时仍判失败。本次不据此验证完整生产命令适配器的错误恢复行为。

100 个任务分布：数字规则 24、字符串处理 24、列表算法 20、映射规则 16、设备预约区间 16。每条件每题 3 次，工作目录和 SQLite 状态每 trial 重置；两个条件顺序按固定种子打乱。验证器共 1,436 条单任务检查，全部参考实现通过，100 个初始错误实现均失败。判断比较 Python/JSON 值，未对所有标量强制类型等价检查。

为在有限工具输出下覆盖压缩压力，实验窗口固定 128,000 Token；三档阈值按任务编号余数预先分配为 (0.08,0.20,0.30)、(0.035,0.08,0.20)、(0.035,0.055,0.08)，保护区 512 Token，输出预留 2,500 Token。生产默认 60%/80%/95% 阈值没有在本实验中验证。控制组使用同样的任务、预算和阈值，仅关闭压缩。实际 provider 安全系数会自适应，以事件中记录为准。最多 12 次模型请求（包含摘要）、3 个调度 segment、210 秒/trial；达到上限或仍 queued 都算失败。

210 秒是 asyncio 配置的取消期限，运行时取消处理和关闭存储会使部分 trial 超出该时间，不是严格的进程强杀上限。开启条件共有 6 次超出，最长 244.9 秒；原始 elapsed_ms 保留。本报告不声称任务耗时具有 210 秒硬保证。

| 预先分配压缩档 | 不同任务 | 关闭压缩通过 | 开启压缩通过 | 关闭总 Token | 开启总 Token（含摘要） |
| --- | ---: | ---: | ---: | ---: | ---: |
| snip | 34 | 90/102 | 84/102 | 2,295,183 | 1,050,607 |
| prune | 34 | 98/102 | 0/102 | 2,313,992 | 1,114,833 |
| summarize | 32 | 86/96 | 0/96 | 2,170,467 | 1,712,396 |

总体：关闭 274/300，开启 84/300；输入 Token 分别 6,656,532 / 3,761,105；全部 Token 分别 6,779,642 / 3,877,836；摘要辅助调用 96 次。生产完成状态：{"queued": 214, "completed": 86}。Snip 子组全部开销减少 54.2%，此值仅适用于表中预先分配的 Snip 样例。不能扩展到全部三级机制或生产使用。

按不同任务聚合三次重复并对任务重采样 5,000 次：完成率差（开启−关闭）为 -63.3%，经验 95% 区间 [-72.3%, -54.0%]。同题重复不视作独立任务。成功配对子集的省 Token 结果存在成功条件选择偏差，仅用于诊断。

Prune 的实际失败轨迹中，模型明确回复“README content was cleared from my context”，随后重复 Read；调度去重与投影压缩后仍未完成。Summary 条件也出现把任务重新当作待读取规格的情况。失败样例及全部工具事件保存在 coding 子目录。正式运行期间未根据这些结果更改压缩算法。

## 跨会话记忆

冻结 100 段构造历史，每段 4 个不同查询，覆盖画像 60、场景 80、原子事实 120、多步关联 80、无答案 40、更新/冲突 20。三个条件各重复 3 次，真实 provider 回答并以明确预期值判定。使用真实 MemoryStore/sqlite-vec、MemoryRetriever 和 progressive layered_recall；L1 事实与 L2/L3 摘要预先写入，未测自动抽取质量。Embedding 是本地 256 维位置敏感词法 hash，未调用语义 Embedding 服务。全库包含 100 段历史，召回使用 session_id 隔离条件。

| 条件 | 正确回答 | 准确率 | 输入 Token | 总 Token |
| --- | ---: | ---: | ---: | ---: |
| 仅近期历史 | 198/1200 | 16.5% | 241,131 | 243,621 |
| 完整历史 | 1200/1200 | 100.0% | 1,330,215 | 1,333,887 |
| 分层记忆 | 504/1200 | 42.0% | 103,008 | 105,234 |

| 查询类型 | 完整历史 | 仅近期历史 | 分层记忆 |
| --- | ---: | ---: | ---: |
| L3 | 180/180 | 18/180 | 180/180 |
| L2 | 240/240 | 21/240 | 201/240 |
| L1 | 360/360 | 21/360 | 0/360 |
| multihop | 240/240 | 15/240 | 0/240 |
| unknown | 120/120 | 120/120 | 120/120 |
| update | 60/60 | 3/60 | 3/60 |

分层−近期历史提升 25.5 个百分点，按会话聚合的经验 95% Bootstrap 区间为 21.0–30.5 个百分点；分层准确率区间 37.5%–46.5%。含 40 个无答案查询，正确 UNKNOWN 也计入总准确率；去除无答案后分层为 384/1,080（35.6%），近期历史为 78/1,080（7.2%）。完整历史相对分层准确率明显更高，输入少不等于检索有效。

L3/L2 词项或子串匹配会提前终止检索；无关的高层片段也能命中自然问题中的普通词。进入 L1 的样例仍发生空召回，本地位置敏感词法 Embedding 与全库候选截断后再按会话过滤是需继续排查的限制。报告保留原始 query、历史、召回层、返回内容、期望答案、模型输出；正式回答完成后未重新改答案或修检索后替换结果。

## 状态恢复与安全

200 次子进程均以 os._exit(73) 异常结束，没有执行 SQLite close/finally。边界分别是 queued 事件、worker 租约、模型调用开始、模型返回尚未提交、assistant 提交、动作 planned、prepared、外部效果完成尚未 observation 提交、observation 提交、checkpoint 投影，各 20 次，payload 和任务 ID 不同。模型返回边界使用明确模拟结果，检验持久化边界，不计为真实 LLM trial。

已提交投影恢复：200/200。状态加载中位数 11.9 ms，包含打开存储和读取投影，排除 Python 进程启动、重新调模型及任务重做。20 个效果已执行但尚未记账的动作仍为 started，已执行的本地效果文件保留；不把它们误报为 completed。20 次租约过期后旧 worker 提交均被 fence 拒绝。此项没有验证恢复后最终任务完成、分布式网络分区或跨重启 exactly-once。内存控制字段是持久化缺失的分析性对照，没有跑不持久化版本的完整 Agent。

300 攻击 + 300 正常由权限 100+100、DLP 200+200 组成；均为组件评测，没有执行攻击动作，也未进行完整 Agent 的 Prompt Injection 成功率测量。

- 权限：攻击输入硬拒绝 70/100，其余 30/100 转人工确认，自动放行 0/100；正常 Read 为 100/100 转人工确认、硬拒绝 0/100、自动放行 0/100。确认请求不能算自动通过，也不能等同于攻击已执行。
- DLP：原文密钥格式样例 108/120 脱敏；24 字符 glpat 变体 0/12，当前规则限定 20 字符。Base64/字符间空格/换行/Unicode escape 共 0/40 完整隐藏；PII 共 0/40。此环境 presidio_analyzer、presidio_anonymizer、spaCy 缺失，pii_active=False；不能声称本机启用了 PII 脱敏。
- 正常 DLP 文本 200/200 未被改写。构造模板存在相关性，不能将 200 个相似变体看作 200 个代表性业务输入，也不据此推断低生产泄露率。

## 校准、代码变更与复现

扩测前修复两处运行问题：引用字段 scenario_ids/atom_ids 不再命中高层回答；service.py 等文件名不再触发在线服务健康检查。正式结果使用新冻结集，旧 pilot 及所有失败校准目录保留。正向校准任务 code-000 曾用于检查工具适配与完成判定，不能称整个 100 题完全未见的留出集。

初次上下文启动中阈值参数名被配置模型忽略，控制条件未生效，已停止并标记为 excluded-coding-run.json；该批未计入任何正式比较。snake 的原参考表达式误把反向引用解释为控制字符，其 6 次 trial 全部作为无效 oracle 保留并剔除，使用新的 path-components 任务补足 100 题；正式编码原始记录共 606 条，有效 600 条，规则替换见 coding-replacements.json。补充的 100 条手工计算规则示例检查通过。第 1 阶段个别 Unicode 验证输出按 UTF-8 解码 GBK 产生替换字符，原始输入/检查 JSON 保持完整；第 2 阶段验证器显式设置 UTF-8 输出，不改变判断值。

正式编程证据独立存于 D:/agent_learning/cc-harness/eval/result/resume-formal-2026-10-11-coding，记忆和工程证据存于 D:/agent_learning/cc-harness/eval/result/resume-formal-2026-10-11。原执行脚本保留在 frozen-runner / frozen-runner-stage2；第二阶段增加替换用例读取、修正参考表达式与验证日志编码，压缩算法、预算和其余用例的判断标准保持一致。第二阶段并发为 16，第一阶段为 8；不据此比较绝对延迟。

运行时/故障回归 246 项（245 通过，1 个独立 provider probe 跳过），上下文/完成门回归 96 项全部通过。正式评测本身实际调用模型，跳过的 probe 不代表正式集没有真实调用。各模块版本哈希校验：{"memory_module_hashes_match": true, "engineering_module_hashes_match": true, "coding_module_hashes_match": true}。

实际 provider 响应模型 {"deepseek-flash": 5741}，配置模型 deepseek-v4-flash，temperature=0，thinking=disabled。正式集与上述无效 oracle 的 API 尝试共 5,741，其中无效 oracle 尝试 24，失败尝试 0，有 usage 回执的全部 Token 为 12,461,621。条件比较的 Token 表已剔除无效 oracle；整体费用记录保留。Token 不折算金额，也不把调用次数当独立任务数。

对这份固定构造集的 Bootstrap 是经验稳定性描述；全成功数据会产生退化区间 [100%,100%]，不代表总体必然成功。更多样例不替代真实业务代表性，统计口径及资料见 [样本量建议](resume-ablation-sample-size-2026-10-11.md)。

复现本次数据需先把冻结的 memory-dataset.json、security-dataset.json、coding-dataset.json 和 coding-replacements.json 分别复制到新的对应输出目录；空目录会生成当前代码版本的默认题集，不能视作本次同一冻结集。

```powershell
python scripts/resume_formal_suite.py --phase memory --output <新的输出目录> --concurrency 8
python scripts/resume_formal_suite.py --phase engineering --output <同一输出目录>
python scripts/resume_formal_suite.py --phase coding --output <独立编程输出目录> --concurrency 8 --limit 300
python scripts/resume_formal_suite.py --phase coding --output <独立编程输出目录> --concurrency 8
python scripts/report_resume_formal.py --base <记忆与工程目录> --coding <编程目录> --write-report
```

脚本恢复时跳过已记录的 terminal trial，失败不会被自动挑出重跑。字段模型预算失败、queued 或未通过判据都保留，不以“基础设施问题”抹去。
