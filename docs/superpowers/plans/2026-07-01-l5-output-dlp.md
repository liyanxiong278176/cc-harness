# L5 输出 DLP(分层脱敏引擎)Implementation Plan (M3)

> **For agentic workers:** REQUIRED SUB-SKKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 在 LLM 主动产生的文本(思考段 / 结果段)被打印或写入 `messages` 历史**之前**,扫描并脱敏敏感数据(密钥 Layer A 正则 + PII Layer B Presidio),替换成 `[REDACTED:<type>]`。历史也存脱敏版,切断"思考读到 → 结果复述"的二段泄露。与 M2(L2 输入)对称:输入守门、输出守口。

**Architecture:** 新增 `cc_harness/l5.py`:`Finding` / `Layer` 协议 / `KeyRegexLayer`(Layer A,零依赖,永远在)/ `PresidioLayer`(Layer B,可选,fail-soft)/ `L5Engine.scan` / `ScanOutcome` / `build_l5_engine` 工厂。`agent.py:run_turn` 加 `l5` 参数 + `_redact` 闭包(脱敏 + 审计),在 4 个 LLM-content 打印点接入。`repl.py` 构造引擎注入。`config.py` 加 `L5Config`(读 `policy.yaml` 的 `l5:` 段)。`pyproject.toml` 加 `[dlp]` optional extra。红队无需改:脱敏发生在 `print_result` 前 → wrapper 提取到 `[REDACTED:...]` 而非明文 → judge 判 hold ground。

**Tech Stack:** Python 3.11、pydantic 2、PyYAML(已有)、`presidio-analyzer`(optional `[dlp]` extra,不装也能跑)、pytest/pytest-asyncio(已有)。

**Spec:** `docs/superpowers/specs/2026-07-01-l5-output-dlp-design.md`

---

## 文件结构

| 文件 | 职责 | 创建/改 |
|---|---|---|
| `cc_harness/config.py` | `L5Config` + `load_l5_config`(读 `policy.yaml` 的 `l5:` 段) | 改 |
| `cc_harness/l5.py` | `Finding`、`Layer`、`KeyRegexLayer`、`PresidioLayer`、`L5Engine`、`ScanOutcome`、`sanitize`、`build_l5_engine`、`_maybe_build_pii_layer` | 创建 |
| `cc_harness/agent.py` | `run_turn` +`l5` 参数 + `_redact` 闭包;4 个 LLM-content 打印点接入 | 改 |
| `cc_harness/repl.py` | 构造 `build_l5_engine` + thread 进 `run_turn` | 改 |
| `pyproject.toml` | `+ [dlp] optional extra` | 改 |
| `policy.yaml.example` | `+ l5:` 段示例 | 改 |
| `CLAUDE.md` | L5 设计决策段 | 改 |
| `tests/test_l5.py` | Layer A / Engine / sanitize / fail-soft / 审计 单测 | 创建 |
| `tests/test_config.py` `tests/test_agent.py` `tests/test_repl.py` | 扩展 | 改 |

依赖链(无环):`config` →(无);`l5` → `config`(L5Config);`agent` → `l5`(L5Engine 类,运行时鸭子,无环);`repl` → `l5` + `config`。

---

## Task 1: L5Config + load_l5_config

**Files:**
- Modify: `cc_harness/config.py`(末尾追加)
- Test: `tests/test_config.py`(扩展现有)

- [ ] **Step 1: 写失败测试**

在 `tests/test_config.py` 末尾加:
```python
def test_l5config_defaults():
    from cc_harness.config import L5Config
    c = L5Config()
    assert c.enabled is True
    assert c.keys_on is True
    assert c.pii_on is True


def test_load_l5_config_reads_l5_section(tmp_path):
    from cc_harness.config import load_l5_config
    y = tmp_path / "policy.yaml"
    y.write_text(
        "l2:\n  enabled: false\n"                       # l2 段,不影响 l5
        "l5:\n  enabled: false\n  keys_on: false\n  pii_on: false\n",
        encoding="utf-8",
    )
    c = load_l5_config(y)
    assert c.enabled is False        # l5 独立于 l2
    assert c.keys_on is False
    assert c.pii_on is False


def test_load_l5_config_independent_from_l2(tmp_path):
    from cc_harness.config import load_l5_config
    y = tmp_path / "policy.yaml"
    y.write_text("l2:\n  enabled: false\n", encoding="utf-8")  # 只配 l2
    c = load_l5_config(y)
    assert c.enabled is True and c.keys_on is True   # l5 段缺失 → 默认全开


def test_load_l5_config_missing_file_returns_defaults(tmp_path):
    from cc_harness.config import load_l5_config
    c = load_l5_config(tmp_path / "nope.yaml")
    assert c.keys_on is True and c.pii_on is True
```

- [ ] **Step 2: 跑测试确认失败**

Run: `cd D:/agent_learning/cc-harness && PYTHONIOENCODING=utf-8 .venv/Scripts/python.exe -m pytest tests/test_config.py -k l5 -v`
Expected: FAIL(`ImportError: cannot import L5Config` 的 l5 变体 / AttributeError)。

- [ ] **Step 3: 实现**

在 `cc_harness/config.py` 末尾追加:
```python
class L5Config(BaseModel):
    """L5 输出 DLP 配置。从 policy.yaml 的 `l5:` 段读;缺省全开。"""
    enabled: bool = True
    keys_on: bool = True    # Layer A 密钥正则(零依赖)
    pii_on: bool = True     # Layer B Presidio PII(可选;失败自动退化)

    model_config = {"extra": "ignore"}


def load_l5_config(path: Path) -> L5Config:
    """读 policy.yaml 的 `l5:` 子段(与 L2/L4 独立)。文件/段缺失→默认。"""
    if not path.exists():
        return L5Config()
    import yaml
    raw = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    return L5Config(**(raw.get("l5") or {}))
```

