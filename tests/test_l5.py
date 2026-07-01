from cc_harness.l5 import KeyRegexLayer, sanitize, build_l5_engine
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


# --- Task 3: Layer B (Presidio PII) + _maybe_build_pii_layer fail-soft ---

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
