# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this is

cc-harness 是一个**本地编程代理**:通过 OpenAI 兼容 LLM(默认配 DeepSeek)执行 ReAct 循环,工具来自 MCP server(fs/git)+ 内置 `run_command`,默认在浏览器 WebUI 里工作(事件流渲染,不再是终端 4 段输出)。

## 当前状态(2026-10,读代码前先看这段)

- **只有一条执行链路**:`main.py` → `cc_harness.entrypoint`(durable)→ `DurableRuntimeClient` → `RunWorker`。
  **产品侧 legacy 已删除**:`repl.py` / `runtime.py` / `tui/` / `project/subagent.py` 不再存在,
  REPL 交互层是 `durable_repl.py`(只调 durable 客户端)。
  `cc_harness/agent.py:run_turn` 保留为 **eval-only 引擎**:唯一消费者是 `eval/locomo/runner.py`
  (已发布 LoCoMo 数字绑定该循环,迁移会重新基线)。产品包 import 它 = guard test 失败
  (`tests/test_single_runtime.py`),预算由 `python scripts/legacy_engine_inventory.py --check` 强制
  (product_refs 必须 0,eval_refs=7,test_refs=165 只降不升);剩余计划见 `docs/runtime-legacy-migration.md`。
- **记忆已接入主链路**(CLAUDE.md 旧版说"未接入"是错的):`capability_runtime` 在 durable boot 时构造 memory deps;
  自动注入只到 L3 画像,`memory_recall` 工具负责按层取 L1/L0。legacy 的 REPL boot 已随 `repl.py` 删除。
- **测试**:`python -m pytest tests` 可一键跑;`tests/test_agentdojo_adapter.py` 在缺 `agentdojo` 时整体 skip
  (可选依赖:`pip install -e '.[benchmarks]'`)。完整套件本地实测 ~45 min,CI 上限已调到 75 min。
- 对外口径(数字/边界/已知问题)以 `README.md` + `docs/eval-evidence/` 为准,不要再引用旧版"RO mount""凭据不可达"等说法。

## Web UI

FastAPI + WebSocket 后端 + React/Vite 前端;WebUI 只读 durable 事件流,不拥有第二套执行引擎。
`cc-harness --no-open --port 3080` 启动后端,前端产物在 `web/dist/`(dev 手册见 `docs/web-frontend.md`)。

详见:
- 前端 dev 手册: `docs/web-frontend.md`
- runtime 全景: `docs/agent-runtime-full-flow.md`

## Common commands

```bash
# Install (pyproject.toml is the source of truth for deps)
pip install -e .                 # required by the eval provider (imports cc_harness)
pip install -e '.[dev]'          # pytest / pytest-asyncio / pytest-cov / ruff
pip install -e '.[benchmarks]'   # optional: agentdojo 等基准适配器

# Run (唯一入口:durable runtime;默认起 WebUI)
python main.py                       # 等价于 cc-harness
python main.py --no-open --port 3080
python main.py --command status      # 命令行控制面(status/list/attach/approve/...)

# Tests (~2,100 个 test 函数,217 个文件)
python -m pytest tests/                          # 全量(本地 ~45min;CI 上限 75min)
python -m pytest tests/ -x -q                    # 快速失败
python -m pytest tests/test_context.py -v        # 单文件
python -m pytest tests/test_policy.py::test_name # 单用例

# Lint
python -m ruff check cc_harness/ tests/

# Phase-1 regression(需要真实 LLM)
python run_verify.py

# Eval / red-team (见下方小节)
cd eval/promptfoo && npm run security
python eval/promptfoo/tools/run_eval.py all --keep-json
```

Force UTF-8 on Windows (avoids GBK crashes on 思考/✅/中文):
```bash
PYTHONIOENCODING=utf-8 .venv/Scripts/python.exe ...
```

## Architecture (data flow)

