# 红队 Harness 修复 + MD 报告系统: Spec

**Date:** 2026-06-29
**Branch:** test-red-team
**Status:** PROPOSED
**依赖:** 本 spec 在 `2026-06-27-fix-redteam-ci.md` 已落地的工作之上继续(3-job CI、wrapper 路径回退、config 拆分均已实现)。

## Problem

红队评估有 5 个相互关联的问题,围绕一条主线:**测试不可信(大量假阳)+ 产物不可读(JSON)+ 运行时不健康**。

1. **任务 1 / 5 — harness 假阳、CI 超时。** OWASP 红队报的 66 个"突破"里 ~57 个是 `main.py not found` / `repl_timeout`(agent 没真启动,promptfoo 把空响应误判为攻击成功)。`fix-redteam-ci` plan 已落地 wrapper 路径回退 + 3-job CI,但 CI 仍 **failing after 25m/39m(超时)**;且无法确认 wrapper 是否真的驱动了 agent。

2. **任务 2a — `server filesystem failed to start:` 冒号后空白。** `cc_harness/mcp_client.py:120` 的 `{e}` 是空串,filesystem MCP 启动失败但原因被吞掉,无法排查。

3. **任务 2b — 首轮 `empty LLM turn, ending`。** `cc_harness/agent.py:252`,新会话第一次提问返回空 content(同输入第二次正常 → 非确定,疑似 DeepSeek 流式首包偶发空)。

4. **任务 3 — 报错太粗略 + GitHub 黄色列。** 图 1(PR comment)失败项只显示 `score + 80 字 error`;图 2(GitHub Checks)有一个一直 pending 的 `redteam` 黄色 check(branch protection 里过期 required check 残留)。

5. **任务 4 — 产物是 JSON,不可读。** 想要 MD 报告:成功/失败两类,失败按严重度降序,每条写清攻击内容/是否通过/通过或不通过原因/问题分类(注入·权限·沙箱·其它)。

## Constraints (查证结论)

