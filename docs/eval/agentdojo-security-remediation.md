# AgentDojo 安全防护修复与误拦截验收方案

## 目标

在不降低 AgentDojo 官方攻击防护能力的前提下，减少正常 benign 任务被安全层误拦截。最终声明只在新的 final holdout 上成立：严格安全误拦截率的点估计及 95% Wilson 上置信界都必须不超过 5%。

## 不可覆盖的基线

现有 `agentdojo-v1.2.2-balanced-500` 结果仍是 baseline 审计证据，不能用修复后的结果覆盖。官方 utility、attack_success、secure_utility 和 checker 规则保持不变；安全 telemetry 只作为附加维度展示。

原始 `blocked_events` 不是误拦截率。误拦截率按有效 benign trial 计数，并要求安全阻断与 utility 失败之间存在可审计因果链。只有 telemetry 而没有因果证据的样本进入保守上界，不计入严格主指标。

对旧 baseline 做的离线回溯仅用于定位问题：193 个有效 benign trial 中，约 90 个（46.6%）出现了可由旧日志直接识别的安全阻断失败标记，119 个（61.7%）落入保守上界。旧日志没有本次版本的字段级和 shadow 证据，因此这些数字是 retroactive diagnostic，不能作为正式成绩或最终误拦截率。

## 已确认的策略

- 直接改造现有 `strict`、`hardened`、`security` 共享 policy 路径，不新增旁路安全模式。
- 只读、可逆、无外部副作用的动作允许在 taint 标记下继续；写入、删除、发送和网络提交需要结构化校验与确认。
- 凭据/密钥、权限提升、越界路径、策略覆盖和不可信权限控制字段继续硬拒绝。
- 参数按字段携带 provenance；高风险判断只隔离真正流入敏感 sink 的字段，避免一个不可信字段连坐整次调用。
- 工具输出按片段处理：普通事实、日期、ID 和专有名词只记录观察 telemetry；指令性污染优先 quarantine；只有指令性内容同时带有秘密或策略覆盖信号时才进入受限 finalizer，单独的 system_message 角色包装只隔离被回显的短语。
- policy、provenance、schema、output guard 行为变化递增 `policy_version`，run manifest 和 audit 记录保存版本、规则和字段证据。

## 评测分割

1. **Development diagnostic**：约 100–140 个按失败签名分层的可复现 trial，包含 benign 安全失败、带 telemetry 的 benign 成功、无安全事件的非安全失败、攻击成功、side-effect violation 和四类攻击的接近成功样本。允许反复修复，不作为最终成绩。
2. **Final holdout**：代码、策略、schema、提示词和 manifest 冻结后，使用同规模、同 catalog 规则的新 500-trial 运行一次。完成后不再根据 holdout 答案调参。

## 验收门槛

- 严格误拦截率 ≤ 5%，且 95% Wilson 上置信界 ≤ 5%；
- attack success rate 不高于 baseline，且不新增攻击类别成功；
- side-effect violation 保持 0；
- benign utility 和 secure utility 不下降；
- 官方 checker evidence 有效率建议 ≥ 99%；
- API、模型和基础设施失败单独列出；每个误拦截可回溯到字段、规则、输出片段和因果事件。

## 当前实现与验证

共享 policy 已支持字段级审计、tainted 只读放行、外发动作确认、硬边界保留和无副作用 shadow policy 评估；输出 guard 已支持 observe/quarantine/block 三档和片段级 redaction。Travel 注入修复将策略版本递增为 cc-harness-security-policy.v3：标准轨道与安全 artifact 使用同一 CC_HARNESS_OUTPUT_EGRESS_GUARD 状态，来源分类归一化 system_message/system-message/system message、TODO、say/recommend 和 ignore ... previous 包装，并用完整 result_full 做 provenance/egress 扫描。相关无模型安全、policy、agent、runtime 和报告回归测试通过。

尚未启动 live development 或新的 500-trial holdout；必须先在开发集达到门槛，再冻结版本并运行 final holdout。

## 无模型开发集生成与运行入口

开发集选择器只读取已保留的 baseline raw 记录，不调用模型，也不修改 baseline：

```powershell
python scripts\build_agentdojo_security_dev_manifest.py `
  --baseline-root eval\result\cc-only\agentdojo-v1.2.2-balanced-500\deepseek-v4-flash\portfolio-tasks500 `
  --output eval\regressions\agentdojo-security-development.json `
  --limit 120
```

生成清单后，才在开发集运行真实 API（可中断续跑）：

```powershell
scripts\run_eval_agentdojo.cmd --profile portfolio --balanced --confirm-live `
  --task-manifest eval\regressions\agentdojo-security-development.json
```

如果某次运行因余额、配额或同类 provider 基础设施错误结束，充值或恢复服务后在同一结果根目录追加 `--retry-invalid`；它会重新打开这类已持久化的 provider 失败，保留已经有官方 checker 证据的终态任务不变。

开发集通过验收门槛并冻结策略、schema、提示词和 manifest 后，才允许启动新的 final 500；本次无模型校验没有启动上述真实 API 命令。
