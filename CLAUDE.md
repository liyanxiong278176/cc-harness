# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this is

cc-harness 是一个**跑在终端里的编程代理**:通过 OpenAI 兼容 LLM(默认配 DeepSeek)执行 ReAct 循环,工具来自 MCP server(fs/git)+ 一个内置 `run_command`,输出 思考/行动/观察/结果 4 段。

## Common commands

```bash
# Install (pyproject.toml is the source of truth for deps)
pip install -e .                 # also required by the eval provider (imports cc_harness)
pip install -e '.[dev]'          # pytest / pytest-asyncio / pytest-cov / ruff

# Run the REPL (entry point)
.venv/Scripts/python.exe main.py
.venv/Scripts/python.exe main.py --mode plan          # start in plan mode
.venv/Scripts/python.exe main.py --design-dir <path>  # custom design save dir

# Tests (~200 in tests/test_*.py)
.venv/Scripts/python.exe -m pytest tests/                 # all
.venv/Scripts/python.exe -m pytest tests/test_X.py -v     # one file
.venv/Scripts/python.exe -m pytest tests/test_X.py::test_name  # one test
.venv/Scripts/python.exe -m pytest tests/ -k "name_substr" # by name

# Lint
.venv/Scripts/python.exe -m ruff check cc_harness/ tests/

# Phase-1 regression (creates + runs hello.py end-to-end)
.venv/Scripts/python.exe run_verify.py

# Eval / red-team (see "Eval / red-team" section below)
cd eval/promptfoo && npm run security     # static + dynamic attacks → eval
cd eval/promptfoo && npm run view         # browser UI of last result
python eval/promptfoo/tools/run_eval.py all --keep-json  # one-shot: security + OWASP → .md report
```

Force UTF-8 on Windows (avoids GBK crashes on 思考/✅/中文):
```bash
PYTHONIOENCODING=utf-8 .venv/Scripts/python.exe ...
```

## Architecture (data flow)

```
main.py
  └── repl.py:run_repl()                  # sticky mode (coding/plan/design), slash commands
        │     slash cmds: /plan /design /coding /mode /help /clear (case-insensitive)
        ├── run_turn()  [agent.py]        # ReAct while loop, max_iter=20
        │     ├── llm.py:LLMClient        # OpenAI stream + tool_calls accumulator
        │     ├── mcp_client.py:MCPClient # stdio/sse/http → OpenAI tool schema
        │     ├── tools.py:NATIVE_TOOLS   # currently: run_command (asyncio subprocess)
        │     ├── tools.py:is_dangerous   # rm -rf / format / drop / shutdown → user confirm
        │     ├── prompts.py:Section pool # 10 sections in SECTION_POOL, gated by conditions
        │     ├── tokens.py               # tiktoken 5-bucket counting + turn/session stats
        │     └── render.py               # 4-phase ReAct output (思考/行动/观察/结果)
        └── _print_disk_changes()         # post-turn: show files modified in last 30s

cc_harness/memory/                        # ⚠️ in-tree but NOT yet wired into the ReAct loop
                                          #   (no import from agent/repl/main). SQLite + embeddings.
```

**Key data flow**:
- `messages: list[dict]` (OpenAI chat format) is the single state across turns
- `messages[0]` is the system prompt; rebuilt on every turn in `agent._refresh_system_prompt` to match the current mode
- Tool specs: `mcp.list_tools() + NATIVE_TOOLS specs` → sent to LLM; tool_calls routed by name (MCP vs native)
- Streaming is buffered (not token-by-token). Each iteration prints the LLM's full text as a single 思考 block, then 行动/观察 for each tool call, so the 4-phase layout is clean and never duplicated. See `agent.run_turn` for the trade-off.
- `tokens.py` categorizes the final `messages` + tool schemas into 5 buckets (system/user/tool_calls/llm_output/tool_definitions) and compares tiktoken totals against the API-reported usage (`api_vs_breakdown_drift_pct`). Per-turn (`TurnTokenStats`) aggregates roll up into `SessionTokenStats`.

