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
- 标题栏和托盘菜单提供“检查更新”；发现新版本后自动下载、校验签名、安装并重启

首次打开桌面端时，请把左侧“工作区”改成实际项目目录（该目录应包含 `.env`，并
按 CLI 的方式配置 `OPENAI_API_KEY`、`OPENAI_BASE_URL`、`OPENAI_MODEL`），然后点
“连接”。如果连接失败，右侧会显示 sidecar 的真实启动错误，可直接点“重试连接”；
不再只显示笼统的 `desktop bridge is not running`。

## 下载并第一次运行

已验证可用的安装包在 [desktop-v0.1.3 Release](https://github.com/liyanxiong278176/cc-harness/releases/tag/desktop-v0.1.3)：

- Windows x64：[cc-harness_0.1.2_x64_en-US.msi](https://github.com/liyanxiong278176/cc-harness/releases/download/desktop-v0.1.3/cc-harness_0.1.2_x64_en-US.msi)
- macOS Intel：[cc-harness_0.1.2_x64.dmg](https://github.com/liyanxiong278176/cc-harness/releases/download/desktop-v0.1.3/cc-harness_0.1.2_x64.dmg)
- macOS Apple Silicon：[cc-harness_0.1.2_aarch64.dmg](https://github.com/liyanxiong278176/cc-harness/releases/download/desktop-v0.1.3/cc-harness_0.1.2_aarch64.dmg)

安装后按下面顺序使用：

1. 不要继续使用 `desktop-v0.1.1` 或没有安装包附件的旧 `desktop-v0.1.2`。
   Windows 先在“应用和功能”卸载旧版，再安装上面的 MSI；macOS 将旧应用移到废纸篓。
2. 准备一个实际项目目录，在其中创建 `.env`，例如：

   ```dotenv
   OPENAI_API_KEY=你的密钥
   OPENAI_BASE_URL=https://你的兼容接口/v1
   OPENAI_MODEL=你的模型名
   ```

3. 启动应用，把左侧“工作区”改成这个目录的绝对路径，点击“连接”。底部连接状态
   变为已连接后再发送任务。桌面安装包已经内置 sidecar，不需要另外安装 Python、
   `cc-harness` 或 npm 包。

如果 Windows SmartScreen 提示“未知发布者”，这是当前未购买商业代码签名证书的
未签名包：点击“更多信息 → 仍要运行”即可。签名发布流程仍可按下面的 Secrets 配置。

## 应用内更新

应用内更新使用 Tauri 官方签名更新包。安装了带 updater 的版本后，点击标题栏或托盘菜单
中的“检查更新”即可：没有新版本时显示当前版本；有新版本时自动下载、验证并安装，Windows
会退出当前进程完成安装，macOS 会自动重启到新版本。

旧的 `desktop-v0.1.3` 安装包还没有 updater 组件，因此需要手动安装一次后续的 updater 版本；
这是一项一次性迁移，之后每次更新都不需要再卸载或手动下载。

更新签名私钥只配置一次：

1. 本项目生成的私钥位于本机 `~/.tauri/cc-harness-updater.key`（Windows 对应
   `%USERPROFILE%\\.tauri\\cc-harness-updater.key`）。不要提交或公开它。
2. 打开 GitHub 仓库 **Settings → Secrets and variables → Actions → New repository secret**，
   名称填写 `TAURI_SIGNING_PRIVATE_KEY`，值填写该文件的完整内容。
3. 由于当前密钥没有密码，`TAURI_SIGNING_PRIVATE_KEY_PASSWORD` 可以留空；如果以后重新生成
   加密密钥，必须同时配置该 Secret。
4. 推送新的 `desktop-v*` 标签。工作流会生成 Windows/macOS 安装包、`.sig` 签名和
   `latest.json`，并把它们发布到 GitHub Release。以后应用内检查更新会读取该清单。

不要更换已经发布版本使用的更新公钥；如果丢失私钥，旧客户端将无法验证后续更新。

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

### GitHub Actions 签名配置

签名是可选的：没有这些 Secrets 时仍可构建测试用的未签名包；配置后，
同一条 Release workflow 会签名并验证安装包。不要把 `.pfx`、`.p12`、`.p8`
或密码提交到仓库。

另外，应用内 updater 必须配置 `TAURI_SIGNING_PRIVATE_KEY`（以及可选的
`TAURI_SIGNING_PRIVATE_KEY_PASSWORD`）；没有它，Release workflow 会主动失败，避免发布
无法自动更新的包。更新签名密钥与 Windows/macOS 代码签名证书是两套独立凭据。

Windows Secrets：`WINDOWS_CERTIFICATE`（Base64 PFX）、
`WINDOWS_CERTIFICATE_PASSWORD`、`WINDOWS_CERTIFICATE_THUMBPRINT`，可选
`WINDOWS_TIMESTAMP_URL`。macOS Secrets：`APPLE_CERTIFICATE`（Base64 P12）、
`APPLE_CERTIFICATE_PASSWORD`、`KEYCHAIN_PASSWORD`、`APPLE_API_ISSUER`、
`APPLE_API_KEY`、`APPLE_API_PRIVATE_KEY`。macOS 使用 Developer ID Application
证书和 App Store Connect API Key 做公证；填写 Secrets 后重新推送
`desktop-v*` 标签即可触发签名发布。

#### 一次性配置步骤

1. 在 GitHub 仓库的 **Settings → Secrets and variables → Actions** 中添加上面的
   Secrets。建议使用仓库 Secrets，并限制能够创建 `desktop-v*` 标签的人员；不要把
   证书文件、密码或私钥放进 Issue、PR、日志或代码。
2. Windows 证书需要是代码签名证书（不是 SSL 证书），导出带私钥的 `.pfx`：

   ```powershell
   certutil -encode certificate.pfx certificate-base64.txt
   (Get-PfxCertificate .\certificate.pfx).Thumbprint
   ```

   将 `certificate-base64.txt` 的内容填入 `WINDOWS_CERTIFICATE`，导出密码填入
   `WINDOWS_CERTIFICATE_PASSWORD`，去掉空格后的指纹填入
   `WINDOWS_CERTIFICATE_THUMBPRINT`。
3. macOS 需要付费 Apple Developer 账号、Developer ID Application 证书和
   App Store Connect API Key。将证书导出的 `.p12` 转成单行 Base64：

   ```bash
   openssl base64 -A -in certificate.p12 -out certificate-base64.txt
   ```

   将该文件内容填入 `APPLE_CERTIFICATE`，分别填入 `.p12` 密码、钥匙串密码、API
   Issuer ID、Key ID 和 `.p8` 私钥内容。API 私钥只能从 Apple 页面下载一次，下载后
   应立即安全保存。
4. 创建新的版本标签（例如 `desktop-v0.1.3`）：

   ```powershell
   git tag desktop-v0.1.3
   git push origin desktop-v0.1.3
   ```

   Actions 会分别构建 Windows、macOS Intel 和 macOS Apple Silicon，并在签名验证
   通过后更新 GitHub Release。Windows 可用 `Get-AuthenticodeSignature` 验证，macOS
   可用 `codesign --verify --deep --strict` 验证。任何一个平台的证书配置不完整时，
   该平台会明确失败，不会静默地把“看似正式”的包当成已签名包发布。

## 设计边界

- 首次启动或恢复会话只读取状态，不自动调用模型或执行工具。
- 所有审批、停止、恢复和事件都写回现有 durable event stream。
- 桌面端只是交互与分发入口；CLI/TUI 仍是默认、可审计、可脚本化入口。
- 未来若需要浏览器或第三方客户端，可在不改变消息 envelope 的前提下增加
  localhost WebSocket 或 HTTP + SSE transport。