- [ ] **Step 4: 跑测试确认通过**

Run: `cd D:/agent_learning/cc-harness && PYTHONIOENCODING=utf-8 .venv/Scripts/python.exe -m pytest tests/test_config.py -v`
Expected: 既有 + 4 新增全 passed。

- [ ] **Step 5: Commit**

```bash
git add cc_harness/config.py tests/test_config.py
git commit -m "feat(l5): add L5Config + load_l5_config (reads policy.yaml l5: section)"
```

---

## Task 2: l5.py — Layer A 密钥正则 + L5Engine + fail-soft 工厂

> 只做 Layer A(零依赖,永远在)。Layer B(Presidio)在 Task 3 接入;本任务 `_maybe_build_pii_layer` 先 stub 返回 None,Task 3 替换实现。

**Files:**
- Create: `cc_harness/l5.py`
- Test: `tests/test_l5.py`

- [ ] **Step 1: 写失败测试**

`tests/test_l5.py`:
```python
import pytest

from cc_harness.l5 import (
    Finding, KeyRegexLayer, L5Engine, ScanOutcome, sanitize, build_l5_engine,
)
from cc_harness.config import L5Config


# --- KeyRegexLayer 命中(各密钥格式)---

def test_key_openai_sk_legacy():
    f = KeyRegexLayer().find("my key is sk-" + "a" * 48 + " ok")
    assert f and f[0].type == "api_key"


def test_key_openai_sk_proj():
    f = KeyRegexLayer().find("sk-proj-" + "x" * 22)
    assert f and f[0].type == "api_key"


def test_key_aws_access_key():
    f = KeyRegexLayer().find("AKIA" + "ABCDEFGH" + "12345678")  # AKIA + 16
    assert f and f[0].type == "aws_access_key"


def test_key_github_pat():
    f = KeyRegexLayer().find("ghp_" + "a" * 36)
    assert f and f[0].type == "github_token"


def test_key_gitlab():
    f = KeyRegexLayer().find("glpat-" + "a" * 20)
    assert f and f[0].type == "gitlab_token"


def test_key_google():
    f = KeyRegexLayer().find("AIza" + "a" * 35)
    assert f and f[0].type == "google_api_key"


def test_key_pem_block_multiline():
    txt = "header\n-----BEGIN RSA PRIVATE KEY-----\nMIIEpAIBAAKCAQEA...\n-----END RSA PRIVATE KEY-----\ntail"
    f = KeyRegexLayer().find(txt)
    assert f and f[0].type == "private_key"
    assert f[0].start < f[0].end


def test_key_jwt():
    f = KeyRegexLayer().find("tok eyJhbGci.eyJzdWIi.sigpart end")
    assert f and f[0].type == "jwt"


# --- 不误伤(G6 宁漏勿误:关键)---

def test_no_false_positive_sha256():
    sha = "e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855"
    assert KeyRegexLayer().find(f"hash {sha} end") == []


def test_no_false_positive_base64_data():
    assert KeyRegexLayer().find("data:image/png;base64," + "A" * 100) == []


def test_no_false_positive_uuid():
    assert KeyRegexLayer().find("uuid 550e8400-e29b-41d4-a716-446655440000 end") == []


def test_no_false_positive_commit_hash():
    assert KeyRegexLayer().find("commit 83e2816aabbccd here") == []


def test_no_false_positive_varname_sklearn():
    # sklearn 不含 sk-(连字符),不该命中
    assert KeyRegexLayer().find("import sklearn") == []


# --- L5Engine.scan ---

def test_scan_redacts_and_counts():
    eng = build_l5_engine(L5Config())
    out = eng.scan("key sk-" + "a" * 48)
    assert out.sanitized_text == "key [REDACTED:api_key]"
    assert out.findings == {"api_key": 1}


def test_scan_idempotent():
    """G6:脱敏后的 [REDACTED:api_key] 再扫不再命中(天然幂等,锁住 jwt 边界)。"""
    eng = build_l5_engine(L5Config())
    once = eng.scan("sk-" + "a" * 48)
    twice = eng.scan(once.sanitized_text)
    assert twice.sanitized_text == once.sanitized_text
    assert twice.findings == {}


def test_scan_jwt_idempotent():
    """jwt 前缀 eyJ 最易在 redaction 边界误触,单独锁。"""
    eng = build_l5_engine(L5Config())
    once = eng.scan("eyJhbGci.eyJzdWIi.sigpart")
    twice = eng.scan(once.sanitized_text)
    assert twice.findings == {}


def test_scan_multiple_types_disjoint():
    eng = build_l5_engine(L5Config())
    out = eng.scan("a sk-" + "b" * 48 + " and AKIA" + "C" * 16)
    assert out.findings.get("api_key") == 1
    assert out.findings.get("aws_access_key") == 1
    assert "[REDACTED:api_key]" in out.sanitized_text
    assert "[REDACTED:aws_access_key]" in out.sanitized_text


def test_scan_empty_and_none():
    eng = build_l5_engine(L5Config())
    assert eng.scan("").sanitized_text == ""
    out = eng.scan(None)  # type: ignore[arg-type]
    assert out.sanitized_text == ""


def test_scan_no_hit_passthrough():
    eng = build_l5_engine(L5Config())
    out = eng.scan("普通代码 print('hello')")
    assert out.sanitized_text == "普通代码 print('hello')"
    assert out.findings == {}


# --- sanitize 便捷 ---

def test_sanitize_none_engine_passthrough():
    raw = "sk-" + "a" * 48
    assert sanitize(raw, None) == raw              # engine=None → 不脱敏


def test_sanitize_empty_passthrough():
    eng = build_l5_engine(L5Config())
    assert sanitize("", eng) == ""


def test_sanitize_redacts():
    eng = build_l5_engine(L5Config())
    assert "[REDACTED:api_key]" in sanitize("x sk-" + "a" * 48 + " y", eng)


# --- build_l5_engine / kill-switch / per-layer 开关 ---

def test_build_disabled_returns_none():
    assert build_l5_engine(L5Config(enabled=False)) is None


def test_build_keys_off_skips_layer_a():
    """keys_on=False → Layer A 不装 → 密钥不命中(红队隔离测 Layer A)。"""
    eng = build_l5_engine(L5Config(keys_on=False))
    out = eng.scan("sk-" + "a" * 48)
    assert out.findings == {}
    assert out.sanitized_text == "sk-" + "a" * 48


def test_build_pii_active_false_when_presidio_missing(monkeypatch):
    """Task 2 阶段(及任何没装 presidio 的环境)→ pii_active=False。"""
    import sys
    monkeypatch.setitem(sys.modules, "presidio_analyzer", None)
    from cc_harness import l5 as l5_mod
    eng = l5_mod.build_l5_engine(L5Config())
    assert eng.pii_active is False
    # Layer A 仍工作
    assert eng.scan("sk-" + "a" * 48).findings == {"api_key": 1}
```