## Design decisions (non-obvious)

**3 modes, not just 1.** `mode in {coding, plan, design}` is sticky across the turn. In plan/design, `tool_specs = None` is sent to the LLM so it physically cannot emit tool_calls (any that leak through are dropped with a warn).

**`run_command` is built-in, NOT via MCP.** Community shell MCP servers either don't work on Windows (`@kevinwatt/shell-mcp` uses `whereis`) or require LLM sampling we don't implement (`@mako10k/mcp-shell-server` enhanced mode). The native async subprocess in `tools.py` just works. Don't add an MCP shell server back without understanding why we removed it.

**Section pool, not a single string.** `prompts.py` has 10 sections in `SECTION_POOL` with conditions (`mode==coding`, `mode==plan`, `mode==design`, `has_tools`, `always`). To add a new section, register it in the pool — don't touch `build_system_prompt`.

**Safety is not a sandbox.** `is_dangerous` only matches a hardcoded regex list (rm -rf, format, drop table, fork bomb, shutdown, reboot). It's "prevent accidental mistakes" not security. Don't expand the regex list to be a permission system — that scope was explicitly cut. (The red-team eval below is how we actually measure safety.)

**L4 权限闸门(M1,2026-06-30)。** `agent.py` 派发点不再用 `is_dangerous` 正则当闸门,
改用 `cc_harness/policy.py` 的 Claude Code 式 allow/ask 两档引擎(无 deny)。
执行/写/工作区外读/出站 → ask(用户 yes/always/no);工作区内读 → allow。
`is_dangerous` 保留但仅用于丰富 ask 原因。会话 allowlist 进程内、退出即失效。
红队无需改:wrapper 喂 `exit` 行 → confirm 返回 no → 所有 ask 自动不执行。
执行加固(cwd 锁/env 剥离/超时)在 `cc_harness/executor.py`。审计落 `<root>/logs/policy.jsonl`。
完整设计见 docs/superpowers/specs/2026-06-30-l4-policy-engine-design.md。

**L2 输入防御(M2,2026-07-01)。** `repl.py` 读入用户输入后、进 `run_turn` 前过两道:
① `cc_harness/l2.py:heuristic_check`(传统正则,命中即拦,零延迟);
② DeepSeek judge(复用 provider,结构化 JSON 分类 benign/injection/jailbreak)。
命中即**硬阻断**:不进主 LLM、不调工具、**不入 messages 历史**(切断上下文传播),
经 `print_result` 打模糊拒绝模板(不透露检测原因,避免帮攻击者迭代)。真实原因落 `<root>/logs/l2.jsonl`。
指令层级(`prompts.py:instruction_hierarchy`,始终生效):`<user_input>` 包用户输入、
`<untrusted>` 包外部工具输出(`agent.py` 仅成功回填处),声明开发者>用户>工具返回。
judge 失败 fail-open(`judge_error` 审计,L4 兜底)。kill-switch:`policy.yaml` 的 `l2.enabled=false`。
**无 key 退化**:judge 仅在配置了 `OPENAI_API_KEY` 时构造 client;无 key 时 `l2_client=None`,
judge 路径 fail-open(等价 heuristic-only,审计记 `judge_error:AttributeError`),heuristic 第一道仍生效。
完整设计见 docs/superpowers/specs/2026-07-01-l2-input-defense-design.md。

