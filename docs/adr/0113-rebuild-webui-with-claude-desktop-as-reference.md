# ADR-0113：以 Claude Desktop 为参照重建 cc-harness WebUI

- 状态：Accepted
- 日期：2026-09-30
- 范围：React/Vite WebUI、项目产物只读 API

## 背景

用户要求完全重做当前 WebUI，并以本机 Claude Desktop 的页面布局、样式和对话交互为主要参照。现有设计契约与 ADR-0111 选择了 vendored DeepSeek UI 和固定三栏工作台；这与新方向冲突。cc-harness 的项目、会话、Durable Runtime、权限、SSE 和既有 API 仍须保持原语义。

## 决策

1. 用 cc-harness 自有 React/Vite 页面实现当前 `web/src`，不依赖 `vendor/deepseek-ui/` 的模块别名或源码；Claude Desktop 仅提供实机布局和交互参照，不复制其源码、品牌资源、云端能力或账户功能。Vendor 归档清理不属于本 ADR 的交付内容。
2. 将左侧导航映射到新建、项目、产物、自定义/设置与现有会话历史；对话主区采用宽阔的居中布局，Runtime 详情改为按需覆盖面板。
3. 保留现有 `/api`、REST/SSE、Durable Runtime、TUI/headless/SDK 语义。只为 Artifacts 增加当前项目范围内的只读列表与预览 API；项目产物覆盖该项目所有历史任务的 Agent 创建/修改路径，按相对路径去重，预览工作区当前文件，不存历史快照。
4. 产物路由由服务端当前选中项目确定根目录，并拒绝越界路径和项目外符号链接。历史删除或不可安全读取的路径不得退回 Runtime 对象存储或旧快照。
5. 本 ADR 取代 ADR-0111，并更新现有 WebUI UX 契约。DeepSeek UI 观察记录保留为历史材料，不再作为当前实现来源。

## 结果与权衡

- 完全拥有前端源码与运行时连接方式，避免依赖大体量上游页面快照和协议适配层。
- Claude Desktop 的布局和交互可作为清晰的产品参照，同时实现仍需使用 cc-harness 自身品牌和本地功能。
- 不依赖上游页面代码会失去直接复用；自有前端需要持续承担样式与交互实现维护。
- 产物预览呈现的是当前项目文件，无法恢复或比较历史版本；路径来源仍可追溯到历史任务。

## 参考契约

- [`Claude Desktop 参考式 WebUI 重设计契约`](../design/claude-desktop-reference-webui.md)
- 被取代记录：[`ADR-0111`](0111-vendor-pinned-deepseek-ui-snapshot.md)。
