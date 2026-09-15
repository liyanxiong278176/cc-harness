# WebUI 任务书执行报告

日期：2026-09-14  
执行目录：`D:\agent_learning\cc-harness`  
明确排除：`D:\agent_learning\外卖`（未读取、未修改、未启动任务）

## 交付矩阵

| 项目 | 结果 | 证据 |
|---|---|---|
| 任务书评审与当前架构校准 | 完成 | `docs/audits/web-ux-taskbook-review.md` |
| 差距、取舍和流式设计文档 | 完成 | `docs/audits/web-ux-gap.md`、`docs/audits/web-ux-decisions.md`、`docs/design/web-streaming.md` |
| Durable provider chunk 的进程内广播 | 完成 | `cc_harness/live_stream.py`、`durable_runtime.py`、`worker.py` |
| Worker 超时/取消/异常的流式终态 | 完成 | 缺少 provider `done` 时由 Worker 补发 `done`，广播仍为 best-effort |
| 旧 ModelAdapter/Kernel 兼容 | 完成 | 签名反射；旧两参数 adapter 测试通过 |
| SSE Durable 对账 + 易失增量 | 完成 | `webui.py`；HTTP/SSE 烟测先收到 `event: runtime` |
| 前端临时消息替换/停止冻结 | 完成 | `web/src/main.tsx`、`web/src/styles.css`；Vite 构建通过 |
| provider 私有 reasoning 脱敏 | 完成 | callback 只发送阶段/终态元数据；自动化测试验证无 `reasoning_content` |
| detached supervisor 跨进程 token 流 | 延期 | 进程内 hub 收不到另一个进程的增量，Durable 轮询继续兜底 |
| 文件树、@ 引用、完整轨迹页、虚拟列表 | 延期 | 当前公开 API 不足，不制造假入口或假数据 |

## 验证结果

执行过的命令：

```text
python -m compileall -q cc_harness                         PASS
ruff check <改动 Python 文件>                               PASS
npm run build（web）                                        PASS
pytest tests/runtime_rebuild tests/test_webui.py \
       tests/test_live_stream.py tests/test_web_sse.py      199 passed
```

本次在隔离临时目录做了两类验证：默认 HTTP/SSE 烟测不调用 provider；另一次在已有配置和明确授权下调用真实 DeepSeek，项目目录仍是临时目录，不是业务项目。

真实本地 HTTP/SSE（无 provider 请求、无费用）：

- `GET /api/health` → `200`，`runtime: durable`
- `GET /api/bootstrap` → `200`
- `GET /` → `200`，嵌入式首页 448 bytes
- `GET /api/sessions/{run_id}/events?after=0` → `200`，第一批为持久 `event: runtime` / `RunCreated`
- 服务由 Ctrl+C 停止，测试端口 3181 已释放

Playwright 浏览器冒烟（1600×1000）记录：会话切换时 `session-loading` 可见且发送按钮禁用；Durable 对账完成后加载态消失；选择一个 `failed_recoverable` 历史会话时右侧 `diagnosis-card` 可见；composer 始终可见，页面水平溢出为 `0`。截图仅保存于本机临时目录，未作为运行事实源。

完整 `pytest -q` 的唯一阻塞是仓库既有可选 benchmark 依赖未安装：`tests/test_agentdojo_adapter.py` 导入 `agentdojo` 时触发 `ModuleNotFoundError`。这不是本次改动失败；相关 Durable/WebUI 测试集已独立全绿。

## 用户可见变化

模型生成期间，WebUI 会在对话区显示临时增量和“正在思考/工具调用中”等阶段状态；持久 `AssistantMessageCommitted` 到达后，临时节点被权威正文替换。浏览器断线、SSE 丢包、停止、取消或 provider 异常不会把临时内容当成已完成事实；重新连接仍以 Durable timeline 为准。慢订阅者会收到 `stream_gap`，不会反向阻塞 worker。

会话切换现在有明确的加载中间态：选中项立即更新，消息区显示“正在载入会话”，请求完成前禁用发送；过期响应不能覆盖后来选中的会话。短且无代码/工具意图的输入使用独立的 `conversation` 完成合约，仍必须有已落盘的助手消息；编码任务继续要求验证/工具证据。右侧运行面板集中显示投影不一致、缺少完成证据、未知副作用、租约冲突和环境降级的安全中文诊断，聊天区不泄露 completion JSON、traceback、原始 reasoning 或密钥。

## 本轮问题与根因

| 现象 | 根因 | 修复与验证 |
|---|---|---|
| “你好”也进入 `stalled` | 所有输入都沿用编码任务的严格完成门；模型只给自然语言时没有工具/验证证据 | 持久化 `interaction_mode`；对话仍以已落盘 `AssistantMessageCommitted` 生成证据，真实 DeepSeek 对话与 follow-up 均完成 |
| 切换大历史会话像空白或发送无响应 | 三个历史请求并发返回，旧响应可覆盖新选择，且没有显式加载态 | 请求代次保护、加载状态和发送禁用；Playwright 验证加载态出现并在对账后消失 |
| 出错只看到“卡住”或原始错误 | Runtime 诊断停留在事件/日志层，聊天区没有安全投影 | 右侧 `diagnosis-card` 按错误类别给出原因/下一步；投影、完成证据、未知副作用、租约、沙箱以及模型连接/超时/限流均有专门诊断；Playwright 选择失败会话验证卡片出现 |
| 页面过长挤压输入框 | 消息流、输入框和诊断信息没有清晰的独立滚动边界 | 中间消息区滚动、composer 固定、右侧面板独立滚动；水平溢出检查为 `0` |

## 已知限制与后续建议

1. detached supervisor 与 WebUI 分进程时没有跨进程 token 广播，需未来增加认证的本机 IPC/Unix socket/命名管道协议；当前 Durable 对账保证正确性。
2. hub 历史和队列是内存有界缓存，重启后必然丢失临时 delta，这是设计约束而非数据丢失。
3. 本次真实 provider 验证仅覆盖最小中文对话和一个 follow-up；编码任务仍可能因为模型没有提交可验证 candidate 而保持 `stalled`，这是完成门的预期行为，不应改成“模型说完成即完成”。