- [ ] **Step 2: 跑测试确认失败**

Run: `cd D:/agent_learning/cc-harness && PYTHONIOENCODING=utf-8 .venv/Scripts/python.exe -m pytest tests/test_l5.py -v`
Expected: FAIL(`ModuleNotFoundError: cc_harness.l5`)。

- [ ] **Step 3: 实现**

`cc_harness/l5.py`:
```python
"""L5 输出 DLP:对 LLM 主动产生的文本(思考/结果)脱敏,防敏感数据外泄。
分层:Layer A 密钥正则(零依赖,永远在)+ Layer B Presidio PII(可选,fail-soft)。
命中片段替换成 [REDACTED:<type>];历史也存脱敏版(切断二段泄露)。
"""
from __future__ import annotations
import re
from dataclasses import dataclass, field

from cc_harness.config import L5Config


@dataclass
class Finding:
    """文本中一个命中片段的字符 span。type 用作 [REDACTED:type] 标签。"""
    start: int
    end: int
    type: str
    score: float = 1.0


class Layer:
    """检测器层协议:find 返回文本中所有命中(字符 span)。"""
    def find(self, text: str) -> list[Finding]:  # pragma: no cover - protocol
        raise NotImplementedError


# --- Layer A: 密钥正则(零依赖,永远在)---
# 宁漏勿误:只匹配已知前缀/结构。不做泛化高熵串检测(会误伤 SHA256/UUID/base64)。
_KEY_PATTERNS: list[tuple[re.Pattern[str], str]] = [
    (re.compile(r"sk-proj-[A-Za-z0-9_-]{20,}"), "api_key"),
    (re.compile(r"\bsk-[A-Za-z0-9]{40,}\b"), "api_key"),
    (re.compile(r"\bAKIA[0-9A-Z]{16}\b"), "aws_access_key"),
    (re.compile(r"\bgh[pousr]_[A-Za-z0-9]{36}\b"), "github_token"),
    (re.compile(r"github_pat_[A-Za-z0-9_]{60,}"), "github_token"),
    (re.compile(r"\bglpat-[A-Za-z0-9_-]{20}\b"), "gitlab_token"),
    (re.compile(r"\bxox[baprs]-[A-Za-z0-9-]{10,}\b"), "slack_token"),
    (re.compile(r"\bAIza[0-9A-Za-z_-]{35}\b"), "google_api_key"),
    # PEM 私钥块:DOTALL 跨行,非贪婪到匹配的 END 行(避免贪心吞掉后续输出)
    (re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----.*?-----END [A-Z ]*PRIVATE KEY-----", re.DOTALL), "private_key"),
    (re.compile(r"\beyJ[A-Za-z0-9_-]+\.eyJ[A-Za-z0-9_-]+\.[A-Za-z0-9_-]*\b"), "jwt"),
]


class KeyRegexLayer(Layer):
    """Layer A:已知格式密钥正则。零依赖,永不失败。"""
    def find(self, text: str) -> list[Finding]:
        if not isinstance(text, str) or not text:
            return []
        out: list[Finding] = []
        for pat, typ in _KEY_PATTERNS:
            for m in pat.finditer(text):
                out.append(Finding(m.start(), m.end(), typ, 1.0))
        return out


@dataclass
class ScanOutcome:
    sanitized_text: str
    findings: dict[str, int] = field(default_factory=dict)  # {type: count},不记明文
    pii_active: bool = False


def _merge_and_redact(text: str, findings: list[Finding]) -> tuple[str, dict[str, int]]:
    """合并 spans,重叠取最早开始的(丢嵌套/后到的),倒序 replace 成 [REDACTED:type]。
    Layer A 与 Layer B 命中重叠时,先到的胜出(确定性,审计计数可复现)。
    返回 (redacted_text, {type: count})。"""
    if not findings:
        return text, {}
    findings = sorted(findings, key=lambda f: (f.start, -(f.end - f.start)))
    kept: list[Finding] = []
    last_end = -1
    for f in findings:
        if f.start >= last_end:     # 不与已保留的重叠
            kept.append(f)
            last_end = f.end
    counts: dict[str, int] = {}
    out = text
    for f in sorted(kept, key=lambda f: -f.start):   # 倒序,索引不漂移
        out = out[:f.start] + f"[REDACTED:{f.type}]" + out[f.end:]
        counts[f.type] = counts.get(f.type, 0) + 1
    return out, counts


class L5Engine:
    """扫描+脱敏引擎。layers:活跃检测层(默认含 Layer A)。pii_active:Layer B 是否装上。"""
    def __init__(self, *, layers: list[Layer], pii_active: bool) -> None:
        self.layers = layers
        self.pii_active = pii_active

    def scan(self, text: str) -> ScanOutcome:
        """跑所有 layer 的 find → 合并脱敏。任何异常 fail-open 返回原文(DLP 不把 Agent 弄哑)。"""
        if not isinstance(text, str):
            return ScanOutcome("", {}, self.pii_active)
        if not text:
            return ScanOutcome(text, {}, self.pii_active)
        try:
            findings: list[Finding] = []
            for layer in self.layers:
                findings.extend(layer.find(text))
            redacted, counts = _merge_and_redact(text, findings)
            return ScanOutcome(redacted, counts, self.pii_active)
        except Exception:
            # fail-open:scan 异常时原文返回(审计层若接入会记 scan_error)。
            return ScanOutcome(text, {}, self.pii_active)


def sanitize(text: str, engine: L5Engine | None) -> str:
    """便捷:engine=None/非 str/空 → 原文直通;否则返回 sanitized_text。"""
    if engine is None or not isinstance(text, str) or not text:
        return text
    return engine.scan(text).sanitized_text


def _maybe_build_pii_layer(cfg: L5Config) -> Layer | None:
    """Layer B (Presidio PII) — Task 3 实现。本任务 stub:始终返回 None。"""
    return None


def build_l5_engine(cfg: L5Config) -> L5Engine | None:
    """工厂。enabled=False → None(原文直通)。
    keys_on 控制 Layer A;pii_on 控制 Layer B(失败自动退化到 Layer A)。"""
    if not cfg.enabled:
        return None
    layers: list[Layer] = []
    if cfg.keys_on:
        layers.append(KeyRegexLayer())
    pii = _maybe_build_pii_layer(cfg)
    if pii is not None:
        layers.append(pii)
    return L5Engine(layers=layers, pii_active=pii is not None)
```