```
main.py                                  # 兼容 shim:只做 UTF-8 stdio + 委托
  └── cc_harness/entrypoint.py           # 唯一入口(durable)
        └── durable_runtime.py           # DurableRuntimeClient:submit/attach/approve
              ├── coordinator.py         # Run 状态机 + 事件
              ├── run_store.py           # append-only 事件 + lease/epoch 校验
              ├── supervisor.py          # 选可运行 run → claim
              └── worker.py:RunWorker    # 每 segment: 投影→压缩→LLM→策略→工具→落事件
                    ├── run_kernel.py    # ReAct 内核(budget / loop guard)
                    ├── capability_runtime.py:build_context  # 上下文装配
                    ├── context.py       # Snippet→Prune→Summary 三级压缩
                    ├── memory/          # L0-L3 分层记忆(已接入主链路)
                    ├── policy.py        # allow/ask/deny + hard-deny
                    ├── security.py      # 确定性参数 provenance
                    └── executor.py / sandbox.py  # 执行加固 / 容器
        (eval-only,产品不得 import) agent.py:run_turn → eval/locomo/runner.py
```

**Key data flow**:
- 持久状态是 `run_store` 的事件流;`messages` 是每个 segment 由投影重建的视图,不是唯一真相。
- 工具动作先过 `PolicyEngine.evaluate`,再走 `RunWorker` 的审批/租约,最后才 dispatch。
- 上下文压缩有独立 SQLite 版本库(lease + epoch),压缩结果按源 digest 幂等复用,并可 `read_ref` 回查原文。
- `tokens.py` 仍用于 5 桶统计,并与 provider 上报 usage 对账(`api_vs_breakdown_drift_pct`)。
- 工具结果统一在 `ToolResult.__post_init__` 做 head/tail 截断(默认 128K 字符,`CC_HARNESS_MAX_TOOL_CHARS` 可调),
  metadata 记 `tool_text_truncated` / `tool_text_chars`;classmethod 和直接构造都挡得住,避免一次巨量输出
  先污染事件日志、再由压缩来收拾。

## 2026-10 运行时加固(都有回归测试或定向验证)

- **`--max-iterations` 真的生效**:以前只解析不落地(活循环没有模型调用上限)。现在映射成 durable 预算
  `{"model_calls": N, "steps": N}`(`entrypoint._budget_limits`),默认 `None` 保持无上限;
  `--unbounded-iterations` 仍是显式无上限。
- **follow-up 启动竞态**:`enqueue`(coordinator/webui)与 `release_ready`(supervisor)可能同时启动同一
  queued item → `UNIQUE run_id` / `RunCreated can only be the first event`。`_start` 现在按 store 取进程级
  asyncio 锁,并把"run 已存在"的插入冲突视为并发启动成功。
- **租约心跳不再静默死**:`ResourceLeaseManager._heartbeat_loop` 遇到瞬时 store 错误会记日志并重试(3 次),
  只有被 fence 才停;`release` 的 `except (…, Exception)` 简化为 `suppress(Exception)` 并注明原因。
- **取消 watchdog 降载**:`RunWorker._cancellation_watchdog` 从"每 50 ms 全量重建 projection"改成
  "单行 `load_run_record` + 50→500 ms 退避",取消延迟仍 <0.5 s,写锁压力下降一个数量级。
- **workspace 快照移出事件循环**:`_workspace_snapshot`(逐文件 sha256)改用 `asyncio.to_thread`,
  不再阻塞心跳/取消/WebUI 流。

## Design decisions (non-obvious)

**3 modes, not just 1.** `mode in {coding, plan, design}` 是 legacy 引擎的粘性模式(plan/design 时
`tool_specs=None`,模型物理上发不出 tool_calls)。**产品已无 mode 选择**:`--mode` 仅为 eval launcher 兼容保留,
durable 自己决定交互模式。要改模式语义,先确认改的是 durable(`interaction_mode`)还是 eval-only `agent.py`。

**`run_command` is built-in, NOT via MCP.** Community shell MCP servers either don't work on Windows (`@kevinwatt/shell-mcp` uses `whereis`) or require LLM sampling we don't implement (`@mako10k/mcp-shell-server` enhanced mode). The native async subprocess in `tools.py` just works. Don't add an MCP shell server back without understanding why we removed it.

**Section pool, not a single string.** `prompts.py` has 10 sections in `SECTION_POOL` with conditions (`mode==coding`, `mode==plan`, `mode==design`, `has_tools`, `always`). To add a new section, register it in the pool — don't touch `build_system_prompt`.

