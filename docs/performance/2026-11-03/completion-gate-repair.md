# 完成判定与推送评估修复

## 原因与修改

1. Windows 上的实际检查常以 `; exit $LASTEXITCODE` 结束。原先按命令全文散列匹配任务契约，导致真实通过的 pytest 无法满足指定检查。现在仅将这个安全的退出码传递后缀规范化；`exit 0`、输出包装、管道及其他检查仍不等价。
2. 项目内验证脚本从直接执行改为 `python -m` 后，旧失败未被相应的成功记录解除。现在解析同一个工作区脚本的身份，保留目录范围约束；不将不相关验证或未知动作视为已修复。
3. 修复提示现在列出任务要求的具体检查命令。
4. 评估控制器原先把初始目标确认消耗为断流恢复机会。冻结任务在提交时明确确认目标，断流恢复必须发生在故障注入之后；轨迹检查额外要求恢复事件晚于故障事件。工具审批与拒绝仍按原流程执行。
5. 独立函数检查原先直接导入只读挂载中的候选文件，使合法的模块相对制品写入失败。现在由非特权候选子进程把 `solution.py` 复制到临时目录执行。原项目仍只读挂载，独立父进程保留隐藏探针与裁决；新增测试验证原文件和宿主制品不被改写。
6. 数据集更新到 `1.1.3`：夹取任务明确要求标量返回值；清单导出的公开检查要求制品与交付结果一致，不提供隐藏答案。外部正确性探针保持原标准。

## 已完成验证

- 第一轮完整测试：2501 通过、26 跳过、0 失败。
- 最终相关回归：251 通过、0 跳过、0 失败。
- 仅含本次提交内容的独立工作树：47 通过、0 跳过、0 失败。
- Ruff 检查：通过。
- 原 `outcome.02` 从 `stalled`、18 次模型调用、121.865 秒，变为两次实际复测均 `completed`（15 次/48.036 秒；9 次/37.073 秒）。模型采样与服务延迟会变化，这不是稳定性能提升比例。

完整真实评估的最终结果另行记录。只有 Golden 与 Holdout 全部通过才允许批准首次基线。

## 证据与复现

原失败：`D:/agent_data/cc-harness-observability/evaluation/runs/release-20261103T070252Z/`。

原数据集下的复测：`completion-fix-probe-20261103`、`completion-fix-all-20261103`。后者在发现新问题后停止，不能作为完整通过证据。

最终评估：`D:/agent_data/cc-harness-observability/evaluation/runs/completion-fix-final-20261103/`。

测试 XML、日志：`D:/agent_data/cc-harness-repair/` 下的 `all-tests.xml`、`final-regression.xml`、`final-clean-tree.xml` 及对应日志。

```powershell
python -m eval.observability.gates release --experiment <new-experiment> --source D:/agent_learning/cc-harness
```

评估绑定当前工作树的源文件散列；完整测试包含工作区已有修改。独立工作树验证单独确认了本次修复不依赖其他未提交改动。源码、配置或数据集版本变化后应重新运行评估，不能复用旧裁决。
