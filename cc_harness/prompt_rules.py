"""Pinned production prompt-rule registry.

The registry is intentionally small and vendor-neutral.  Research sources are
recorded for audit/release tooling, while the runtime only renders the locally
adapted rule text.  Production never fetches prompt material from the network.
"""
from __future__ import annotations

import hashlib
from dataclasses import dataclass


RULE_REGISTRY_VERSION = "rules-v1"


@dataclass(frozen=True)
class PromptRule:
    rule_id: str
    local_version: str
    layer: str
    text: str
    source_record: str
    license_record: str

    @property
    def digest(self) -> str:
        payload = "\n".join(
            (self.rule_id, self.local_version, self.layer, self.text,
             self.source_record, self.license_record)
        )
        return hashlib.sha256(payload.encode("utf-8")).hexdigest()

    def audit_metadata(self) -> dict[str, str]:
        """Full provenance for release review, never sent to the model/UI."""
        return {
            "rule_id": self.rule_id,
            "local_version": self.local_version,
            "layer": self.layer,
            "source_record": self.source_record,
            "license_record": self.license_record,
            "digest": self.digest,
        }


# These are adapted behavioural rules, not copied provider prompts.  The
# source record points to the reviewed research bundle in ADR-0088; updating a
# rule requires changing its local version and digest before release.
PRODUCTION_RULES: tuple[PromptRule, ...] = (
    PromptRule(
        "external.inspect-before-edit", "1", "shared-core",
        "修改前先读取相关文件和约束;不要凭猜测声称已理解项目。",
        "https://github.com/openai/codex/blob/main/codex-rs/models-manager/prompt.md#agents-md-spec;reviewed=2026-08-25",
        "Apache-2.0;openai/codex",
    ),
    PromptRule(
        "external.verify-before-complete", "1", "shared-core",
        "完成前用与验收条件匹配的最小验证;没有证据就明确标注缺口,不要把生成文本当作验证。",
        "https://github.com/openai/codex/blob/main/codex-rs/models-manager/prompt.md#planning;reviewed=2026-08-25",
        "Apache-2.0;openai/codex",
    ),
    PromptRule(
        "external.small-scope-change", "1", "shared-core",
        "优先采用小而可回滚的变更,保留用户已有约束;遇到不确定或有副作用的动作先停在安全边界。",
        "https://github.com/swe-agent/SWE-agent/tree/main/docs;reviewed=2026-08-25",
        "MIT;swe-agent/SWE-agent",
    ),
    PromptRule(
        "external.report-blockers", "1", "shared-core",
        "遇到阻塞时报告具体证据、已尝试路径和下一步,不要用重复调用制造进展。",
        "https://github.com/All-Hands-AI/OpenHands/tree/main/openhands/agenthub;reviewed=2026-08-25",
        "MIT;All-Hands-AI/OpenHands",
    ),
)


def render_production_rules(mode: str) -> str:
    """Render the adapted rule text without provenance/source mapping."""
    del mode  # all rules are vendor-neutral shared-core rules
    lines = ["## 审计后的生产规则"]
    lines.extend(f"- {rule.text}" for rule in PRODUCTION_RULES)
    return "\n".join(lines)


def production_rule_metadata() -> dict[str, object]:
    """Return safe, non-reconstructable operational metadata."""
    return {
        "version": RULE_REGISTRY_VERSION,
        "rule_count": len(PRODUCTION_RULES),
        "digest": hashlib.sha256(
            "\n".join(rule.digest for rule in PRODUCTION_RULES).encode("ascii")
        ).hexdigest(),
    }


def production_rule_audit_records() -> tuple[dict[str, str], ...]:
    """Return provenance only for explicit release/audit tooling."""
    return tuple(rule.audit_metadata() for rule in PRODUCTION_RULES)


__all__ = [
    "PromptRule",
    "RULE_REGISTRY_VERSION",
    "PRODUCTION_RULES",
    "render_production_rules",
    "production_rule_metadata",
    "production_rule_audit_records",
]