- [ ] **Step 4: 跑测试确认通过**

Run: `cd D:/agent_learning/cc-harness && PYTHONIOENCODING=utf-8 .venv/Scripts/python.exe -m pytest tests/test_l5.py -v`
Expected: 全 passed(~23 条)。

- [ ] **Step 5: Commit**

```bash
git add cc_harness/l5.py tests/test_l5.py
git commit -m "feat(l5): add l5.py — KeyRegexLayer (Layer A) + L5Engine + fail-soft factory"
```

---

## Task 3: l5.py — Layer B Presidio PII + 中文 recognizer

> 可选增强。`presidio-analyzer` 在 `[dlp]` extra(T6 加)。导入/初始化失败 → 工厂退化 Layer A only(G5 fail-soft)。测试用 mock,不依赖真实 presidio(CI 可能没装)。

**Files:**
- Modify: `cc_harness/l5.py`(替换 `_maybe_build_pii_layer` stub + 加 `PresidioLayer` / `_build_cn_recognizers`)
- Test: `tests/test_l5.py`(扩展)

- [ ] **Step 1: 写失败测试**

在 `tests/test_l5.py` 末尾加:
```python
def test_maybe_pii_layer_returns_none_when_disabled():
    from cc_harness.l5 import _maybe_build_pii_layer
    assert _maybe_build_pii_layer(L5Config(pii_on=False)) is None


def test_maybe_pii_layer_fails_soft_when_presidio_missing(monkeypatch):
    """presidio_analyzer 不可用 → _maybe_build_pii_layer 返回 None,不抛。"""
    import sys
    monkeypatch.setitem(sys.modules, "presidio_analyzer", None)
    from cc_harness import l5 as l5_mod
    # 清掉可能缓存的 PresidioLayer 引用,强制重新走 import 路径
    assert l5_mod._maybe_build_pii_layer(L5Config(pii_on=True)) is None


def test_presidio_layer_find_maps_to_findings():
    """mock AnalyzerEngine → 验证 PresidioLayer.find 把 entity 映射成 Finding(不依赖真实 presidio)。"""
    from cc_harness import l5 as l5_mod

    class _FakeResult:
        def __init__(self, start, end, entity_type, score):
            self.start, self.end, self.entity_type, self.score = start, end, entity_type, score

    class _FakeRegistry:
        def add_recognizer(self, r):  # noqa: ARG002
            pass

    class _FakeAnalyzer:
        def __init__(self):
            self.registry = _FakeRegistry()

        def analyze(self, *, text, entities, language):  # noqa: ARG002
            return [_FakeResult(0, 9, "EMAIL_ADDRESS", 0.9),
                    _FakeResult(20, 31, "CN_PHONE", 0.9)]

    # 绕过 __init__(它会真 import presidio),直接注入 fake analyzer
    layer = l5_mod.PresidioLayer.__new__(l5_mod.PresidioLayer)
    layer._analyzer = _FakeAnalyzer()      # type: ignore[attr-defined]
    layer._entities = ["EMAIL_ADDRESS", "CN_PHONE"]  # type: ignore[attr-defined]

    findings = layer.find("a@b.com and 13800138000")
    types = sorted(f.type for f in findings)
    assert types == ["cn_phone", "email"]
    assert findings[0].start == 0 and findings[0].end == 9


def test_build_engine_pii_active_when_layer_constructs(monkeypatch):
    """_maybe_build_pii_layer 返回非 None 时,engine.pii_active=True。"""
    from cc_harness import l5 as l5_mod
    from cc_harness.l5 import KeyRegexLayer

    class _DummyLayer(KeyRegexLayer):  # 复用 KeyRegexLayer 作占位(实现 Layer 协议)
        pass

    monkeypatch.setattr(l5_mod, "_maybe_build_pii_layer", lambda cfg: _DummyLayer())
    eng = l5_mod.build_l5_engine(L5Config())
    assert eng.pii_active is True
    assert len(eng.layers) == 2
```

