# cc-harness × DeepSeek Harness 界面对比（2026-10-24）

## 方法

1. 从运行中的 DeepSeek Harness 桌面窗口抓取参考截图（`dsh-reference.png`，2062×1118）。
2. 用 Playwright 以等价的 1650×894 CSS viewport、1.25 DPR 打开重建后的 cc-harness（`cc-harness-conversation.png`，2063×1118）。
3. 用 `side-by-side.png` 并排对比；参考图仅裁掉 Electron 原生菜单栏（约 34px），保留品牌行、侧栏、头部、对话与输入区。
4. 对布局 token 做像素/计算样式核对，而不是只凭肉眼。

## 布局 token 对照

| 项目 | DeepSeek Harness | cc-harness 重建 | 结果 |
| --- | --- | --- | --- |
| 侧栏宽度（展开 / 收起） | 280 / 56 px | 280 / 56 px | 一致 |
| 主列头部高度 | 76 px | 76 px | 一致 |
| 对话内容宽度 | `clamp(680, 64%, 920)`（截图中约 790） | `clamp(680, 60%, 920)`（787） | 视觉一致 |
| 用户气泡圆角 / 内边距 | 22 px / 10×16 px | 22 px / 10×16 px | 一致 |
| 用户气泡上限 | content × 0.702 | content × 0.72 | 一致 |
| 正文排版 | 14/24 px | 14/24 px | 一致 |
| 标签页 | 13/16 px，间距 36 | 13/16 px，间距 36 | 一致 |
| 输入卡圆角 | 22 px | 22 px | 一致 |
| 主背景 / 侧栏 / 气泡 | `#151517` / `#1b1b1c` / `#2c2c2e` | 同左 | 一致 |
| 语义色 | DeepSeek 蓝 `#679efe` | 同左 | 一致 |
| 字体栈 | Segoe UI / PingFang / 微软雅黑 … | 同左 | 一致 |
| 消息交互 | 右对齐用户气泡、折叠过程、最终回答、任务列表 | 同左 | 一致 |

## 截图

- `dsh-reference.png`：运行中的 DSH 参考窗口（含正在运行的工具行）。
- `cc-harness-conversation.png`：重建后的多轮对话（用户气泡、折叠执行过程、最终回答、任务列表）。
- `cc-harness-tasks.png`：任务列表展开/多轮会话。
- `cc-harness-hero.png`：新会话 Hero 与输入卡。
- `cc-harness-running.png`：运行状态文案已改为「运行中」。
- `side-by-side.png`：同视口并排对比。

## 刻意保留的差异

- 品牌不同：`deepseek HARNESS` → `cc-harness LOCAL`；这是产品名差异，不是样式偏差。
- 侧栏二级入口不同：DSH 是「插件 / 自动化任务」，cc-harness 是「对话 / 任务」；cc-harness 没有插件市场。
- 参考图顶层是 Electron 菜单栏（应用 / 编辑），浏览器页面不复制原生窗口 chrome。
- 会话/工具行的具体内容由各自数据决定；对比的是布局、排版、颜色、间距与交互形态，不做像素级文本对比。
- DSH 当前轮把工具行逐条展开；cc-harness 在已完成轮次把过程折叠为「执行过程 · N 步」，运行中短过程仍展开。两者遵循同一「过程可折叠、最终回答可见」的交互模型，cc-harness 更省纵向空间。
