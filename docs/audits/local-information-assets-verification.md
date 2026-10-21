# 本地信息能力补齐验证记录

日期：2026-10-21。

## 验证范围

本次实现面向单用户本地编码 Agent：结构化续行、压缩完整性、来源清单、记忆来源验证和级联遗忘、本地 Skill 固定版本、引用恢复、孤立对象清理，以及共用后端的 WebUI/命令入口。

## 已完成检查

| 检查 | 结果 |
| --- | --- |
| 新增 `tests/test_local_information_assets.py` | 14 项用例，覆盖本地 Skill、来源验证、遗忘并发、版本派生、恢复、重复压缩、预算不足、引用迁移及管理接口 |
| 最新针对性回归 | 66 passed；包含上述 14 项及上下文契约、引用回读、记忆并发、后台任务、维护、执行交互、事件、存储与快照兼容性 |
| 补充兼容性回归 | 64 passed；覆盖旧版卸载引用、上下文-记忆评测契约、进度记录、LoCoMo 历史保留与上下文投影 |
| 前期扩大模块回归 | 上下文、记忆、恢复及 WebUI 模块组通过；后续调整再用针对性回归验证 |
| Python 静态检查 | `python -m ruff check cc_harness tests/test_local_information_assets.py` 通过 |
| WebUI 构建 | TypeScript 检查和 Vite 构建通过，已同步到 Python 包内的静态资源 |
| 浏览器实际交互 | 在隔离临时项目中启动真实本地服务，用 Chromium 验证记忆展示与遗忘、Skill 启用、上下文入口、清理预览、Escape 关闭；无页面脚本错误 |
| 命令实际执行 | 对同一隔离项目运行 `--command assets`，读取到浏览器已完成的遗忘代次和 Skill 启用状态；未启动模型 |
| 差异空白检查 | `git diff --check` 通过 |

新增测试中的“真实压缩”指调用项目实际的压缩与持久化实现，摘要模型使用确定性离线替身；并不代表进行了真实模型质量或成本评测。

## 完整套件的已知阻碍

首次执行完整套件时，`tests/test_agentdojo_adapter.py` 因缺少可选依赖 `agentdojo` 而在收集阶段失败。

排除该文件后，扩大套件执行结果为：**2380 passed、33 failed、24 skipped**。该次扩大执行之后的新增/修正内容通过上面的最新针对性回归验证，没有将前一次扩大执行误记为最新代码全量通过。

将这 33 个失败节点全部放到独立提取的、未修改的 HEAD 版本 `eae345a98245c2c3be31b0609bef1cc589a61987` 中重新执行，结果同样为 **33 failed**。失败集中在既有旧版子代理/REPL、租约恢复状态、命令活性与中断恢复测试；本次没有修改这些既有契约来掩盖失败。

基线复测输出保存在本机临时目录：

```text
C:\Users\A2781\AppData\Local\Temp\cc-harness-head-check-tfj92584\baseline-all-failures.txt
C:\Users\A2781\AppData\Local\Temp\cc-harness-head-check-tfj92584\selected-baseline-nodes.json
```

浏览器验证截图与可复跑脚本保存在本次可视化目录：

```text
C:\Users\A2781\.codex\visualizations\2026\10\21\01a15c24-5b43-7331-ad39-28c9738da888\local-assets-skills.png
C:\Users\A2781\.codex\visualizations\2026\10\21\01a15c24-5b43-7331-ad39-28c9738da888\verify_local_assets_ui.py
```

## 验证边界

- 没有为本次验证调用付费模型或运行真实模型 benchmark，不能据此宣称模型成功率、压缩事实保留率或费用得到提升。
- 真实提供方、容器及受系统权限限制的测试维持原有门控；本记录不将跳过项算作通过。
- 级联遗忘保留独立治理的原始历史，无法撤回已经发送给在途模型的输入，也不声称清除外部备份或实现物理安全擦除。
