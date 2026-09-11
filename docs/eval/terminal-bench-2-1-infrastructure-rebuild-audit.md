# Terminal-Bench 2.1 接入重建审计

日期：2026-09-01
范围：`eval/cc_only`、`eval/harbor`、`harbor_plugins`、`scripts` 及相关测试。
依据：官方 Harbor/Terminal-Bench 2.1 单任务协议、附件中的“预防→检测→恢复→清理”方案，以及项目当前冻结结果证据。

## 结论

当前接入已经具备“可审计的单任务执行器”，但还不是附件方案要求的“全维度基础设施防护层”。现有实现可以正确区分一部分 verifier 未执行的基础设施错误，也能保留检查点；不过防护逻辑分散在 Harbor adapter、runner 和 WSL supervisor 中，缺少统一的 guard 契约、任务前资源门禁、任务后幂等清理和统一基础设施事件账本。

因此本次重建采用**兼容式重建**：保留已验证的官方 Harbor adapter、冻结 catalog 和历史结果；新增 `eval/terminal_bench` 防护层，并从现有 runner/adapter 调用它。这样不会删除历史证据或改变官方 verifier，而是把正式评分路径收敛到官方数据集 + agent-runtime-only overlay。

## 证据快照

| 项目 | 当前证据 | 判定 |
| --- | --- | --- |
| 官方数据集 | `terminal-bench/terminal-bench-2-1@sha256:7d7bdc1cbedad549fc1140404bd4dc45e5fd0ea7c4186773687d177ad3a0699a` | 已固定 |
| Harbor | `0.20.0` | 已固定 |
| 任务数 | full profile 89 | 已校验 |
| 评分 authority | Harbor 官方 verifier | 已保留 |
| agent overlay | `CC_HARNESS_TERMINAL_AGENT_RUNTIME=1`，禁止 verifier overlay | 已有 fail-closed 审计 |
| checkpoint | `state.json` + 每次 attempt 证据目录 | 已有，可续跑 |
| verifier 未执行 | 记录为 `invalid`/pending，不写入官方 fail | 已有 |
| Docker 地址池 | 曾发生 `all predefined address pools...`，且旧 runner 曾继续启动后续任务 | 已修复 outer-loop pause，仍需统一 guard |
| 并发写入 | SQLite projection 曾出现 race/locked 证据 | 需要 WAL/串行化回归 |
| 费用 | token 可见，provider 费用可能 incomplete | 需要统一遥测契约 |
| 进程监督 | 默认经 WSL systemd supervisor | 与附件“直接前台运行”不一致 |

## 与附件方案的差距矩阵

| 维度 | 附件要求 | 当前实现 | 风险 | 本次处理 |
| --- | --- | --- | --- | --- |
| 统一 guard 契约 | 10 个 guard 都实现 `pre_check/prevent/recover/post_cleanup` | 只有 `infrastructure.py` 分类函数，实际检查散落在 adapter/host/preflight | 处理顺序和清理不一致 | 新增 `eval/terminal_bench/infra_guard` 基类与 10 个 guard；runner 统一编排 |
| Docker | daemon、context、存储、网络、容器、镜像、地址池检查与幂等清理 | 有 daemon 快速检查和前后快照；没有统一 prune/泄漏门禁 | 地址池/孤儿网络连环失败 | 任务前后 guard；地址池失败只保留同一 attempt 并暂停 |
| 网络 | DNS、PyPI、GitHub、API、代理、容器网络 | WSL 环境默认直连，adapter 只捕获 Harbor 错误 | stale Clash/容器网络错误可能延迟到任务中 | 只做非侵入式探测；不擅自改用户代理；失败记录为 infra |
| 内存 | WSL/交换/Docker/agent 资源监控与回收 | 没有统一内存 guard | 长跑资源积累 | 增加可观测阈值和幂等回收；不默认 drop_caches/杀进程 |
| 文件系统 | 磁盘、inode、临时目录、原子写入、锁 | 有原子 JSON 写入和路径压缩；缺少统一门禁 | 部分写入、磁盘满 | 新 guard + 原子写入探针 |
| 依赖 | uv/Python/Harbor/wheel/verifier 依赖 | `terminal_preflight` 有若干检查和 frozen bootstrap | 检查结果不能统一进入事件账本 | 统一 dependency/verifier guard；正式路径不动态覆盖 verifier |
| Harbor | 版本/catalog/超时/结果格式/串行 | adapter 已固定版本、串行、官方 dataset | 结果缺失与 launcher 错误处理分散 | Harbor guard + 结果完整性校验 |
| Agent | pid、空闲、资源、JSONL、子进程清理 | adapter 有 watchdog、activity snapshot、JSONL 解析 | stuck 与真实长任务边界易混 | 分级 liveness；不把“无 stdout”直接判 fail |
| Verifier | verifier 未执行不得算模型 fail | 已有 failure attribution，但仍需统一入口 | 误报会污染分母 | 统一 `verifier_executed`/official reward gate |
| API | 402 暂停、429/5xx 有界退避、超时 | provider proxy/runner 有部分处理 | 费用和重试边界不统一 | API guard + 不重放已开始模型阶段 |
| 进程 | fd、进程数、僵尸、fork | 没有统一 guard | 长跑泄漏 | 只读探针 + 事件；不杀非本运行进程 |
| Runner | 任务前总检、任务后清理、infra 暂停、断点续跑 | 已有 checkpoint 和 outer-loop pause | guard 不能统一阻断/恢复 | 新 orchestrator 接入；保留当前结果 schema 兼容 |
| 启动路径 | 官方 Harbor、串行、一次尝试、不改 verifier | 官方断言已存在，但 Windows launcher 默认走 supervisor | 额外监督层增加状态漂移 | 增加直接前台官方入口，评测时使用它；保留 supervisor 供显式重连 |

## 明确保留的边界

1. 不删除 `eval/result/` 历史结果，不把旧 `_superseded` 结果合并到新成绩。
2. 不把 guard 诊断、预热镜像或自定义 verifier 注入官方评分路径。
3. 不对已经开始模型调用的 attempt 自动重放；该 attempt 只保留证据并暂停。
4. 不在 guard 中无条件杀进程、重启 Docker、切换代理或执行破坏性清理；这些动作必须有明确匹配、范围校验和事件记录。
5. 基础设施重试预算保持有界；默认沿用项目已批准的“最多 10 次 pre-model transient 重试”，不对模型阶段重试。附件中的 3 次是较保守建议，作为可配置上限而非覆盖既有运行合同。

## 实施顺序

1. 先落地本审计与差距证据。
2. 修复已确认问题：基础设施错误暂停 outer loop、Docker 资源清理/地址池门禁、状态事件账本、SQLite 并发回归、费用/模型调用契约。
3. 新增兼容式 `eval/terminal_bench` guard 层和官方前台启动入口，运行无模型单元测试与官方协议静态审计。
4. 仅当所有门禁通过后，在同一个 89-task output root 启动一次正式评测；出现基础设施错误时保存证据、暂停、修复后从 checkpoint 续跑，不重跑已完成任务。