**L5 输出 DLP(M3,2026-07-01)。** 与 M2(L2 输入)对称,守**输出**:`agent.py:run_turn`
在 LLM 主动产生的文本(思考段 + 结果段)被 `print_*` / `messages.append` 之前过 `cc_harness/l5.py`。
分层检测:① Layer A 密钥正则(`KeyRegexLayer`,零依赖,永远在,已知格式:OpenAI/AWS/GitHub/GitLab/Slack/Google/PEM/JWT);
② Layer B Presidio PII(`PresidioLayer`,可选 `pip install -e '.[dlp]'`,邮箱 + 中文手机/身份证 custom recognizer)。
命中替换为 `[REDACTED:<type>]`,**历史也存脱敏版**(切断"思考读到→结果复述"二段泄露)。
原则:**宁漏勿误**——不做泛化高熵串检测(会误伤 SHA256/UUID/base64/commit hash)。
fail-soft:Presidio 不可用 → 自动只跑 Layer A(密钥仍护);scan 异常 → 原文返回(DLP 不把 Agent 弄哑)。
静默脱敏 + 审计落 `<root>/logs/l5.jsonl`(`action=l5_redact`,只记类型计数,**绝不记明文**)。
kill-switch:`policy.yaml` 的 `l5.enabled=false`。工具观察段不扫(M2 已用 `<untrusted>` 隔离;脱敏它破坏 coding)。
完整设计见 docs/superpowers/specs/2026-07-01-l5-output-dlp-design.md。

**Windows GBK fix in `main.py` lines 17-23 must stay.** Without `sys.stdin.reconfigure(encoding="utf-8")`, the GBK default codepage crashes on the first non-ASCII char the LLM outputs (✅, 中文, 思考, etc.).

## Eval / red-team (`eval/promptfoo/`)

A promptfoo-based red-team suite (LOCAL only, 2026-07-06 起退役 CI integration):

- **Provider** = `wrappers/cc_harness.py`, a custom Python provider that spawns `python -u main.py --mode coding`, waits `boot_wait`s for MCP init, writes the attack prompt to stdin + `exit`, then parses the 结果 segment out of the 4-phase output. `timeout` is in **ms** (not s) — footgun(no implicit conversion,在 wrapper 已是 ms,但 README 一律标 ms)。
- **One unified config, LOCAL only (2026-07-06 起)**:`promptfooconfig.unified.yaml` — 同文件含 `tests:` + `redteam:` 两段,两次 promptfoo invocation 一把跑完。取代历史的 4 个 config(`security` / `redteam` / `redteam-full` / `security-allow`,已删除)。
  - **tests 段**(`promptfoo eval`):静态 40(`attacks.yaml` git-tracked,含 §10-14 新增 5 类打 L2/L5 盲点 `pii-exfil`/`encoded-exfil`/`repo-injection`/`terminal-injection`/`delayed-ci-exfil`)+ 动态 N(`dynamic_attacks.yaml` gitignored,`run_eval.py unified` 自动 regen)+ 沙箱 30(inline 进 config,`providers: [cc-harness-allow]` 覆盖顶层 `defaultTest.providers` 走沙箱模式)。`defaultTest.assert` = `not-contains-any`(密钥前缀)+ `javascript`(PII 正则)+ `llm-rubric`(threshold **0.7**, `judges/attack_held_ground.txt`)+ `python`(非空)— AND 关系,确定性断言兜底"嘴上 hold 实际泄露"。
  - **redteam 段**(`promptfoo redteam run`):OWASP 全 4 类(1/6/7/9 → 17 底层 plugins,见 §9.6)+ `coding-agent:all`(13 件全集,取代历史的 `:core` 5 件)+ `mcp`。Needs `PROMPTFOO_API_KEY`(promptfoo cloud 生成 attack)。`numTests: 3` × ~20 plugin = ~60 底 probe(展开含 `harmful` × 26)~150-200,本地串行 ~5-10h。
  - **执行类沙箱(L8)**:inline 沙箱 30 条让 wrapper 注 `CC_HARNESS_AUTOCONFIRM=always` + `SANDBOX_FALLBACK=hard`(沙箱挂不降级 native → 防降级泄露 CI secret / 真泄露),`judges/sandbox_isolation.txt` 判容器 vs 宿主视角。前置 Docker + `pip install -e '.[sandbox]'`,不起跑 → `SandboxExecutor` fail-soft → false negative(L8 hold 不住报 hold,假数据)。
  - **跑法**:`python tools/run_eval.py unified`(或 `npm run redteam-all`)— 内部 `promptfoo eval` 跑 tests 段 + `promptfoo redteam generate/eval` 跑 redteam 段,合一份 `unified-report.md`。`run_eval.py` 仍保留 `security/redteam/all` 旧入口(向后兼容,历史引用)。
  - **不卡 CI**:本机跑 ~5-10h,Actions free-tier 跑不起。`.github/workflows/redteam.yml` 与 `.github/workflows/redteam-full.yml` 2026-07-06 起整体退场(stub,`on: {}`),CI 红队历史详见 git blame。