**Safety is not a sandbox.** `is_dangerous` only matches a hardcoded regex list (rm -rf, format, drop table, fork bomb, shutdown, reboot). It's "prevent accidental mistakes" not security. Don't expand the regex list to be a permission system — that scope was explicitly cut. (The red-team eval below is how we actually measure safety.)

**L4 权限闸门(M1 起,持续加固)。** 派发点不再用 `is_dangerous` 正则当闸门,改用
`cc_harness/policy.py` 的三档引擎:**allow / ask / deny**。
工作区内读 → allow;执行/写/出站/未知工具 → ask;工作区外路径与敏感凭据路径 → **deny(hard,不可询问)**。
`is_dangerous` 保留但仅用于丰富 ask 原因。会话 allowlist 进程内、退出即失效,且**绕不过 hard-deny**。

2026-10 加固(有回归测试):
- **路径包含递归化**:`_extract_paths` 递归遍历参数,已知 path key 与"长得像路径"的未知 key(`target`、
  `options.path`)都进 hard-deny。此前只查固定 key,换 key 名就能读 `~/.ssh`。
- **写动作优先于读子串**:工具名里出现 write/delete/push/commit/apply 等动词即降级为 ask,
  防止不可信 MCP server 用 `evil_filesystem_read_all` / `git_list_and_push` 把自己命名进免审档。
- **工作区不能给自己松绑**:`load_policy_config` / `load_l2_config` / `load_l5_config` /
  `load_executor_config` 会把落在 cwd 内的 `policy.yaml` 视为"数据"——它可以收紧,但不能把
  `enabled` / `l2.enabled` / `l5.enabled` 设成 false,也不能把 `executor.backend` 降级成 native;
  被拒的字段记在 `workspace_overrides_rejected` 并打 warning。显式信任要 `CC_HARNESS_TRUST_WORKSPACE_POLICY=1`
  (`tests/test_workspace_policy_trust.py` 覆盖)。`mcp.json` 仍是用户信任的代码,不受此约束。
- **残余风险(已知)**:分类仍主要靠名字 + 声明 capability;`mcp.json` 从工作区读取,
  克隆不可信仓库时应先审阅或用 `hardened` profile。

执行加固(cwd 锁/env 剥离/超时/进程树 kill)在 `cc_harness/executor.py`。审计落 `<root>/logs/policy.jsonl`。
完整设计见 docs/superpowers/specs/2026-06-30-l4-policy-engine-design.md。

**L2 输入防御(M2,2026-07-01)。** 入口在 durable 的 `capability_runtime`(提交 run 时、进入 kernel 之前)过两道:
① `cc_harness/l2.py:heuristic_check`(传统正则,命中即拦,零延迟);
② DeepSeek judge(复用 provider,结构化 JSON 分类 benign/injection/jailbreak)。
命中即**硬阻断**:不进主 LLM、不调工具、**不入 messages 历史**(切断上下文传播),
打模糊拒绝模板(不透露检测原因,避免帮攻击者迭代)。真实原因落审计。
指令层级(`prompts.py:instruction_hierarchy`,始终生效):`<user_input>` 包用户输入、
`<untrusted>` 包外部工具输出,声明开发者>用户>工具返回。
judge 失败 fail-open(`judge_error` 审计,L4 兜底)。kill-switch:`policy.yaml` 的 `l2.enabled=false`
(仅用户级配置生效;工作区内的 policy 不能关,见 L4 一节)。
**长输入不再绕过**:正则层跑全文(线性 pattern,`MAX_HEURISTIC_SCAN_LEN` 只兜超大病态输入,超限改为头+尾扫描),
语义 judge 只看 head+tail(`MAX_JUDGE_INPUT_CHARS=24K`),截断会写进审计 reason(`judge_input_truncated`)。
**无 key 退化**:judge 仅在配置了 `OPENAI_API_KEY` 时构造 client;无 key 时 `l2_client=None`,
judge 路径 fail-open(等价 heuristic-only,审计记 `judge_error:AttributeError`),heuristic 第一道仍生效。
完整设计见 docs/superpowers/specs/2026-07-01-l2-input-defense-design.md。