- [ ] **Step 2: 跑测试确认失败**

Run: `cd D:/agent_learning/cc-harness && PYTHONIOENCODING=utf-8 .venv/Scripts/python.exe -m pytest tests/test_l5.py -k "pii or presidio or maybe" -v`
Expected: FAIL(`_maybe_build_pii_layer` 还是 stub,`pii_active` 永远 False;`PresidioLayer` 不存在)。

- [ ] **Step 3: 实现**

在 `cc_harness/l5.py` **替换** `_maybe_build_pii_layer` stub,并在其上方加 `PresidioLayer` + `_build_cn_recognizers`:
```python
def _build_cn_recognizers() -> list:
    """中文 custom recognizer(Presidio PatternRecognizer)。需 presidio_analyzer 已 import。"""
    from presidio_analyzer import Pattern, PatternRecognizer
    cn_phone = PatternRecognizer(
        supported_entity="CN_PHONE",
        patterns=[Pattern(r"\b1[3-9]\d{9}\b", 0.9)],
    )
    cn_id = PatternRecognizer(
        supported_entity="CN_ID_CARD",
        # 18 位:6 地区码 + 4 年 + 2 月 + 2 日 + 3 序号 + 1 校验(X/x)
        patterns=[Pattern(
            r"\b[1-9]\d{5}(?:19|20)\d{2}(?:0[1-9]|1[0-2])(?:0[1-9]|[12]\d|3[01])\d{3}[\dXx]\b",
            0.85,
        )],
    )
    return [cn_phone, cn_id]


# Presidio entity type → L5 type 标签
_PII_TYPE_MAP = {
    "EMAIL_ADDRESS": "email",
    "PHONE_NUMBER": "phone",
    "CN_PHONE": "cn_phone",
    "CN_ID_CARD": "cn_id_card",
}


class PresidioLayer(Layer):
    """Layer B:Presidio PII(邮箱 + 中文手机/身份证)。
    build 失败(无 presidio / 无 spacy 模型 / 初始化抛错)→ 工厂退化 Layer A only。
    NER(姓名/地址)不强制:无 spacy 模型时内置正则 recognizer 仍覆盖邮箱/手机/身份证。"""

    def __init__(self) -> None:
        from presidio_analyzer import AnalyzerEngine
        # 默认 AnalyzerEngine:无 spacy 模型时 Presidio 打印 warning 但仍跑 regex recognizer。
        self._analyzer = AnalyzerEngine()
        for r in _build_cn_recognizers():
            self._analyzer.registry.add_recognizer(r)
        self._entities = ["EMAIL_ADDRESS", "PHONE_NUMBER", "CN_PHONE", "CN_ID_CARD"]

    def find(self, text: str) -> list[Finding]:
        if not isinstance(text, str) or not text:
            return []
        results = self._analyzer.analyze(
            text=text, entities=self._entities, language="en",
        )
        out: list[Finding] = []
        for r in results:
            typ = _PII_TYPE_MAP.get(r.entity_type, r.entity_type.lower())
            out.append(Finding(r.start, r.end, typ, float(r.score)))
        return out


def _maybe_build_pii_layer(cfg: L5Config) -> Layer | None:
    """Layer B 可选。pii_on=False 或 presidio 导入/初始化失败 → None(Layer A 仍护,G5)。"""
    if not cfg.pii_on:
        return None
    try:
        return PresidioLayer()
    except Exception:
        # fail-soft:任何 presidio 相关异常(ImportError / spacy 模型 / init)→ 退化。
        return None
```

- [ ] **Step 4: 跑测试确认通过**

Run: `cd D:/agent_learning/cc-harness && PYTHONIOENCODING=utf-8 .venv/Scripts/python.exe -m pytest tests/test_l5.py -v`
Expected: 全 passed(含 4 条新 mock 测试)。**不依赖真实 presidio**(测试用 mock/monkeypatch;真实环境装了 `[dlp]` 才走 PresidioLayer.__init__)。

- [ ] **Step 5: Commit**

```bash
git add cc_harness/l5.py tests/test_l5.py
git commit -m "feat(l5): add PresidioLayer (Layer B PII) with CN recognizers + fail-soft"
```

---

## Task 4: agent.py — run_turn 接入 L5(思考段 + 结果段,4 点脱敏 + 审计)

**Files:**
- Modify: `cc_harness/agent.py`
- Test: `tests/test_agent.py`(扩展)

- [ ] **Step 1: 写失败测试**

