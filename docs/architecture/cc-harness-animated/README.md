# cc-harness 动态架构图

基于当前代码与 Runtime 文档绘制。图中主链路为：用户/工作区输入 → 目标入队与状态准备 → Supervisor/Worker 持租执行 → ReActKernel/LLM 推理与工具调用 → 按任务契约验收 → 交付结果。

## 文件

- `cc-harness.gif`：动态预览，1210×1138，20 fps，41 帧，循环时长 2.05 秒。
- `cc-harness.png`：静态预览。
- `cc-harness.excalidraw`：可编辑的矢量源文件。
- `spec.json`：内容配置，可修改标题、模块和说明。
- `render.py`：本图使用的本地渲染器。
- `verification.json`、`media-info.json`：格式、帧差和媒体参数检查结果。

## 阅读方式

1. 上方四个入口汇入同一条 Durable Runtime 执行链路。
2. 蓝色主区域概括准备、调度与推理。模型动作先经过策略判断，工具观察回流后进入后续推理与验证；证据不足时继续循环。
3. 左下为上下文压缩与 L1–L2–L3 分层记忆。自动记忆注入到 L3，其余层按需召回。
4. 中下展示 RunStore 的事件、投影、检查点与续跑关系。事件保存权威事实；投影重建当前状态，检查点按需加速恢复，制品保存观察与验证材料。未知动作需核对后推进。
5. 右下概括 Policy 权限判断、Native/MCP 工具派发和项目工作区操作。命令执行按配置选择 OpenSandbox 或本机；审批和输出保护保持在统一 Runtime 控制路径中。

图为架构概览，模块间箭头表达主要数据流与支撑关系。具体事件、审批和恢复状态机见 `docs/agent-runtime-full-flow.md`。

## 核对来源

- `README.md`：项目定位和唯一执行链路。
- `docs/agent-runtime-full-flow.md`：任务编排、Worker、验收和恢复。
- `cc_harness/durable_runtime.py`、`coordinator.py`、`supervisor.py`、`worker.py`、`run_kernel.py`：执行主链路。
- `cc_harness/capability_runtime.py`：上下文及 L3 记忆注入。
- `cc_harness/run_store.py`、`run_projection.py`、`artifacts.py`：持久事件、投影、快照与制品。
- `cc_harness/policy.py`、`executor.py`、`sandbox.py`：权限与配置执行器。

## 重新生成

在仓库根目录运行，需要 Python 和 Pillow：

```powershell
python -X utf8 docs/architecture/cc-harness-animated/render.py --spec docs/architecture/cc-harness-animated/spec.json --outdir docs/architecture/cc-harness-animated --basename cc-harness --verify --check
```

渲染器来自 `cclank/lanshu-animated-architecture-diagram`，上游版本 `c17f5b4e5de99d3603b364530ad04d930d038d24`，MIT 许可见 `renderer-LICENSE.txt`。本地副本保留 Windows 字体适配，并对输入框高度、持久化模块间距、连线与标签作了排版调整，静态文件与动画坐标同步。

已目视检查 PNG；输出合同检查通过；抽样帧之间均存在像素变化。当前环境未提供 ffprobe，媒体尺寸、帧数、每帧时长和总时长使用 Pillow 从实际 GIF 逐帧读取验证。