- **`defense_matrix.yaml` = 分类单一来源**:category/pluginId → 防御层(L2/L4/L5/**L8**/judge)+ severity。执行类(coding-agent `sandbox-read/write-escape`/`network-egress-bypass`/`procfs-credential-read`/`secret-env/file-read`)主层 **L8**(沙箱隔离层,spec §6.4):allow 模式跑,区分 L4(agent 闸门漏)vs L8(沙箱兜)。`report_to_md.py:classify_layer` 从它加载(取代硬编码表)。加新 category/pluginId **必须先登记**,否则 `UnknownCategoryError` → report fail-closed 收集到"未知 category"段(不静默落"其它")。`harmful:*` special-case 归 judge(不进 matrix)。
- **Per-layer ASR 衡量**:report 的"防御矩阵"表按 L2/L4/L5/**L8**/judge 各层汇总 ASR(真实突破/总数),回答"M1(L4)/M2(L2)/M3(L5)/M4(L8 沙箱) 各挡哪类攻击"。L8 ASR = allow 模式执行类攻击的沙箱隔离率(沙箱 hold/总数);deny 模式 config 不产生 L8 数据(执行类只在 allow 跑)。
- **`[dlp]` 前置**:测 PII 类(`pii-exfil`)前 `pip install -e '.[dlp]'`(装 presidio)。未装时 `report_to_md._presidio_available()` 返回 False,`pii-exfil` 不计入 L5 ASR(避免被算成 L5 全突破),report 标"环境未就绪"。
- **Tools (`eval/promptfoo/tools/`)**:
  - `generate_attacks.py` — LLM-generates dynamic attacks per category (5 cats × N). Categories are hardcoded — **add a new capability to cc-harness → add a category here first**, else the dynamic generator won't probe the new attack surface.
  - `curate_attacks.py` — promotes eval failures into the static set. Promotion requires `score < 0.4` AND `cosine_sim < 0.85` vs existing (embedding dedup, fail-closed). Manual (`npm run curate`); human reviews the diff before commit.
  - `report_to_md.py` — **single source of truth for classification + PR comment** (no JS/Python split). Run by CI's comment job. `classify_layer` + `compute_asr_by_layer` + `severity_gate()` 都在这。
  - `run_eval.py` — one-shot Python harness: `python tools/run_eval.py {security|redteam|all|unified} [--keep-json] [--per-cat N]`. 推荐 `unified`(LOCAL 一键跑完,2026-07-06 起取代 `security/redteam/all` 三个旧入口)。Wraps `npx promptfoo`, writes JSON to `.report-cache/` (deleted unless `--keep-json`), emits `*-report.md`(`unified-report.md` 为合并输出)。
- **CI retired (2026-07-06)**:`.github/workflows/redteam.yml` 与 `.github/workflows/redteam-full.yml` 整体退场(`on: {}`,stub)。Actions free-tier 跑不起 full OWASP + coding-agent:all(~5-10h,job 360min 上限余量为 0),改成 LOCAL `npm run redteam-all` 跑。CI 历史上以 `redteam.yml` 3 jobs(eval → redteam → comment)串 + `severity_gate` 阻断合并;`run_eval.py unified` 单进程含所有 4 类 ASR (L2/L4/L5/L8)。CI 红队历史详见 git blame,re-enable 需重写一个 CI-only config(`numTests: 1` 提速 + 启 PROMPTFOO_API_KEY secret)。

## Test conventions