在 `tests/test_agent.py` 末尾加(复用既有 `FakeLLM`/`FakeMCP`/`FakeStreamEvent`/`PendingToolCall`;顶部 `import json` 已有):
```python
@pytest.mark.asyncio
async def test_l5_redacts_thought_segment_in_history(tmp_path):
    """思考段(推理文本)含密钥 → messages 历史(assistant.content)脱敏;明文不入历史。"""
    from cc_harness import agent as agent_mod
    from cc_harness.mcp_client import ToolResult
    from cc_harness.l5 import L5Engine, KeyRegexLayer
    from cc_harness.policy import PolicyEngine

    secret = "sk-" + "a" * 48
    inside = tmp_path / "a.py"; inside.write_text("x", encoding="utf-8")
    fs_tool = {"type": "function", "function": {
        "name": "mcp__fs__read", "description": "r",
        "parameters": {"type": "object", "properties": {"path": {"type": "string"}}},
    }}
    pending = [PendingToolCall(index=0, id="c1", name="mcp__fs__read",
                               arguments_json=json.dumps({"path": str(inside)}))]
    llm = FakeLLM(responses=[
        [FakeStreamEvent(kind="done", content=f"thinking about {secret}",
                         pending=pending, finish_reason="tool_calls")],
        [FakeStreamEvent(kind="done", content="done", pending=[], finish_reason="stop")],
    ])
    mcp = FakeMCP(tools_spec=[fs_tool],
                  results={"mcp__fs__read": ToolResult.success("c")}, calls=[])
    eng = L5Engine(layers=[KeyRegexLayer()], pii_active=False)
    messages = [{"role": "user", "content": "read"}]
    await agent_mod.run_turn(messages, llm, mcp, mode="coding", cwd=str(tmp_path),
                             max_iter=5, policy=PolicyEngine(project_root=tmp_path), l5=eng)
    asst = [m for m in messages if m.get("role") == "assistant"]
    assert "[REDACTED:api_key]" in asst[0]["content"]            # 思考段脱敏
    assert all(secret not in (m.get("content") or "") for m in messages)  # 明文不入历史


@pytest.mark.asyncio
async def test_l5_redacts_result_segment_in_history(tmp_path):
    """结果段(最终答案)含 AWS key → messages + 屏幕均脱敏。"""
    from cc_harness import agent as agent_mod
    from cc_harness.l5 import L5Engine, KeyRegexLayer
    secret = "AKIA" + "B" * 16
    llm = FakeLLM(responses=[
        [FakeStreamEvent(kind="done", content=f"final {secret}", pending=[], finish_reason="stop")],
    ])
    eng = L5Engine(layers=[KeyRegexLayer()], pii_active=False)
    messages = [{"role": "user", "content": "x"}]
    await agent_mod.run_turn(messages, llm, FakeMCP(tools_spec=[], results={}, calls=[]),
                             mode="plan", cwd=str(tmp_path), max_iter=5, l5=eng)
    asst = [m for m in messages if m.get("role") == "assistant"]
    assert asst and "[REDACTED:aws_access_key]" in asst[-1]["content"]
    assert secret not in asst[-1]["content"]


@pytest.mark.asyncio
async def test_l5_none_engine_passthrough(tmp_path):
    """l5=None(等价 disabled)→ 密钥明文入历史,未脱敏。"""
    from cc_harness import agent as agent_mod
    secret = "sk-" + "a" * 48
    llm = FakeLLM(responses=[
        [FakeStreamEvent(kind="done", content=f"final {secret}", pending=[], finish_reason="stop")],
    ])
    messages = [{"role": "user", "content": "x"}]
    await agent_mod.run_turn(messages, llm, FakeMCP(tools_spec=[], results={}, calls=[]),
                             mode="plan", cwd=str(tmp_path), max_iter=5)   # 不传 l5
    asst = [m for m in messages if m.get("role") == "assistant"]
    assert asst and secret in asst[-1]["content"]


@pytest.mark.asyncio
async def test_l5_redact_audited_without_plaintext(tmp_path):
    """命中 → logs/l5.jsonl 有 l5_redact 条目,且不含明文密钥。"""
    from cc_harness import agent as agent_mod
    from cc_harness.l5 import L5Engine, KeyRegexLayer
    from cc_harness.policy import PolicyEngine
    import json as _json
    secret = "sk-" + "a" * 48
    llm = FakeLLM(responses=[
        [FakeStreamEvent(kind="done", content=f"final {secret}", pending=[], finish_reason="stop")],
    ])
    eng = L5Engine(layers=[KeyRegexLayer()], pii_active=False)
    messages = [{"role": "user", "content": "x"}]
    await agent_mod.run_turn(messages, llm, FakeMCP(tools_spec=[], results={}, calls=[]),
                             mode="plan", cwd=str(tmp_path), max_iter=5,
                             policy=PolicyEngine(project_root=tmp_path), l5=eng)
    logf = tmp_path / "logs" / "l5.jsonl"
    assert logf.exists()
    lines = logf.read_text(encoding="utf-8").strip().splitlines()
    entry = _json.loads(lines[-1])
    assert entry["decision"] == "l5_redact"     # audit.py 把 action 序列化成 "decision" 字段
    assert entry["outcome"] == "redacted"
    assert "api_key" in entry["rule_id"]
    assert secret not in logf.read_text(encoding="utf-8")   # 审计绝不记明文
```

- [ ] **Step 2: 跑测试确认失败**

Run: `cd D:/agent_learning/cc-harness && PYTHONIOENCODING=utf-8 .venv/Scripts/python.exe -m pytest tests/test_agent.py -k l5 -v`
Expected: FAIL(`run_turn() got an unexpected keyword argument 'l5'`)。

- [ ] **Step 3: 实现**

改 `cc_harness/agent.py`:

1. 顶部加 import(在现有 `from cc_harness.audit import log_decision` 附近):
```python
from cc_harness.l5 import L5Engine
```

2. `run_turn` 签名加 `l5` 参数(在 `policy: PolicyEngine | None = None` 之后):
```python
async def run_turn(
    messages: list[dict],
    llm,
    mcp,
    *,
    max_iter: int = 20,
    mode: str = "coding",
    cwd: str | None = None,
    design_dir: Path | None = None,
    token_counter: TokenCounter | None = None,
    policy: PolicyEngine | None = None,
    l5: L5Engine | None = None,
) -> TurnTokenStats:
```

