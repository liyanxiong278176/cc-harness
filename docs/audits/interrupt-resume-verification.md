# 中断续跑与租约冲突验证

## 结论

2026-09-14 在临时项目目录执行了真实 provider 的中断续跑冒烟测试。第一次真实模型响应返回后、Runtime 写入模型结果前注入 `Ctrl+C`，随后通过同一个 `run_id` 执行继续。运行最终完成，没有创建第二个根 Run。

| 检查项 | 结果 |
| --- | --- |
| 第一次 provider 调用 | 已返回，随后在提交边界中断 |
| 中断状态 | `cancel_requested` → `cancelled` |
| 续跑方式 | `RunResumed`，复用原 `run_id`/检查点 |
| 最终状态 | `completed` |
| 根 Run 数量 | 1 |
| 事件计数 | `RunCreated=1`、`InterruptRequested=1`、`RunCancelled=1`、`RunResumed=1`、`CompletionAccepted=1` |
| 模型调用计数 | 2（中断前结果未落盘时，续跑会重新进入模型边界） |

测试使用 `deepseek-v4-flash`，项目目录为一次性临时目录，不是业务项目目录；没有把 API key 或模型响应写入此文档。

为把本次实验的关注点限定在“中断边界与同一 Run 恢复”，测试 wrapper 只在第二次真实响应没有提供可验证的 completion candidate 时补入固定的测试 candidate；这不是伪造 provider 调用，也不代表模型原文一定包含该协议标记。生产路径仍由 Runtime 的完成门校验模型 candidate、证据和验收条件。

## 本次修复

1. `run_record.projection_digest` 被进程崩溃或旧 Projection schema 留下旧值时，只要事件序号与不可变事件流一致，`RunStore.load_projection()` 会从事件重建并原子修复派生游标。事件序号不一致、事件无法解析或快照字节摘要不一致仍然 fail-closed。
2. 多窗口/多进程竞争项目 supervisor 租约时，WebUI 将当前进程降级为 control plane，消息、审批和 `RunResumed` 仍写入 Durable Runtime，由租约持有者消费；不再把正常的领导权竞争返回 HTTP 500。
   如果原调度器已崩溃，当前进程会在内存中有界等待权威租约 TTL/释放，再尝试一次接管；租约仍由其他活跃进程持有时不会反复初始化 provider 或重复调度。
3. 右侧 Runtime 面板新增“调度器”状态：`本窗口运行中`、`其他窗口运行中`、`等待启动`，让租约归属可见。
4. stale session id 改为 HTTP 404；前端清除失效选择，停止重复轮询 `/api/sessions/{id}`。

## 回归命令

```text
python -m pytest -q tests/test_webui.py tests/runtime_rebuild/test_projection_compatibility.py tests/test_interrupt_resume.py tests/runtime_rebuild/test_three_layer_leases.py
npm run build                 # web/
python scripts/build_webui.py
```

以上回归测试通过；仅存在既有的 requests/httpx 依赖告警。WebUI 验证服务使用临时端口，端口不是运行时契约的一部分。
