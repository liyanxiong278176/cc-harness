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
    """合并 spans,重叠取最早开始的(丢嵌套/后到的;同起点取较长 span,多捕敏感内容),
    倒序 replace 成 [REDACTED:type]。
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