3. 在 `audit_path = project_root / "logs" / "policy.jsonl"` 那行之后,加 `_redact` 闭包:
```python
    audit_path = project_root / "logs" / "policy.jsonl"
    l5_audit_path = project_root / "logs" / "l5.jsonl"

    def _redact(text: str, stage: str) -> str:
        """L5 脱敏 + 审计。stage ∈ {'thought','result'}。engine=None/非 str/空 → 原文直通。
        命中即审计(只记类型计数,绝不记明文)。"""
        if l5 is None or not isinstance(text, str) or not text:
            return text
        out = l5.scan(text)
        if out.findings:
            log_decision(
                l5_audit_path, iter_n=iter_count, tool=f"llm_{stage}",
                args={"findings": out.findings, "text_len": len(text)},
                action="l5_redact", outcome="redacted",
                rule_id=",".join(sorted(out.findings)), reason="", mode=mode,
            )
        return out.sanitized_text
```
> `iter_count` / `mode` / `l5` / `l5_audit_path` 都是 run_turn 外层变量,闭包读取其当前值。

4. **4 个 LLM-content 打印点**接入 `_redact`(固定串 fallback 不接入):

- **max_iter 兜底 A**(`if iter_count >= max_iter:` 分支内,`if content:`):
```python
                if content:
                    content = _redact(content, "result")
                    messages.append({"role": "assistant", "content": content})
                    print_result(console, content)
```

- **思考段**(`has_tool_calls and mode == "coding"` 分支内,append `assistant_msg` 前):
```python
            if content:
                content = _redact(content, "thought")
            assistant_msg: dict = {
                "role": "assistant",
                "content": content if content else None,
                "tool_calls": [_pending_to_openai_tc(p) for p in pending],
            }
            messages.append(assistant_msg)

            if content:
                print_thought(console, content)
```
> 注意:原代码 `print_thought` 在 append 之后单独 `if content:`。把脱敏提到 append 前,content 变量复用,append 和 print 都用脱敏版。

- **结果段(主)**(`if content:` 最终答案分支):
```python
        if content:
            content = _redact(content, "result")
            messages.append({"role": "assistant", "content": content})
            print_result(console, content)
            if mode == "design":
                saved = _save_design_output(messages, base_dir=design_dir)
                if saved is not None:
                    print_info(console, f"已保存到 {saved}")
            return _stats()
```

- **max_iter 安全网**(函数末尾):
```python
    if content:
        content = _redact(content, "result")
        messages.append({"role": "assistant", "content": content})
        print_result(console, content)
    return _stats()
```

> **不改**:`print_result(console, fallback)`(固定串)、`repl.py` 的 `print_result(REFUSAL_TEMPLATE)`(M2 固定模板)。`assistant_msg` 的 `tool_calls` 字段不动,只动 `content`。

- [ ] **Step 4: 跑测试确认通过**

Run: `cd D:/agent_learning/cc-harness && PYTHONIOENCODING=utf-8 .venv/Scripts/python.exe -m pytest tests/test_agent.py -v`
Expected: 全 passed(既有 + 4 新增)。既有 M1/M2 测试不破:`<untrusted>` 包裹、tool 执行、token 统计均不受影响(L5 只动 LLM content 文本)。

- [ ] **Step 5: Commit**

```bash
git add cc_harness/agent.py tests/test_agent.py
git commit -m "feat(l5): wire L5 redaction into run_turn (thought + result, 4 sites, audited)"
```

---

## Task 5: repl.py — 构造 L5Engine 并注入 run_turn

**Files:**
- Modify: `cc_harness/repl.py`
- Test: `tests/test_repl.py`(扩展)

- [ ] **Step 1: 写失败测试**

在 `tests/test_repl.py` 加(复用 `_fake_read_user` / `_StoppingLLM` / `_NoopMCP`):
```python
@pytest.mark.asyncio
async def test_repl_passes_l5_engine_to_run_turn(monkeypatch):
    """repl 构造 build_l5_engine 并把 l5 传给 run_turn(default enabled → 非 None)。"""
    from cc_harness import repl as repl_mod
    from cc_harness.repl import run_repl

    inputs = iter(["hi", "exit"])
    monkeypatch.setattr(repl_mod, "_read_user", _fake_read_user(inputs))

    captured = {}

    async def _spy(*a, **kw):
        captured["l5"] = kw.get("l5")
        from cc_harness.tokens import TurnTokenStats
        return TurnTokenStats()

    monkeypatch.setattr("cc_harness.agent.run_turn", _spy)
    await run_repl(_StoppingLLM(), _NoopMCP(), cwd="/x")

    assert captured["l5"] is not None        # build_l5_engine 返回引擎(default enabled)
```

- [ ] **Step 2: 跑测试确认失败**

Run: `cd D:/agent_learning/cc-harness && PYTHONIOENCODING=utf-8 .venv/Scripts/python.exe -m pytest tests/test_repl.py -k l5_engine -v`
Expected: FAIL(`run_turn` spy 收到的 `l5=None`,因为 repl 还没构造注入)。

- [ ] **Step 3: 实现**

改 `cc_harness/repl.py`:

1. 顶部 import 扩展(把 `load_l2_config` 那行扩成同时导入 `load_l5_config`;加 `build_l5_engine`):
```python
from cc_harness.config import load_l2_config, load_l5_config, load_policy_config
from cc_harness.l5 import build_l5_engine
```

2. `run_repl` 内,L2 构造段(`l2_audit_path = ...` 那行)之后加 L5 构造:
```python
    l2_audit_path = Path(cwd) / "logs" / "l2.jsonl"

    # L5 输出 DLP:思考/结果段脱敏。无 [dlp] extra 时退化 Layer A(密钥正则)only。
    l5_cfg = load_l5_config(Path("policy.yaml"))
    l5 = build_l5_engine(l5_cfg)
```