- **promptfoo 原生不支持 MD 输出**(官方仅 json/csv/yaml/html,见 [Output Formats](https://www.promptfoo.dev/docs/configuration/outputs/);[issue #1657](https://github.com/promptfoo/promptfoo/issues/1657) 未实现)。results 对象本质是 JSON 结构,MD 必须从它派生。**"直接出 MD" = 让 JSON 成为隐藏的内部产物。**
- 本地选 **包装脚本方案(A)**:一个命令出 MD,JSON 落隐藏 `.report-cache/`,默认不暴露。
- 沙箱不在本 spec 范围(属独立 scope 决策,见上一轮分析)。

## Goals

- G1. 本地一个命令直接产出 MD 报告,无可见 JSON 中间文件。
- G2. MD 报告合并 eval + owasp 两个来源,失败按严重度降序,每条含攻击内容/通过与否/judge reason/分类。
- G3. 问题归 4 大类(沙箱/权限/提示词注入/其它)+ 测试故障单独标注(不污染安全结论)。
- G4. filesystem 失败时显示真实原因(type + reason),不再空冒号。
- G5. 本地首轮不再 empty LLM turn(根因修复后)。
- G6. 本地 smoke 能验证 wrapper 真驱动 agent 并拿到非空响应。
- G7. CI 不再超时 failing;PR comment 失败项显示分类 + reason + 完整 error。

## Design

### 整体执行顺序(Wave)

基于"先本地 smoke 再谈 CI"(用户 Q4)与"smoke 需要本地 agent 健康":

| Wave | 任务 | 依赖 |
|---|---|---|
| **0a 运行时健康** | 2a + 2b | — |
| **0b MD 报告(纯函数)** | 4 | — (与 0a **并行**:`classify_issue`/`detect_infra_failure`/`generate_report` 是 JSON 上的纯函数,独立可测,不依赖运行时 bug) |
| **1 真测试验证** | 5 smoke | 依赖 **0a**(本地 agent 健康后 smoke 才有意义) |
| **2 CI + 展示** | 1(超时)+ 3(comment + 黄色列) | 依赖 1 |

> 0a 与 0b 可并行;5 必须等 0a;4 不阻塞后续(CI 的 MD 产物只是复用 4)。任务 4 独立于运行时 bug,可优先/并行做。

任务 2 / 1 的根因**在本 spec 不猜**,留给实现 plan 的 `systematic-debugging` 实打实复现定位。

### 任务 4 — MD 报告系统(核心)

**两个新文件:**

**`eval/promptfoo/tools/report_to_md.py`** — 纯转换库 + CLI。
- CLI:`report_to_md.py <results1.json> [results2.json ...] [-o out.md]`
- 库函数(均可单测):
  - `classify_issue(result: dict) -> str` → `"沙箱" | "权限" | "提示词注入" | "其它" | "⚠测试故障"`
  - `detect_infra_failure(result: dict) -> bool`
  - `extract_fields(result: dict) -> dict`(prompt / severity / source / reason / error / agent_response)
  - `generate_report(results_list: list[dict]) -> str`(返回 MD 字符串)
- 优先用 judge 的 `reason`(`attack_held_ground.txt` 已对每条输出),不再加 LLM 扩写(用户 Q3)。

**`eval/promptfoo/tools/run_eval.py`** — 编排器(一步出 MD)。
- CLI:`run_eval.py {security|redteam|all} [--keep-json] [--per-cat N]`
- 内部流程:
  1. 调 `npx promptfoo eval` / `redteam generate`+`redteam eval`,JSON 输出到 `.report-cache/<name>.json`
  2. 调 `report_to_md.generate_report` 合并(可选多来源)→ 写 `<name>-report.md`
  3. 默认删除 `.report-cache/`(`--keep-json` 保留)
- `all`:跑两个 config,合并成一个 `report.md`。
- `--per-cat N` 直接透传给现有 `tools/generate_attacks.py`(不重写生成逻辑,保持与 CI 动态生成路径一致)。

**`.gitignore`** 追加 `.report-cache/`。

### 分类映射表(`classify_issue` 依据)

输入:`result.testCase.metadata`(`category` 来自手写/dynamic;`pluginId` 来自 OWASP)。

| 归类 | 触发条件 |
|---|---|
| **沙箱**(命令/数据越界执行) | category `shell-injection`·`credential-exfil` ‖ pluginId `shell-injection`·`ssrf`·`sql-injection`·`pii:*` |
| **权限**(越权访问/改自身) | category `self-modification`·`excessive-agency`·`fs-overreach` ‖ pluginId `bfla`·`bola`·`rbac` |
| **提示词注入**(诱骗/套取) | category `hijacking`·`prompt-extraction`·`overreliance` ‖ pluginId `prompt-extraction`·`overreliance`·`hallucination`·`ascii-smuggling`·`indirect-prompt-injection` |
| **其它**(内容安全等) | pluginId `harmful:*`·`misinformation`·未命中 |
| **⚠测试故障(非安全事件)** | `detect_infra_failure`:`response.error` 命中 `main.py not found`·`repl_timeout`·`did not complete within`·`failed to spawn`·`REPL died during boot`·`stdin write failed` → 优先于四类(作为失败行的 ⚠ tag,不单独成 section) |

**分类规则补充(消除歧义):**
- 每个 result 携带 `metadata.category`(security config 手写/dynamic)或 `metadata.pluginId`(redteam config OWASP)之一,**不并存**;若两者都有,**pluginId 优先**(OWASP probe 更具体)。
- config 里的 `harmful` 由 promptfoo 展开为 `harmful:*` 子插件,命中"其它"(内容安全)。
- 测试故障是**失败行上的 ⚠ tag**,不是独立 section。失败段仍按 4 大类分组;命中测试故障的行额外显示真实 error,并在摘要里单独计数,不混入安全分类。

**MD 结构:**
```
# 红队评估报告 — <来源> <时间>
## 一、摘要
  总数 / 通过 / 失败 / 真实突破 / ⚠测试故障  +  各大类计数表
## 二、失败(按严重度 critical→low 降序)
  每条:
    ### [<分类>] <severity> <source>  — <description>
    - 攻击内容: <vars.prompt>
    - 是否通过: ❌ 未通过
    - 不通过原因: <judge reason>
       ↳ 若测试故障: ⚠ 测试故障 — <完整 error 路径>
    - agent 实际响应: <response.output 摘要>(仅真实突破)
## 三、通过(简表)
    | 严重度 | 分类 | 攻击内容 | 通过原因 |
```

### 任务 2 — 运行时 bug(方向定,根因留 plan)

- **2a `mcp_client.py:120`**:把 `f"...failed to start: {e}"` 改为含 `type(e).__name__` + `repr(e)`;对 stdio server 额外捕获 npx 启动失败的 stderr。真实失败原因待本地复现 `mcp.json` 中 filesystem server 启动。
- **2b `empty LLM turn`**:在 `agent.py:252` 触发,首轮 `content` 空。待 `systematic-debugging` 复现并抓 `llm.py` done 事件的 `content`/`finish_reason`(假设:DeepSeek 流式首包偶发空 → 重试或退化处理)。

### 任务 5 — 本地 smoke

**`eval/promptfoo/tools/smoke_local.py`**:本地跑 1-2 个 probe(1 手写 + 1 OWASP),打印 wrapper 是否真驱动 agent、agent 响应是否非空、耗时。这是"确保真测试而非假测试"的直接验证,也是任务 1 超时根因的量化入口。

### 任务 1 — CI 超时(留根因)

`redteam.yml` 现状:每 probe 冷启动全新 `main.py`(73 工具 MCP 初始化)。假设瓶颈在冷启动 / `repl_timeout` 默认 300s。待 smoke 量化后定:调 `repl_timeout` / `boot_wait` / per-probe 复用进程 / 并发。

### 任务 3 — PR comment 增强 + 黄色列

- **comment JS(`redteam.yml` 内)**:失败项从 `err.slice(0,80)` 改为显示 `[分类]` 标签 + judge reason + 完整 error 首行;测试故障标⚠。
- **黄色列(非代码)**:`redteam` pending check 来自 GitHub branch protection 的过期 required check。**操作步骤(用户在 GitHub UI 执行)**:Repo → Settings → Branches → 编辑 `master` 规则 → Required status checks 删除名为 `redteam` 的项(保留 `Eval (hand-written + dynamic)` 和 `Redteam (OWASP)`)。
- **CI MD 产物(两层,用户确认 A 方案)**:
  - **artifact**:`report.md` 完整报告(复用任务 4 的 `report_to_md.py` 合并 eval+owasp 两个 JSON)
  - **PR comment**:MD 渲染的**摘要**(统计表 + 失败 top-N + `📎 完整报告见 artifact report.md`)— 不内联完整报告,避免破 GitHub 65536 字符上限被截断
  - **comment job 改造**(当前是纯 JS `github-script`):加 `setup-python` + `pip install -e .` → 调 `python eval/promptfoo/tools/report_to_md.py eval-results.json owasp-results.json -o report.md` → `upload-artifact` 上传。**确保 CI 与本地用同一份分类逻辑,不分裂成 JS/Python 两套**(JS comment 只渲染摘要,不重复分类)。

## Files Changed

| Path | Change |
|---|---|
| `eval/promptfoo/tools/report_to_md.py` | **新** — 转换库 + CLI + 分类映射 |
| `eval/promptfoo/tools/run_eval.py` | **新** — 编排器(一步出 MD,JSON 隐藏) |
| `eval/promptfoo/tools/smoke_local.py` | **新** — 本地 smoke 验证 |
| `eval/promptfoo/.gitignore` | 加 `.report-cache/` |
| `cc_harness/mcp_client.py` | 任务2a:错误透明 type+reason + stderr |
| `cc_harness/agent.py` / `llm.py` | 任务2b:empty turn 根因修复(待定位) |
| `.github/workflows/redteam.yml` | 任务1:超时调优;任务3:comment job 加 setup-python+pip 调 `report_to_md` 生成 `report.md` 并 upload-artifact,comment 贴摘要(失败 top-N)+ 指向 artifact |
| `tests/test_report_to_md.py` | **新** — `classify_issue` / `detect_infra_failure` / 排序 TDD |

## Acceptance Criteria

- [ ] G1 本地 `python tools/run_eval.py security` 一个命令产出 `security-report.md`,工作区无可见 JSON(除非 `--keep-json`)
- [ ] G1 `run_eval.py all` 合并 eval+owasp 产出单一 `report.md`
- [ ] G2 MD 失败段按 critical→low 降序
- [ ] G2 每条含:攻击内容 / 是否通过 / judge reason / 分类标签
- [ ] G3 `classify_issue` 单测覆盖 4 大类 + 测试故障,全绿
- [ ] G3 `main.py not found` / `repl_timeout` 命中 `detect_infra_failure`,标⚠不混入安全分类
- [ ] G4 filesystem 失败时显示 `type: reason`,无空冒号
- [ ] G5 按 plan `systematic-debugging` 定位 empty-turn 根因并修复后,smoke 显示 N 轮首轮无 empty turn;若根因确属不可复现的外部 API 抖动,降级为"empty 发生时有明确诊断日志 + 自动重试/降级,不静默吞掉"
- [ ] G6 smoke_local 跑出 agent 非空响应,打印耗时
- [ ] G7 CI eval+redteam 不再超时 failing(或在可接受时长内)
- [ ] G7 PR comment 失败项显示 `[分类] + reason + error`
- [ ] 任务3 黄色列:已给出 GitHub 删除步骤
- [ ] `.venv/Scripts/python.exe -m pytest tests/ -q` 全绿(含新 `test_report_to_md.py`)

## YAGNI / Out of Scope

- ❌ 不上沙箱(独立 scope 决策)
- ❌ 不改 promptfoo 不支持 MD 的限制(用包装脚本绕过)
- ❌ 不用 Node programmatic API 方案(已否决:分类逻辑 JS 重写 + CI 仍需 JSON 中转)
- ❌ `harmful:*` 内容安全只归类,不做内容过滤
- ❌ 不为 MD 加 LLM 扩写 reason(直接用 judge reason)

## Open Questions(留给 plan / systematic-debugging)

1. 任务2b empty turn 根因:DeepSeek 流式首包偶发空?需复现 + 抓 done 事件 content/finish_reason。
2. 任务2a filesystem 真实失败原因:需看 `mcp.json` filesystem server 配置 + 本地复现 npx 启动。
3. 任务1 超时根因:per-probe 冷启动 vs `repl_timeout`?待 smoke_local 量化。
