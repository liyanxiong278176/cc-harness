# 评测证据（原始汇总，非精选）

这里放的是评测运行产出的**机器汇总**，从本地完整结果目录中复制/裁剪而来，未经人工挑选。
完整结果（原始 trial、日志、状态机）体积很大，保留在本地 `eval/result/`，不提交到 Git；
复现方式见 [`docs/eval-methodology.md`](../eval-methodology.md)。

| 文件 | 来源运行 | 说明 |
| --- | --- | --- |
| `terminal-bench-2.1.summary.json` | `eval/result/cc-only/terminal-bench-2.1/deepseek-v4-flash/full-new-260905220633/summary.json` | 裁剪掉 docker/环境自检字段，保留 counts、逐类准确率、adaptations |
| `agentdojo-v1.2.2-balanced-500.summary.json` | 同路径 `portfolio/summary.json` | 原样复制；含攻击/良性分组、攻击成功率、utility |
| `../../eval/result/cc-only/locomo-memory/deepseek-v4-flash/full/report.md` | LoCoMo 1,986 QA | 该报告本身已在仓库内跟踪，未重复复制 |

## 口径注意事项

1. **模型**：全部为 `deepseek-v4-flash`（仓库默认配置的兼容模型），不是线上业务模型。
2. **Terminal-Bench 2.1**：Harbor 本地执行，89 个任务 × 5 trials；不是官方 leaderboard 提交
   （`leaderboard_compatible=false`，官方要求 ≥5 trials 且指定 agent 接口）。`successful_tasks=72`
   表示 89 个任务中至少一次通过，逐 trial 准确率见文件内 `benchmark_metrics`。
3. **AgentDojo**：冻结的 500-trial 子集，覆盖四个官方 suite 与 standard/hardened 双轨。
   `attack_success_rate=0.0` 是在**带权限闸门**的 agent 上取得的：注入触发的工具调用会进入
   ask/deny，而不是被模型自行拒绝。因此不能与"无人工审批"的 leaderboard 直接比较；
   同一次运行的工具效用（utility）见文件内分组数据。
4. **LoCoMo**：`official_deterministic_f1=0.606`（1,986 QA）；分层记忆在内部对照里低于
   "完整历史"注入（24/24 vs 20/24），这里只声称"省 token"，不声称"更准"。
5. 所有指标都是单机、单模型、可重复运行的结果，不构成线上 SLA 或跨模型泛化结论。
