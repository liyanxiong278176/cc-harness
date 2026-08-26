# cc-harness Desktop (experimental)

这是可选的本地桌面客户端入口，和 `cc-harness` CLI/TUI 共用同一套 Python
Durable Runtime。桌面端不会复制 Agent、提示词、工具、权限、压缩或费用逻辑。

## 当前第一阶段

- Windows system tray / macOS menu-bar 常驻
- 托盘状态图标按空闲、运行、待审批、失败和完成显示不同颜色，并附带活动计数
- 关闭窗口只隐藏，不停止 sidecar 或已提交的任务
- 从托盘退出时先检查活动任务，确认后才结束 Runtime
- 左侧工作区与多 run 列表；中间流式事件；右侧审批/任务/用量投影
- 底部显示模型、运行、连接、权限和活动数量
- Python sidecar 通过版本化 JSONL/stdin-stdout bridge 工作

## 本地开发

### 仅预览前端

```powershell
cd desktop
npm install
npm run dev
```

### Tauri 桌面调试

需要安装 Rust、Cargo 和 Windows 的 MSVC/Windows SDK；macOS 需要 Xcode
Command Line Tools。Tauri 的 external sidecar 名称必须是：

```text
cc-harness-desktop-bridge-<rust-target-triple>
```

放在 `desktop/src-tauri/binaries/` 下后运行：

```powershell
cd desktop
npm run tauri:dev
```

sidecar 源码也可以直接验证：

```powershell
python -m cc_harness.desktop_bridge --cwd D:\path\to\project
```

输入一行 JSON 即可执行 `hello`、`list`、`inspect`、`events`、`watch`、
`submit`、`follow_up`、`interrupt`、`cancel`、`resume`、`approve`、`reject`
和 `shutdown`。`watch` 事件使用持久化 `sequence`，客户端断线后可用
`events` 从最后一个序号补读。

## 打包

```powershell
cd desktop
npm run build
npm run tauri:build
```

本仓库的 `.github/workflows/desktop-release.yml` 会在 `desktop-v*` 标签或手动
触发时，为 Windows x64、macOS Intel 和 macOS Apple Silicon 构建安装包，并把
它们作为 GitHub Release 附件发布。当前工作流使用 GitHub Actions 的
`macos-15-intel` 与 `macos-15` runner，避免使用已退役的旧 macOS 镜像。工作流先在目标系统本机运行
`scripts/build_desktop_sidecar.py`，再运行 Tauri 构建；这样不会把某个平台的
Python 原生扩展错误地交叉打包到另一个平台。

正式发布前还需要为 Windows 和 macOS 配置代码签名/公证（证书、notarization
凭据只放在 GitHub Actions Secrets），并在仓库设置中限制谁可以创建
`desktop-v*` 标签。当前开发机若没有 Rust/MSVC，不能执行本地安装包构建，
但不影响 CLI/TUI 或 sidecar 协议测试。

## 设计边界

- 首次启动或恢复会话只读取状态，不自动调用模型或执行工具。
- 所有审批、停止、恢复和事件都写回现有 durable event stream。
- 桌面端只是交互与分发入口；CLI/TUI 仍是默认、可审计、可脚本化入口。
- 未来若需要浏览器或第三方客户端，可在不改变消息 envelope 的前提下增加
  localhost WebSocket 或 HTTP + SSE transport。