**L5 输出 DLP(M3,2026-07-01)。** 与 M2(L2 输入)对称,守**输出**:durable 侧在 assistant 文本被
提交为事件之前过 `cc_harness/l5.py`(`capability_runtime` 调用);eval-only 的 `agent.run_turn` 走同一层。
分层检测:① Layer A 密钥正则(`KeyRegexLayer`,零依赖,永远在,已知格式:OpenAI/AWS/GitHub/GitLab/Slack/Google/PEM/JWT);
② Layer B Presidio PII(`PresidioLayer`,可选 `pip install -e '.[dlp]'`,邮箱 + 中文手机/身份证 custom recognizer)。
命中替换为 `[REDACTED:<type>]`,**历史也存脱敏版**(切断"思考读到→结果复述"二段泄露)。
原则:**宁漏勿误**——不做泛化高熵串检测(会误伤 SHA256/UUID/base64/commit hash)。
fail-soft:Presidio 不可用 → 自动只跑 Layer A(密钥仍护);scan 异常 → 原文返回(DLP 不把 Agent 弄哑)。
静默脱敏 + 审计落 `<root>/logs/l5.jsonl`(`action=l5_redact`,只记类型计数,**绝不记明文**)。
kill-switch:`policy.yaml` 的 `l5.enabled=false`。工具观察段不扫(M2 已用 `<untrusted>` 隔离;脱敏它破坏 coding)。
完整设计见 docs/superpowers/specs/2026-07-01-l5-output-dlp-design.md。

**Windows GBK fix at the top of `main.py` must stay.** Without `sys.stdin.reconfigure(encoding="utf-8")` (and the same for stdout/stderr), the GBK default codepage crashes on the first non-ASCII char the LLM outputs (✅, 中文, 思考, etc.).

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
- **Eval-v2 (2026-07-28,14 task) 在原 red-team 上加了 5 个能力**(详见 `docs/eval-methodology.md`):
  - **Trajectory + outcome 双覆盖**:`python main.py --emit-events <path>` 把 Thought/Action/Observation/Result 落 JSONL,`wrappers/cc_harness.py` 把步数/工具错误/borderline 摘要塞进 judge input,`report_to_md.py` 失败/通过表加 3 列。独立可视化:`python eval/promptfoo/tools/trajectory_to_html.py <dir> -o trajectory-report.html`(零依赖,不碰 web/src)。
  - **Pass^k + Wilson CI**:`promptfooconfig.unified.yaml` 15 处 critical attack 加 `repeat: 5`,`report_to_md.py:wilson_ci + aggregate_repeats` 出 `hold^k / σ / 95%CI` 表(n=5 CI 仍宽,诚实呈现)。
  - **Multi-source judge**:`promptfooconfig.unified.yaml` defaultTest 加第二源 MiMo(`openai:mimo-v2.5-pro` + `MIMO_API_KEY` 兜底 `OPENAI_API_KEY`),`report_to_md.py:judge_agreement` 算一致率 + Δ>0.3 标 `⚠ judge 分歧` 送人工。
  - **Gold-set + κ + 回归**:`judges/calibration_set.yaml` 双区(50 baseline + pending),`tools/calibrate.py` 三件套 `cohen_kappa` / `collect_failures` / `regression_run` + `report_to_md.py` 末尾渲染校准+回归段。50 baseline 全 placeholder(`# TODO: 人工校准`),当前 κ=1.0 trivial;真实校准后才有判别力。
  - **Cyber 6 子类 + Locomo 桥接**:`attacks.yaml` 加 cyber 6 子类各 ≥5(privilege-escalation / lateral-movement / persistence / data-exfiltration / credential-theft / supply-chain),`defense_matrix.yaml` 登记 6 子类 layer。Locomo memory 5-key(recall/timeliness/utilization/compaction/consistency)经 `render_locomo_section` 桥接到 `unified-report.md`;记忆现已接入主链路,LoCoMo 独立报告见 `eval/result/cc-only/locomo-memory/.../report.md`(tracked),但 `run_eval.py:_run_locomo` 的 unified 桥接仍是 TODO。

## Test conventions

- 2,100+ 个 test 函数、217 个 `tests/test_*.py`(默认 pattern 收集)。其中 **15 个文件 / 165 处引用**驱动
  eval-only 的 `run_turn`;预算由 `python scripts/legacy_engine_inventory.py --check` 守着(product_refs 必须 0,
  test_refs/eval_refs 只降不升),**新测试一律对准 durable**;批次计划见 `docs/runtime-legacy-migration.md`。Eval tooling 自带测试:`test_generate_attacks.py`,
  `test_curate_attacks.py`, `test_dedup_logic.py`, `test_report_to_md.py`, `test_run_eval.py`,
  `test_smoke_curate.py`, `test_strategies_yaml.py`, `test_cc_harness_wrapper.py`, `test_tokens.py`,
  `test_defense_matrix.py`, `test_attacks_yaml.py`, `test_promptfoo_configs.py`。
