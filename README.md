# cc-harness

https://github.com/user-attachments/assets/b30e264d-889f-4b11-b173-1d9f7d392246

本地运行的 coding agent：在选定项目目录里读写文件、执行命令、跑测试，并在浏览器 WebUI 里对话与审阅。
核心是一条 **Durable Runtime**（事件溯源 + 租约 + 可恢复执行），外围是上下文压缩、分层记忆、权限闸门与输出 DLP。

> 定位：单用户、本地、可审计的编程代理。**不是**多租户沙箱，也**不是**"无人值守跑任意外部代码"的运行时。

## 三个可复核的数字

| 评测 | 结果 | 口径 |
| --- | --- | --- |
| Terminal-Bench 2.1 | 89 任务 × 5 trials，**72/89 任务至少一次通过**（80.9%） | Harbor 本地执行，非官方 leaderboard 提交 |
| AgentDojo v1.2.2（500 trials） | 攻击成功 **0/306**；工具效用 74.8%（受攻击组）/ 87.1%（良性组） | 冻结子集，**带权限闸门**，不能与无审批榜单直接比较 |
| LoCoMo（1,986 QA） | 官方确定性 F1 **0.606**；分层记忆比完整历史注入省 **57.3%** 输入 token | 准确率 20/24 vs 24/24：省 token，但**没有**更准 |

机器汇总见 [`docs/eval-evidence/`](docs/eval-evidence/README.md)，口径与失败样例见
[评测方法](docs/eval-methodology.md)。所有结果均为单机、单模型（`deepseek-v4-flash`）可重复运行。

## 架构

![cc-harness 动态架构图](docs/architecture/cc-harness-animated/cc-harness.gif)

## 快速开始

要求 Python 3.11+。

```powershell
python -m pip install -e .
cc-harness                     # 启动 WebUI（默认 http://127.0.0.1:3080/）
cc-harness --no-open --port 3080
```

首次运行需要 `.env`（`OPENAI_API_KEY` / `OPENAI_BASE_URL` / `OPENAI_MODEL`）。
模型与执行后端的选择写在 `policy.yaml`，默认执行后端是 OpenSandbox，Docker 不可用时按 profile 决定是否降级
（`standard` 降级到 host 并审计，`hardened` / `benchmark-one-shot` 直接失败）。
