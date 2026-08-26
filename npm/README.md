# @liyanxiong278176/cc-harness

`cc-harness` 的 npm 安装入口。核心运行时仍然来自 Python 项目；此包只负责
准备运行环境并转发命令行参数。

## 安装

```bash
npm install -g @liyanxiong278176/cc-harness
cc-harness
```

要求 Node.js 18+。如果系统安装了 `uv`，优先使用 uv 运行 Python 核心；否则
使用 Python 3.11+ 在用户缓存目录创建隔离虚拟环境。首次运行会下载依赖，可能
需要一些时间。

## 运行指定项目

```bash
cd /path/to/project
cc-harness
cc-harness --cwd /path/to/project
```

所有参数都会原样传给 Python 核心，例如 `cc-harness --help`、`cc-harness -c`。

## 配置核心来源

默认从 GitHub 的固定提交获取 Python 核心，保证 npm 版本可复现。发布新版本时，
同步更新 npm 版本和入口脚本中的提交号。也可以通过环境变量覆盖来源：

```bash
CC_HARNESS_CORE_SOURCE="git+https://github.com/liyanxiong278176/cc-harness.git@2f9f176"
CC_HARNESS_NPM_REINSTALL=1
cc-harness
```

Python 模型配置仍使用 `OPENAI_API_KEY`、`OPENAI_BASE_URL` 和 `OPENAI_MODEL`，
也可以在首次启动时交互填写。

## 维护者发布

`liyanxiong278176` 是当前维护者账号的 npm 用户 scope。登录官方 registry 后，
在本目录执行：

```bash
npm login --registry https://registry.npmjs.org
npm publish --access public --registry https://registry.npmjs.org
```

发布前应先提升 `package.json` 版本，并同步更新入口脚本中的 `DEFAULT_CORE_REF`。