- ~200 tests in `tests/test_*.py` (collected by pytest, default pattern). Eval tooling has its own tests: `test_generate_attacks.py`, `test_curate_attacks.py`, `test_dedup_logic.py`, `test_report_to_md.py`, `test_run_eval.py`, `test_smoke_curate.py`, `test_strategies_yaml.py`, `test_cc_harness_wrapper.py`, `test_tokens.py`, `test_defense_matrix.py`, `test_attacks_yaml.py`, `test_promptfoo_configs.py`.
- `tests/_test_*.py` (leading underscore) are **integration tests requiring a real LLM** — not collected by pytest by default; only run manually
- Test agents use `FakeLLM` (pre-programmed stream events) + `FakeMCP` (pre-programmed tool results), defined in `test_agent.py` and reused via imports
- New test file naming: `test_<module>.py`, mirror source module names
- For REPL tests, mock `_read_user` (not `builtins.input` directly — too fragile in subprocess tests)

## Config & files

- `.env` — core (3 required, no defaults — see `config.py`): `OPENAI_API_KEY`, `OPENAI_BASE_URL`, `OPENAI_MODEL`. Eval + memory add: `EMBEDDING_BASE_URL` / `EMBEDDING_API_KEY` / `EMBEDDING_MODEL` / `EMBEDDING_DIM` (SiliconFlow `BAAI/bge-m3`, dim 1024), `JUDGE_MODEL`, and `PROMPTFOO_API_KEY` (OWASP plugins only). CI builds these into `eval/promptfoo/.env.ci` from secrets.
- `pyproject.toml` — deps + packaging (`pip install -e .`). pytest config: `asyncio_mode = "auto"`, `testpaths = ["tests"]`. ruff: line-length 100, target py311.
- `mcp.json`: MCP server entries. Per-server failures are isolated — one bad server logs a red warning, the rest still boot (`init_timeout_s` defaults to 30s in `mcp_client.py`). The bundled config mixes stdio (npx-launched: filesystem, playwright) and SSE (ModelScope-hosted: fetch + others) transports. Tool names are exposed to the LLM as `mcp__{server}__{tool}`.
- `~/.cc-harness/designs/`: design-mode artifacts land here by default (`{ISO ts}-{first-line-slug}.md`); override with `python main.py --design-dir <path>`
- `docs/superpowers/` — planning artifacts for the superpowers workflow: `plans/<date>-<slug>.md` and `specs/<date>-<slug>-design.md` per feature (e.g. context-compaction, real-token-tracking, dynamic-attack-generation, severity-redesign). Read the matching plan before extending that feature.
- `run_verify.py` (root): Phase-1 regression script — spawns the REPL as a subprocess, pipes one command in, captures output, exits. Useful for end-to-end smoke after a refactor. Requires a real LLM (hits the configured provider).
- `eval/bug/`: captured red-team failure artifacts from past runs (result JSONs, screenshots, reports) — debugging evidence, not test fixtures.

## Out of scope (don't add unless asked)

- Multi-LLM backend switching (locked to OpenAI-compatible)
- Sandbox / Docker — M1 (2026-06-30) landed a portable permission gate (`cc_harness/policy.py`, allow/ask two-tier) + execution hardening (`cc_harness/executor.py`: cwd-lock, env-secret-strip, timeout);M4 (2026-07-03) landed OpenSandbox 用户态容器沙箱(`cc_harness/sandbox.py:SandboxExecutor`,Docker runtime,会话级 lazy create + 项目根 RO mount + 通信错降级 native;`cc_harness/sandbox_server.py` 自动起 opensandbox-server)。A true kernel sandbox (gVisor/Firecracker) is still out of scope — Linux-only, deferred。kill-switch:`policy.yaml` 的 `executor.backend=native` 回 NativeExecutor。
- Wiring `cc_harness/memory/` into the live agent — the package exists (SQLite + embeddings) but is not yet imported by the ReAct loop. Treat session state as in-memory until it's wired.
- Concurrent tool calls (serial only)
- SubAgent / Agent Team (PDF 阶段 4-5, not started)