- `tests/_test_*.py` (leading underscore) are **integration tests requiring a real LLM** — not collected by pytest by default; only run manually
- Test agents use `FakeLLM` (pre-programmed stream events) + `FakeMCP` (pre-programmed tool results), defined in `test_agent.py` and reused via imports
- New test file naming: `test_<module>.py`, mirror source module names
- 交互层测试:改 `durable_repl` 时 mock `_read_user`,不要 patch `builtins.input`(子进程测试里太脆)。
- legacy `run_turn` 测试的夹具(`FakeLLM` / `FakeMCP` / `FakeStreamEvent`)仍定义在 `test_agent.py`,
  由 15 个文件 import;删除引擎时它们一起走。

## Config & files

- `.env` — core (3 required, no defaults — see `config.py`): `OPENAI_API_KEY`, `OPENAI_BASE_URL`, `OPENAI_MODEL`. Eval + memory add: `EMBEDDING_BASE_URL` / `EMBEDDING_API_KEY` / `EMBEDDING_MODEL` / `EMBEDDING_DIM` (SiliconFlow `BAAI/bge-m3`, dim 1024), `JUDGE_MODEL`, and `PROMPTFOO_API_KEY` (OWASP plugins only). CI builds these into `eval/promptfoo/.env.ci` from secrets.
- `pyproject.toml` — deps + packaging (`pip install -e .`). pytest config: `asyncio_mode = "auto"`, `testpaths = ["tests"]`. ruff: line-length 100, target py311.
- `mcp.json`: MCP server entries. Per-server failures are isolated — one bad server logs a red warning, the rest still boot (`init_timeout_s` defaults to 30s in `mcp_client.py`). The bundled config mixes stdio (npx-launched: filesystem, playwright) and SSE (ModelScope-hosted: fetch + others) transports. Tool names are exposed to the LLM as `mcp__{server}__{tool}`.
- `~/.cc-harness/designs/`: design-mode artifacts 的旧落盘目录;design 模式随 legacy 引擎一起移除,新代码不要再写这里。
- `docs/superpowers/` — planning artifacts for the superpowers workflow: `plans/<date>-<slug>.md` and `specs/<date>-<slug>-design.md` per feature (e.g. context-compaction, real-token-tracking, dynamic-attack-generation, severity-redesign). Read the matching plan before extending that feature.
- `run_verify.py` (root): Phase-1 regression script — spawns the CLI as a subprocess, pipes one command in, captures output, exits. Useful as an end-to-end smoke after a refactor. Requires a real LLM (hits the configured provider).
- `eval/bug/`: captured red-team failure artifacts from past runs (result JSONs, screenshots, reports) — debugging evidence, not test fixtures.

## Out of scope (don't add unless asked)

- Multi-LLM backend switching (locked to OpenAI-compatible)
- 真正的内核级沙箱(gVisor/Firecracker)—— 现有 OpenSandbox 是用户态容器:`cc_harness/sandbox.py:SandboxExecutor`
  会话级 lazy create,**workspace 是 RW 挂载**(不是 RO,旧文档写错了),敏感路径用空 RO overlay 遮罩;
  Docker 不可用时 `standard` profile **降级 native 并审计**,`hardened` / `benchmark-one-shot` 直接失败。
  kill-switch:`CC_HARNESS_EXECUTOR_BACKEND=native` 显式选择(工作区 policy.yaml 不能把 backend 降级)。
- 不要新增第三套执行引擎:产品是 Durable Runtime,eval-only 的 `agent.run_turn` 是唯一例外(见迁移文档)。
- 不要把 `agent.run_turn` 重新接回产品路径:`tests/test_single_runtime.py` + 预算脚本会失败。
- 并发工具调用(目前串行)
- SubAgent / Agent Team(已有 `dispatch_subagent`,但仍是串行子任务,不做多 agent 编排)