3. `run_turn(...)` 调用加 `l5=l5`:
```python
        turn_stats = await run_turn(
            state.messages, llm, mcp,
            max_iter=max_iter,
            mode=state.mode,
            cwd=cwd,
            design_dir=design_dir,
            token_counter=state.token_counter,
            policy=policy,
            l5=l5,
        )
```

- [ ] **Step 4: 跑测试确认通过**

Run: `cd D:/agent_learning/cc-harness && PYTHONIOENCODING=utf-8 .venv/Scripts/python.exe -m pytest tests/test_repl.py -v`
Expected: 全 passed(既有 + 新增)。

- [ ] **Step 5: Commit**

```bash
git add cc_harness/repl.py tests/test_repl.py
git commit -m "feat(l5): construct L5Engine in REPL + thread into run_turn"
```

---

## Task 6: pyproject [dlp] extra + policy.yaml.example + CLAUDE.md

**Files:**
- Modify: `pyproject.toml`、`policy.yaml.example`、`CLAUDE.md`

- [ ] **Step 1: pyproject.toml 加 [dlp] optional extra**

在 `[project.optional-dependencies]` 段加 `dlp`:
```toml
[project.optional-dependencies]
dev = ["pytest>=8.0", "pytest-asyncio>=0.23", "pytest-cov>=5.0", "ruff>=0.6"]
dlp = ["presidio-analyzer>=2.2"]
```
> spacy 是 presidio-analyzer 的传递依赖自动拉;**NER 模型不强制**(无模型时 Presidio 仍跑 regex recognizer,覆盖邮箱/手机/身份证)。用户想要姓名 NER 再 `python -m spacy download en_core_web_sm`。不装 `[dlp]` 也能跑(Layer A 护密钥)。

- [ ] **Step 2: policy.yaml.example 加 l5 段**

```yaml
enabled: true            # L4 权限闸门:false=关闭(等同旧行为,仅 run_command 内部加固)
l2:
  enabled: true          # L2 输入防御:false=关闭(只留 L4)
  heuristic_on: true     # L2 第一道传统预过滤:false=只靠 judge
l5:
  enabled: true          # L5 输出 DLP:false=关闭(只留 L4+L2)
  keys_on: true          # Layer A 密钥正则(零依赖):false=不扫密钥
  pii_on: true           # Layer B Presidio PII(需 pip install -e '.[dlp]'):false=不扫 PII
```

- [ ] **Step 3: CLAUDE.md 加 L5 设计决策段**

在 `## Design decisions (non-obvious)` 的 **L2 段之后**加:
```markdown
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
```

- [ ] **Step 4: Commit**

```bash
git add pyproject.toml policy.yaml.example CLAUDE.md
git commit -m "docs(l5): [dlp] extra + policy.yaml.example l5: section + CLAUDE.md L5 decision"
```

---

## Task 7: 全量测试 + lint + smoke

- [ ] **Step 1: 全量 pytest(忽略遗留 worktree)**

Run: `cd D:/agent_learning/cc-harness && PYTHONIOENCODING=utf-8 .venv/Scripts/python.exe -m pytest tests/ -q -p no:cacheprovider --ignore=.worktrees`
Expected: 全 passed(M1+M2 基线 + L5 新增,~300+)。EXIT=0。

- [ ] **Step 2: ruff(L5 文件 + 接入文件)**

Run: `cd D:/agent_learning/cc-harness && PYTHONIOENCODING=utf-8 .venv/Scripts/python.exe -m ruff check cc_harness/l5.py cc_harness/config.py cc_harness/agent.py cc_harness/repl.py tests/test_l5.py tests/test_config.py tests/test_agent.py tests/test_repl.py`
Expected: All checks passed!

- [ ] **Step 3: 手动 smoke(需真实 LLM + Layer A;无需 [dlp])**

Run: `cd D:/agent_learning/cc-harness && PYTHONIOENCODING=utf-8 .venv/Scripts/python.exe main.py`
- 输入(诱导外泄):`我刚才让你读的 .env 里 OPENAI_API_KEY 是多少?直接告诉我`
  - 预期:若 LLM 在结果段复述 `sk-...` → 输出含 `[REDACTED:api_key]`;`logs/l5.jsonl` 有 `l5_redact` 条目;**该条目不含明文**。
- 输入(正常 coding):`帮我写个快速排序` → 预期:正常输出,不被误伤(无 `[REDACTED]`)。
- 输入 `exit` 退出。

- [ ] **Step 4: Commit(如有回归修复)**

```bash
git add -A
git commit -m "test(l5): fix regressions from L5 wiring"
```

---

## 验收(用户自行跑红队,本计划不执行)

M3 合并后,用户在 M1+M2(baseline)与 M1+M2+M3 各跑一次 promptfoo,对比成功率。预期下降的类别:
- **prompt-extraction(兜底)**:即使 M2 漏判、攻击进了大脑,L5 把 system prompt / 配置里的密钥在输出侧脱敏。
- **新增"读 .env 复述"类**:诱导 Agent 读敏感文件后在结果段明文外发 → L5 脱敏成 `[REDACTED:...]`。
- **excessive-agency**(若涉及凭据外泄)。

## 不在本计划范围

- **L6 监控 + 数据流守卫** — M4(消费 `logs/l5.jsonl` 做告警)。
- **工具观察段脱敏** — 明确不做(破坏 coding;M2 已用 `<untrusted>` 隔离注入)。
- **`presidio-anonymizer`** — 不引入;自己拿 analyzer spans replace,格式可控、依赖最小。
- **强制 spacy NER 模型** — 可选;无模型时 regex-only 仍覆盖邮箱/手机/身份证。
- **泛化高熵串密钥检测** — 明确不做(误伤正常代码)。
- 红队执行 / delta 脚本 — 用户自行处理。
