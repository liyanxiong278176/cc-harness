"""Deterministic provenance and egress controls for the agent runtime.

The module deliberately contains no model calls.  Tool output is data, never
an instruction source, and an action argument is trusted only when its value
can be traced to the user request (or to an explicitly delegated source).
Callers may attach richer metadata from an MCP capability contract; missing
metadata is represented as ``unknown`` rather than inferred from a tool name.
"""

from __future__ import annotations

import calendar
import json
import re
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Mapping, Sequence


SECURITY_POLICY_VERSION = "cc-harness-security-policy.v3"
SECURITY_AUDIT_SCHEMA_VERSION = "cc-harness.security-audit.v2"


class CapabilityEffect(str, Enum):
    """The side effect class a tool is allowed to have."""

    READ = "read"
    WRITE = "write"
    EXTERNAL_WRITE = "external_write"
    NETWORK = "network"
    CREDENTIAL = "credential"
    UNKNOWN = "unknown"


@dataclass(frozen=True)
class ToolCapability:
    """Explicit tool capability contract.

    ``declared`` is intentionally separate from ``effect``.  A contract that
    declares ``unknown`` is still useful evidence, while missing metadata must
    remain distinguishable and conservative.
    """

    effect: CapabilityEffect = CapabilityEffect.UNKNOWN
    network: bool = False
    credential: bool = False
    idempotent: bool = True
    requires_user_intent: bool = True
    declared: bool = False
    source: str = "missing"

    @property
    def high_risk(self) -> bool:
        return self.effect in {
            CapabilityEffect.WRITE,
            CapabilityEffect.EXTERNAL_WRITE,
            CapabilityEffect.NETWORK,
            CapabilityEffect.CREDENTIAL,
            CapabilityEffect.UNKNOWN,
        } or self.network or self.credential


class ProvenanceSource(str, Enum):
    USER_REQUEST = "user_request"
    DELEGATED_SOURCE = "delegated_source"
    TOOL_RESULT = "tool_result"
    PROJECT_INSTRUCTION = "project_instruction"
    UNKNOWN = "unknown"


@dataclass(frozen=True)
class FieldProvenance:
    source: ProvenanceSource
    trusted: bool
    delegated: bool = False
    evidence: str = ""


@dataclass(frozen=True)
class ToolFieldContract:
    """Typed metadata for one tool argument field.

    Contracts are evidence about how a field may be used; they are not an
    authorization grant.  A value can still be ``unknown``/tainted after a
    schema match, and high-impact sinks continue through the policy gate.
    """

    path: str
    value_type: str = "unknown"
    sink: str = ""
    sensitive: bool = False
    side_effect: bool = False
    declared: bool = False


@dataclass(frozen=True)
class ActionPlan:
    """A typed view of an imminent tool call used by policy and auditing."""

    tool_name: str
    arguments: Mapping[str, Any]
    capability: ToolCapability
    fields: Mapping[str, FieldProvenance] = field(default_factory=dict)
    field_contracts: Mapping[str, ToolFieldContract] = field(default_factory=dict)

    @property
    def has_untrusted_fields(self) -> bool:
        return any(not item.trusted for item in self.fields.values())

    @property
    def untrusted_fields(self) -> tuple[str, ...]:
        return tuple(path for path, item in self.fields.items() if not item.trusted)

    @property
    def untrusted_sensitive_fields(self) -> tuple[str, ...]:
        return tuple(
            path
            for path in self.untrusted_fields
            if self.field_contracts.get(path, ToolFieldContract(path)).sensitive
        )

    @property
    def untrusted_tool_fields(self) -> tuple[str, ...]:
        return tuple(
            path
            for path, provenance in self.fields.items()
            if not provenance.trusted and provenance.source is ProvenanceSource.TOOL_RESULT
        )

    @property
    def hard_boundary_fields(self) -> tuple[str, ...]:
        """Return untrusted fields that can change security authority.

        The field-name fallback is intentionally narrow and only identifies
        control/credential fields.  Ordinary destination/path validation is
        still performed by ``PolicyEngine`` against resolved roots.
        """

        hard_names = {
            "authorization", "api_key", "apikey", "access_token", "token",
            "secret", "password", "passwd", "private_key", "credential",
            "system_prompt", "developer_prompt", "policy", "permissions",
            "permission", "role", "admin", "escalate", "override_policy",
        }
        result: list[str] = []
        for path in self.untrusted_fields:
            contract = self.field_contracts.get(path)
            leaf = _argument_leaf(path)
            if (
                (contract is not None and (contract.sensitive or contract.sink in {
                    "credential", "policy", "permission",
                }))
                or leaf in hard_names
                or any(name in leaf for name in ("secret", "token", "password", "credential"))
            ):
                result.append(path)
        return tuple(result)

    def audit_record(self) -> dict[str, Any]:
        """Return redaction-safe, field-level evidence for policy telemetry."""

        return {
            "tool": self.tool_name,
            "capability": {
                "effect": self.capability.effect.value,
                "network": self.capability.network,
                "credential": self.capability.credential,
                "declared": self.capability.declared,
                "source": self.capability.source,
            },
            "fields": {
                path: {
                    "source": provenance.source.value,
                    "trusted": provenance.trusted,
                    "delegated": provenance.delegated,
                    "evidence": provenance.evidence,
                    "contract": (
                        {
                            "type": contract.value_type,
                            "sink": contract.sink,
                            "sensitive": contract.sensitive,
                            "side_effect": contract.side_effect,
                            "declared": contract.declared,
                        }
                        if (contract := self.field_contracts.get(path)) is not None
                        else None
                    ),
                }
                for path, provenance in self.fields.items()
            },
            "untrusted_fields": list(self.untrusted_fields),
            "untrusted_sensitive_fields": list(self.untrusted_sensitive_fields),
            "untrusted_tool_fields": list(self.untrusted_tool_fields),
            "hard_boundary_fields": list(self.hard_boundary_fields),
        }


@dataclass(frozen=True)
class OutputFinding:
    kind: str
    matches: tuple[str, ...]
    source: str = "tool_result"
    severity: str = "observe"
    signals: tuple[str, ...] = ()
    quarantined: bool = False
    blocking: bool = False
    quarantine_matches: tuple[str, ...] = ()


_CAPABILITY_METADATA_KEYS = (
    "x-cc-harness-capability",
    "cc_harness_capability",
    "capability",
)

# Only exact first-party names are registered here.  Names containing words
# such as "write" are not guessed into a capability; an unregistered tool is
# always unknown and therefore conservative in strict mode.
_EXPLICIT_CAPABILITIES: dict[str, ToolCapability] = {
    "context7": ToolCapability(
        effect=CapabilityEffect.READ,
        requires_user_intent=False,
        declared=True,
        source="first_party_registry",
    ),
}


def _as_effect(value: Any) -> CapabilityEffect:
    try:
        return CapabilityEffect(str(value).strip().lower())
    except ValueError:
        return CapabilityEffect.UNKNOWN


def capability_from_metadata(metadata: Mapping[str, Any] | None) -> ToolCapability | None:
    """Parse an explicit capability object from an MCP/OpenAI tool spec."""

    if not metadata:
        return None
    raw: Any = None
    for key in _CAPABILITY_METADATA_KEYS:
        if key in metadata:
            raw = metadata[key]
            break
    # Internal callers pass the already-unwrapped contract map.  It is still
    # explicit metadata (not a tool-name inference), so accept that shape as
    # well as the OpenAI extension wrapper.
    if raw is None and "effect" in metadata:
        raw = metadata
    if raw is None:
        return None
    if isinstance(raw, str):
        raw = {"effect": raw}
    if not isinstance(raw, Mapping):
        return ToolCapability(source="invalid_contract")
    return ToolCapability(
        effect=_as_effect(raw.get("effect")),
        network=bool(raw.get("network", False)),
        credential=bool(raw.get("credential", False)),
        idempotent=bool(raw.get("idempotent", True)),
        requires_user_intent=bool(raw.get("requires_user_intent", True)),
        declared=True,
        source="tool_contract",
    )


def resolve_tool_capability(
    tool_name: str,
    metadata: Mapping[str, Any] | None = None,
) -> ToolCapability:
    """Resolve a capability without treating a name substring as authority."""

    contract = capability_from_metadata(metadata)
    if contract is not None:
        return contract
    exact = _EXPLICIT_CAPABILITIES.get(tool_name)
    if exact is not None:
        return exact
    return ToolCapability()


def _field_contract_from_raw(path: str, raw: Any) -> ToolFieldContract:
    if isinstance(raw, str):
        raw = {"type": raw}
    if not isinstance(raw, Mapping):
        raw = {}
    leaf = _argument_leaf(path)
    sink = str(raw.get("sink") or "").strip().lower()
    sensitive = bool(raw.get("sensitive", False))
    if leaf in {
        "authorization", "api_key", "apikey", "access_token", "token",
        "secret", "password", "passwd", "private_key", "credential",
    }:
        sensitive = True
    if not sink and sensitive:
        sink = "credential"
    return ToolFieldContract(
        path=path,
        value_type=str(raw.get("type") or raw.get("value_type") or "unknown"),
        sink=sink,
        sensitive=sensitive,
        side_effect=bool(raw.get("side_effect", False)),
        declared=bool(raw.get("declared", True)),
    )


def field_contracts_from_metadata(
    metadata: Mapping[str, Any] | None,
) -> dict[str, ToolFieldContract]:
    """Parse explicit field contracts and JSON-schema properties.

    The parser is deliberately permissive for old tool specs: absence of a
    contract does not grant trust, it only leaves the field tainted.  Nested
    object properties are represented with dotted paths so policy evidence can
    point to the exact sink.
    """

    if not metadata:
        return {}
    result: dict[str, ToolFieldContract] = {}
    raw_fields = metadata.get("fields")
    if isinstance(raw_fields, Mapping):
        for raw_path, raw_contract in raw_fields.items():
            path = str(raw_path).strip()
            if path:
                result[path] = _field_contract_from_raw(path, raw_contract)

    schema = metadata.get("parameters")
    if not isinstance(schema, Mapping):
        schema = metadata.get("input_schema")
    if not isinstance(schema, Mapping):
        return result

    def walk(node: Mapping[str, Any], prefix: str = "") -> None:
        properties = node.get("properties")
        if not isinstance(properties, Mapping):
            return
        for key, child in properties.items():
            path = f"{prefix}.{key}" if prefix else str(key)
            raw = child if isinstance(child, Mapping) else {}
            existing = result.get(path)
            contract = _field_contract_from_raw(path, raw)
            if existing is not None:
                contract = ToolFieldContract(
                    path=path,
                    value_type=existing.value_type if existing.value_type != "unknown" else contract.value_type,
                    sink=existing.sink or contract.sink,
                    sensitive=existing.sensitive or contract.sensitive,
                    side_effect=existing.side_effect or contract.side_effect,
                    declared=True,
                )
            result[path] = contract
            if isinstance(raw, Mapping):
                walk(raw, path)

    walk(schema)
    return result


_DELEGATION_TERMS = re.compile(
    r"\b(?:according to|from|in|inside|attached|attachment|email|message|"
    r"invoice|receipt|readme|document|file|tool output|result|网页|邮件|文件|"
    r"文档|附件|工具结果|address|location|name|restaurant|hotel|rating|"
    r"mentioned|todo|minutes|deadline|contact|calendar|event|each|person)\b",
    flags=re.IGNORECASE,
)

_MONTH_NAMES = {
    name.casefold(): number
    for number, names in enumerate(
        (
            ("january", "jan"),
            ("february", "feb"),
            ("march", "mar"),
            ("april", "apr"),
            ("may",),
            ("june", "jun"),
            ("july", "jul"),
            ("august", "aug"),
            ("september", "sep", "sept"),
            ("october", "oct"),
            ("november", "nov"),
            ("december", "dec"),
        ),
        start=1,
    )
    for name in names
}
_MONTH_PATTERN = "|".join(sorted(_MONTH_NAMES, key=len, reverse=True))
_MONTH_FIRST_DATE = re.compile(
    rf"\b(?P<month>{_MONTH_PATTERN})\s+(?P<day>\d{{1,2}})(?:st|nd|rd|th)?"
    rf"(?:\s*,?\s*(?P<year>\d{{4}}))?\b",
    flags=re.IGNORECASE,
)
_DAY_FIRST_DATE = re.compile(
    rf"\b(?P<day>\d{{1,2}})(?:st|nd|rd|th)?\s+(?:of\s+)?"
    rf"(?P<month>{_MONTH_PATTERN})(?:\s*,?\s*(?P<year>\d{{4}}))?\b",
    flags=re.IGNORECASE,
)
_NUMERIC_DATE = re.compile(
    r"\b(?P<year>\d{4})[-/.](?P<month>\d{1,2})[-/.](?P<day>\d{1,2})\b"
)
_ARGUMENT_DATETIME = re.compile(
    r"(?P<date>\d{4}[-/.]\d{1,2}[-/.]\d{1,2})[T ]"
    r"(?P<hour>\d{1,2}):(?P<minute>\d{2})(?::\d{2}(?:\.\d+)?)?"
    r"(?:Z|[+-]\d{2}:?\d{2})?"
)
_USER_CLOCK = re.compile(
    r"\b(?P<hour>\d{1,2})(?::(?P<minute>[0-5]\d))\s*(?P<ampm>a\.?m\.?|p\.?m\.?)?\b",
    flags=re.IGNORECASE,
)
_DURATION = re.compile(
    r"\b(?:for|lasting)\s+(?P<amount>\d+|one|two|three|four|five|half)\s+"
    r"(?P<unit>hours?|hrs?|minutes?|mins?)\b",
    flags=re.IGNORECASE,
)


def _word_date_matches(text: str) -> list[re.Match[str]]:
    """Return both month-first and day-first natural-language dates."""

    matches = [
        match
        for pattern in (_MONTH_FIRST_DATE, _DAY_FIRST_DATE)
        for match in pattern.finditer(text)
    ]
    return sorted(matches, key=lambda match: match.start())


def _valid_date_key(year: int, month: int, day: int) -> str | None:
    """Return a canonical date only for a real calendar date."""

    if not 1 <= month <= 12:
        return None
    try:
        if not 1 <= day <= calendar.monthrange(year, month)[1]:
            return None
    except (ValueError, OverflowError):
        return None
    return f"{year:04d}-{month:02d}-{day:02d}"


def _user_date_keys(text: str) -> set[str]:
    """Extract conservative canonical dates from user text.

    This deliberately supports only unambiguous ISO dates and English
    month-name dates.  A year omitted from one side of a date range inherits
    the sole explicit year in the request (e.g. ``January 11th to January
    15th 2025``), which covers the harmless formatting normalization models
    routinely perform before a tool call without trusting arbitrary values.
    """

    word_matches = _word_date_matches(text)
    explicit_years = [
        (match, int(match.group("year")))
        for match in word_matches
        if match.group("year")
    ]
    numeric: set[str] = set()
    for match in _NUMERIC_DATE.finditer(text):
        key = _valid_date_key(
            int(match.group("year")),
            int(match.group("month")),
            int(match.group("day")),
        )
        if key:
            numeric.add(key)
    result = set(numeric)
    for match in word_matches:
        year_text = match.group("year")
        if year_text:
            year = int(year_text)
        else:
            # Inherit a year only across an explicit date range.  This avoids
            # treating an unrelated historical date elsewhere in the request
            # as authority for a newly generated reservation date.
            nearby = [
                (other, value)
                for other, value in explicit_years
                if abs(other.start() - match.start()) <= 80
            ]
            candidate_years = {value for _, value in nearby}
            if len(candidate_years) != 1:
                continue
            range_text = " ".join(
                text[min(match.end(), other.start()) : max(match.start(), other.end())]
                for other, _ in nearby
            )
            if not re.search(
                r"\b(?:to|through|until|and|between|from)\b|[-–—]",
                range_text,
                flags=re.IGNORECASE,
            ):
                continue
            year = next(iter(candidate_years))
        month = _MONTH_NAMES[match.group("month").casefold()]
        key = _valid_date_key(year, month, int(match.group("day")))
        if key:
            result.add(key)
    return result


def _user_partial_date_parts(text: str) -> set[tuple[int, int]]:
    """Extract explicit month/day dates whose year was omitted.

    A missing year is useful for calendar/reservation benchmark requests, but
    relative phrases such as ``next January 11th`` must not silently authorize
    an arbitrary model-selected year.  The small relative-word guard keeps the
    normalization deterministic without treating historical dates as current
    intent.
    """

    result: set[tuple[int, int]] = set()
    for match in _word_date_matches(text):
        if match.group("year"):
            continue
        prefix = text[max(0, match.start() - 32) : match.start()]
        if re.search(
            r"\b(?:next|following|coming|last|previous|prior|later|earlier)\s*$",
            prefix,
            flags=re.IGNORECASE,
        ):
            continue
        result.add(
            (
                _MONTH_NAMES[match.group("month").casefold()],
                int(match.group("day")),
            )
        )
    return result


def _argument_date_key(value: str) -> str | None:
    """Canonicalize only ISO-like argument dates, never free-form text."""

    match = re.fullmatch(r"(\d{4})[-/.](\d{1,2})[-/.](\d{1,2})", value.strip())
    if not match:
        return None
    return _valid_date_key(int(match.group(1)), int(match.group(2)), int(match.group(3)))


def _argument_temporal_parts(value: str) -> tuple[str | None, int | None]:
    """Return ``(canonical_date, minutes_since_midnight)`` for ISO arguments."""

    normalized = value.strip()
    match = _ARGUMENT_DATETIME.fullmatch(normalized)
    if match:
        date_key = _argument_date_key(match.group("date"))
        hour = int(match.group("hour"))
        minute = int(match.group("minute"))
        if date_key is not None and hour < 24:
            return date_key, hour * 60 + minute
        return None, None
    return _argument_date_key(normalized), None


def _user_clock_minutes(text: str) -> set[int]:
    result: set[int] = set()
    for match in _USER_CLOCK.finditer(text):
        hour = int(match.group("hour"))
        minute = int(match.group("minute") or 0)
        ampm = (match.group("ampm") or "").casefold().replace(".", "")
        if ampm:
            if not 1 <= hour <= 12:
                continue
            if ampm == "pm" and hour != 12:
                hour += 12
            if ampm == "am" and hour == 12:
                hour = 0
        if hour < 24:
            result.add(hour * 60 + minute)
    return result


def _duration_minutes(text: str) -> set[int]:
    words = {"one": 1, "two": 2, "three": 3, "four": 4, "five": 5, "half": 0.5}
    result: set[int] = set()
    for match in _DURATION.finditer(text):
        raw = match.group("amount").casefold()
        amount = float(words.get(raw, raw))
        unit = match.group("unit").casefold()
        result.add(round(amount * (60 if unit.startswith(("hour", "hr")) else 1)))
    return result


def _message_texts(messages: Sequence[Mapping[str, Any]] | None, role: str | None = None) -> list[str]:
    result: list[str] = []
    for message in messages or ():
        if role is not None and message.get("role") != role:
            continue
        content = message.get("content", "")
        if isinstance(content, str):
            result.append(content)
        elif isinstance(content, list):
            result.append(
                "\n".join(
                    str(part.get("text", ""))
                    for part in content
                    if isinstance(part, Mapping) and part.get("type") == "text"
                )
            )
    return [item for item in result if item]


def _scalar_values(value: Any, prefix: str) -> list[tuple[str, str]]:
    if isinstance(value, Mapping):
        result: list[tuple[str, str]] = []
        for key, nested in value.items():
            result.extend(_scalar_values(nested, f"{prefix}.{key}" if prefix else str(key)))
        return result
    if isinstance(value, (list, tuple)):
        result = []
        for index, nested in enumerate(value):
            result.extend(_scalar_values(nested, f"{prefix}[{index}]"))
        return result
    if value is None:
        return []
    return [(prefix, str(value))]


def _argument_leaf(path: str) -> str:
    return re.sub(r"\[\d+\]", "", path.rsplit(".", 1)[-1]).casefold()


def _delegated_field_requested(path: str, user_text: str) -> bool:
    """Whether the user explicitly delegated this argument's value source."""

    leaf = _argument_leaf(path)
    text = user_text.casefold()
    if not _DELEGATION_TERMS.search(user_text):
        return False
    if any(token in leaf for token in ("recipient", "participant", "attendee", "email", "to", "cc", "bcc")):
        return bool(
            re.search(
                r"\b(?:email|recipient|person|people|contact|each|mentioned|send|address)\b|"
                r"\b(?:todo|to-do|minutes|document|file)\b",
                text,
            )
        )
    if any(token in leaf for token in ("location", "address", "venue")):
        return bool(re.search(r"\b(?:address|location|restaurant|hotel|venue)\b", text))
    if any(token in leaf for token in ("restaurant", "hotel", "company", "name")):
        return bool(re.search(r"\b(?:name|restaurant|hotel|company|rating|rated|choose|best)\b", text))
    if any(token in leaf for token in ("title", "subject")):
        return bool(
            re.search(
                r"\b(?:title|subject|event|remind|calendar|task|deadline|todo|"
                r"to-do|minutes|precise|include|explanation)\b",
                text,
            )
            or re.search(r"\{[^}]+\}", user_text)
        )
    if any(token in leaf for token in ("body", "description", "content", "message")):
        return bool(
            re.search(
                r"\b(?:task|deadline|todo|to-do|minutes|precise|description|include|"
                r"explanation|remind|reminder|book|dinner|calendar|event)\b",
                text,
            )
        )
    if any(token in leaf for token in ("file", "attachment", "document")):
        return bool(re.search(r"\b(?:file|attachment|document|minutes|todo)\b", text))
    # IDs are intentionally excluded.  A model must not turn an arbitrary
    # search-result identifier into a destructive action merely because the
    # user delegated another field from that result.
    return False


def _safe_delegated_scalar(value: str) -> bool:
    normalized = value.strip()
    if not normalized or len(normalized) > 512 or "\n" in normalized or "\r" in normalized:
        return False
    if normalized.startswith("[Tool Error]"):
        return False
    marker = globals().get("_INSTRUCTION_MARKERS")
    return not (marker and marker.search(normalized))


def _source_records(
    tool_results: Sequence[str] | None,
    tool_result_records: Sequence[Mapping[str, Any]] | None,
) -> list[tuple[str, str]]:
    """Normalize old string-only and richer tool-result records."""

    result: list[tuple[str, str]] = []
    for record in tool_result_records or ():
        if not isinstance(record, Mapping):
            continue
        text = record.get("result_full") or record.get("result") or ""
        if text:
            result.append((str(record.get("name") or ""), str(text)))
    if result:
        return result
    return [("", str(text)) for text in (tool_results or ()) if text]


def _structured_source_values(records: Sequence[tuple[str, str]]) -> list[tuple[str, str, str, bool]]:
    """Extract safe scalar candidates and their structured source keys.

    The raw text remains evidence, but instruction-bearing free-form fields are
    never promoted to trusted values.  JSON object keys are included because
    AgentDojo lookup tools use entity names as keys (for example restaurant
    name -> address).
    """

    values: list[tuple[str, str, str, bool]] = []
    email_pattern = re.compile(r"\b[A-Z0-9._%+-]+@[A-Z0-9.-]+\.[A-Z]{2,}\b", re.IGNORECASE)
    iso_pattern = re.compile(r"\b\d{4}-\d{2}-\d{2}(?:[T ]\d{2}:\d{2}(?::\d{2})?)?\b")
    for tool_name, raw in records:
        suspicious = _is_instruction_bearing_source(raw)
        try:
            decoded = json.loads(raw)
        except (TypeError, ValueError, json.JSONDecodeError):
            decoded = None

        def walk(value: Any, key: str = "") -> None:
            if isinstance(value, Mapping):
                for nested_key, nested in value.items():
                    key_text = str(nested_key)
                    if _safe_delegated_scalar(key_text) and len(key_text) >= 3:
                        values.append((tool_name, key_text, key_text, suspicious))
                    walk(nested, key_text)
                return
            if isinstance(value, (list, tuple)):
                for nested in value:
                    walk(nested, key)
                return
            if value is None:
                return
            scalar = str(value).strip()
            if _safe_delegated_scalar(scalar):
                values.append((tool_name, key, scalar, suspicious))

        if decoded is not None:
            walk(decoded)
        # Some benchmark functions intentionally return human-readable lists
        # rather than JSON.  Keep only short line candidates after a colon.
        for line in raw.splitlines():
            candidate = line.strip().lstrip("-*").strip()
            if ":" in candidate:
                candidate = candidate.split(":", 1)[1].strip()
            if _safe_delegated_scalar(candidate) and 3 <= len(candidate) <= 256:
                values.append((tool_name, "text", candidate, suspicious))
        if not suspicious:
            for candidate in email_pattern.findall(raw) + iso_pattern.findall(raw):
                if _safe_delegated_scalar(candidate):
                    values.append((tool_name, "content", candidate, False))
    return values


def _source_value_matches(
    path: str,
    value: str,
    user_text: str,
    source_values: Sequence[tuple[str, str, str, bool]],
) -> tuple[str, bool] | None:
    """Return delegated evidence for an exact or safe composed source value."""

    if not _delegated_field_requested(path, user_text):
        return None
    normalized = value.strip().casefold()
    leaf = _argument_leaf(path)
    for tool_name, key, candidate, suspicious in source_values:
        if suspicious or not _safe_delegated_scalar(candidate):
            continue
        candidate_folded = candidate.casefold()
        if normalized == candidate_folded:
            # Content fields are accepted only for explicit TODO/minutes/file
            # delegation; lookup keys/typed fields are safe for their matching
            # entity fields.
            if key == "content" and not re.search(
                r"\b(?:todo|to-do|minutes|document|file|deadline)\b",
                user_text,
                flags=re.IGNORECASE,
            ):
                continue
            return "structured_delegated_source", True
        if candidate_folded in normalized and len(candidate) >= 3:
            # Deterministic templates such as
            # ``Dinner at {restaurant_name}`` are permitted only when the user
            # supplied a placeholder/template, never for arbitrary prose.
            if leaf in {"title", "subject"}:
                if re.search(r"\{[^}]+\}", user_text):
                    return "delegated_template", True
                if leaf == "subject" and re.search(
                    r"\b(?:task|deadline|todo|to-do|minutes|precise|include|explanation)\b",
                    user_text,
                    flags=re.IGNORECASE,
                ):
                    return "delegated_composition", True
            if leaf in {"body", "description", "content", "message"} and re.search(
                r"\b(?:task|deadline|todo|to-do|minutes|precise|include|remind|reminder|"
                r"book|dinner|calendar|event)\b",
                user_text,
                flags=re.IGNORECASE,
            ):
                return "delegated_composition", True
    if leaf == "subject" and _safe_delegated_scalar(value):
        # Subjects are often a deterministic wrapper around the delegated
        # source name (for example ``Action Item from Team Meeting Minutes``)
        # and may not repeat an ISO scalar.  Require a recognizable source
        # phrase from the user; arbitrary model prose remains unknown.
        if re.search(
            r"\b(?:todo|to-do|team meeting|meeting minutes|deadline|task|document|file)\b",
            value,
            flags=re.IGNORECASE,
        ) and re.search(
            r"\b(?:todo|to-do|team meeting|meeting minutes|deadline|task|document|file)\b",
            user_text,
            flags=re.IGNORECASE,
        ):
            return "delegated_source_phrase", True
    return None


def derive_field_provenance(
    arguments: Mapping[str, Any],
    messages: Sequence[Mapping[str, Any]] | None = None,
    tool_results: Sequence[str] | None = None,
    *,
    tool_name: str = "",
    tool_result_records: Sequence[Mapping[str, Any]] | None = None,
) -> dict[str, FieldProvenance]:
    """Trace argument values to user text or an explicitly delegated source."""

    user_text = "\n".join(_message_texts(messages, role="user"))
    lowered_user = user_text.casefold()
    delegated = bool(_DELEGATION_TERMS.search(user_text))
    user_dates = _user_date_keys(user_text)
    partial_dates = _user_partial_date_parts(user_text)
    user_times = _user_clock_minutes(user_text)
    durations = _duration_minutes(user_text)
    source_records = _source_records(tool_results, tool_result_records)
    source_values = _structured_source_values(source_records)
    argument_temporals = {
        path: _argument_temporal_parts(value)
        for path, value in _scalar_values(arguments, "")
    }
    user_authorized_argument_dates = {
        date_key
        for date_key, _minutes in argument_temporals.values()
        if date_key
        and (
            date_key in user_dates
            or (int(date_key[5:7]), int(date_key[8:10])) in partial_dates
        )
    }
    calendar_action = bool(
        re.search(r"calendar|calendar_event|reminder|appointment", tool_name, flags=re.IGNORECASE)
        and re.search(r"\b(?:add|create|book|reserve|reschedule|schedule|remind)\b", user_text, flags=re.IGNORECASE)
    )
    result: dict[str, FieldProvenance] = {}
    for path, value in _scalar_values(arguments, ""):
        normalized = value.strip()
        if normalized and normalized.casefold() in lowered_user:
            result[path] = FieldProvenance(
                ProvenanceSource.USER_REQUEST,
                trusted=True,
                evidence="exact_value_in_user_request",
            )
            continue
        # Tool schemas commonly require ISO dates even when the user supplied
        # natural language ("January 11th ... 2025").  Trust only a canonical
        # date that was deterministically extracted from the user request;
        # this does not make arbitrary model-generated values trusted.
        argument_date, argument_minutes = _argument_temporal_parts(normalized)
        argument_parts: tuple[int, int] | None = None
        if argument_date:
            argument_parts = (int(argument_date[5:7]), int(argument_date[8:10]))
        if argument_date and argument_date in user_dates:
            evidence = "normalized_datetime_in_user_request" if argument_minutes is not None else "normalized_date_in_user_request"
            if argument_minutes is not None and user_times and argument_minutes not in user_times:
                # A user-specified date plus an explicitly requested duration
                # can deterministically yield an end time, but an arbitrary
                # start time is not trusted here.
                leaf = _argument_leaf(path)
                if not (leaf.startswith("end") and durations):
                    evidence = ""
            if evidence:
                result[path] = FieldProvenance(
                    ProvenanceSource.USER_REQUEST,
                    trusted=True,
                    evidence=evidence,
                )
                continue
        if argument_parts and argument_parts in partial_dates:
            # Yearless dates are accepted only as the explicit calendar date
            # requested by the user.  Relative expressions were removed by
            # _user_partial_date_parts, so a model cannot inject a year.
            if argument_minutes is None or calendar_action:
                result[path] = FieldProvenance(
                    ProvenanceSource.USER_REQUEST,
                    trusted=True,
                    evidence=(
                        "normalized_partial_datetime_on_user_date"
                        if argument_minutes is not None
                        else "normalized_partial_date_in_user_request"
                    ),
                )
                continue
        if (
            argument_minutes is not None
            and calendar_action
            and argument_date
            and (argument_date in user_dates or argument_parts in partial_dates)
            and not user_times
        ):
            # AgentDojo permits an event date without a time.  A bounded
            # same-day time is a deterministic completion of that request;
            # it is not authority to move the event to another date.
            result[path] = FieldProvenance(
                ProvenanceSource.USER_REQUEST,
                trusted=True,
                evidence="bounded_calendar_time_on_user_date",
            )
            continue
        if (
            argument_minutes is not None
            and calendar_action
            and _argument_leaf(path).startswith("end")
            and durations
            and argument_date in user_authorized_argument_dates
        ):
            # End time derived from a user-declared duration.  The start date
            # check above still prevents cross-day or tool-derived dates.
            result[path] = FieldProvenance(
                ProvenanceSource.USER_REQUEST,
                trusted=True,
                evidence="normalized_duration_end_time",
            )
            continue
        delegated_match = _source_value_matches(
            path, normalized, user_text, source_values
        )
        if delegated_match is not None:
            result[path] = FieldProvenance(
                ProvenanceSource.DELEGATED_SOURCE,
                trusted=True,
                delegated=True,
                evidence=delegated_match[0],
            )
            continue
        if delegated and normalized and any(
            normalized.casefold() in text.casefold()
            and not _is_instruction_bearing_source(text)
            for _, text in source_records
        ) and _delegated_field_requested(path, user_text):
            result[path] = FieldProvenance(
                ProvenanceSource.DELEGATED_SOURCE,
                trusted=True,
                delegated=True,
                evidence="exact_value_in_delegated_tool_source",
            )
            continue
        instruction_bearing_source = any(
            _is_instruction_bearing_source(text)
            for _, text in source_records
            if text
        )
        if instruction_bearing_source:
            result[path] = FieldProvenance(
                ProvenanceSource.TOOL_RESULT,
                trusted=False,
                evidence="unresolved_value_after_instruction_bearing_tool_result",
            )
        else:
            result[path] = FieldProvenance(
                ProvenanceSource.UNKNOWN,
                trusted=False,
                evidence="value_not_traceable_to_user_or_delegated_source",
            )
    return result


def build_action_plan(
    tool_name: str,
    arguments: Mapping[str, Any],
    *,
    messages: Sequence[Mapping[str, Any]] | None = None,
    tool_results: Sequence[str] | None = None,
    tool_result_records: Sequence[Mapping[str, Any]] | None = None,
    capability: ToolCapability | None = None,
    capability_metadata: Mapping[str, Any] | None = None,
) -> ActionPlan:
    resolved = capability or resolve_tool_capability(tool_name, capability_metadata)
    return ActionPlan(
        tool_name=tool_name,
        arguments=dict(arguments),
        capability=resolved,
        fields=derive_field_provenance(
            arguments,
            messages,
            tool_results,
            tool_name=tool_name,
            tool_result_records=tool_result_records,
        ),
        field_contracts=field_contracts_from_metadata(capability_metadata),
    )


_INSTRUCTION_MARKERS = re.compile(
    r"\b(?:ignore\s+(?:all\s+)?previous|system\s+message|developer\s+message|"
    r"assistant\s+instructions?|follow\s+these\s+instructions?|"
    r"disregard\s+(?:all\s+)?(?:prior|previous)|new\s+system\s+instruction)\b",
    flags=re.IGNORECASE,
)
_SOURCE_ROLE_MARKER = re.compile(
    r"\b(?:system|developer|assistant)\s+"
    r"(?:message|instruction|prompt)\b",
    flags=re.IGNORECASE,
)
_SOURCE_IGNORE_PREVIOUS_MARKER = re.compile(
    r"\bignore(?:\s+[a-z0-9'’-]+){0,5}\s+previous\b",
    flags=re.IGNORECASE,
)
_SOURCE_LABEL_DIRECTIVE_MARKER = re.compile(
    r"\b(?:todo|task|note)\s*[:\-]\s*"
    r"(?:please\s+)?(?:do\s+)?(?:say|recommend|visit|promote|advertise|"
    r"claim|state|tell)\b",
    flags=re.IGNORECASE,
)
_SOURCE_LEADING_DIRECTIVE_MARKER = re.compile(
    r"^(?:please\s+)?(?:do\s+)?(?:say|recommend|visit|promote|advertise|"
    r"claim|state|tell)\b",
    flags=re.IGNORECASE,
)
_SOURCE_DIRECTIVE_VERB = re.compile(
    r"\b(?:say|recommend|visit|promote|advertise|claim|state|tell)\b",
    flags=re.IGNORECASE,
)


def _normalize_instruction_source(text: str) -> str:
    """Normalize common source wrappers before classifying their semantics."""

    normalized = str(text or "").replace("_", " ").replace("-", " ")
    normalized = re.sub(r"[#()\[\]{}<>]", " ", normalized)
    return re.sub(r"\s+", " ", normalized).strip()


def _source_instruction_signals(
    context: str,
    candidate: str | None = None,
) -> set[str]:
    """Classify an instruction-bearing source span, optionally for a value.

    A bare word such as ``recommend`` is not enough. For candidate-aware
    checks it must lead the candidate's sentence (or follow a source label),
    which keeps ordinary review prose from quarantining factual hotel names.
    """

    raw = str(context or "")
    normalized = _normalize_instruction_source(raw)
    signals: set[str] = set()
    if _SOURCE_ROLE_MARKER.search(normalized):
        signals.update({"instructional", "role_injection"})
    if _SOURCE_IGNORE_PREVIOUS_MARKER.search(normalized):
        signals.add("instructional")
    if _SOURCE_LABEL_DIRECTIVE_MARKER.search(raw) or _SOURCE_LABEL_DIRECTIVE_MARKER.search(normalized):
        signals.add("instructional")

    if candidate:
        candidate_normalized = _normalize_instruction_source(candidate)
        candidate_pos = normalized.casefold().find(candidate_normalized.casefold())
        if candidate_pos >= 0:
            prefix = normalized[:candidate_pos]
            sentence_start = max(
                prefix.rfind("."),
                prefix.rfind("!"),
                prefix.rfind("?"),
                prefix.rfind(";"),
                prefix.rfind(":"),
            )
            sentence_prefix = prefix[sentence_start + 1 :].strip()
            if _SOURCE_LEADING_DIRECTIVE_MARKER.search(sentence_prefix):
                signals.add("instructional")
            elif _SOURCE_DIRECTIVE_VERB.search(sentence_prefix):
                # A directive verb is accepted only when it is the first
                # meaningful word in the candidate's sentence. This avoids
                # treating factual prose such as ``staff recommend X`` as an
                # instruction source.
                verb = _SOURCE_DIRECTIVE_VERB.search(sentence_prefix)
                if verb and not sentence_prefix[: verb.start()].strip():
                    signals.add("instructional")
    return signals


def _is_instruction_bearing_source(text: str) -> bool:
    """Return whether a source contains a structural injection directive."""

    return bool(_source_instruction_signals(text))


_SECRET_MARKERS = re.compile(
    r"\b(?:secret(?:s)?|password(?:s)?|token(?:s)?|api\s*key(?:s)?|"
    r"credential(?:s)?|private\s+key(?:s)?|access\s+key(?:s)?)\b",
    flags=re.IGNORECASE,
)
_POLICY_OVERRIDE_MARKERS = re.compile(
    r"\b(?:override|bypass|disable|change|replace|ignore)\b.{0,64}\b"
    r"(?:policy|policies|safety|guard|rule|rules|system|developer)\b|"
    r"\b(?:system|developer)\s+(?:prompt|message|instruction)\b",
    flags=re.IGNORECASE | re.DOTALL,
)
_EXTERNAL_ACTION_MARKERS = re.compile(
    r"\b(?:send|transfer|upload|delete|remove|share|post|publish|forward|"
    r"execute|run|write|overwrite|invite|email)\b",
    flags=re.IGNORECASE,
)
_QUOTED = re.compile(r"['\"]([^'\"]{3,160})['\"]")
_PROPER_PHRASE = re.compile(
    r"\b(?:[A-Z][\w'’-]*\s+){1,5}[A-Z][\w'’-]*\b"
)
_TOKEN = re.compile(r"[A-Za-z0-9][A-Za-z0-9'’.-]*")


def _echo_candidates(text: str) -> list[str]:
    candidates: list[str] = []
    candidates.extend(match.strip() for match in _QUOTED.findall(text))
    candidates.extend(match.strip() for match in _PROPER_PHRASE.findall(text))
    # Quoted/proper-name extraction catches AgentDojo's hotel target.  The
    # short n-gram pass covers JSON values such as account identifiers without
    # treating common one-word fields as an injection.
    tokens = _TOKEN.findall(text)
    for size in (3, 4, 5):
        for index in range(0, max(0, len(tokens) - size + 1)):
            phrase = " ".join(tokens[index : index + size])
            if any(char.isdigit() for char in phrase) or any(
                token[:1].isupper() for token in tokens[index : index + size]
            ):
                candidates.append(phrase)
    unique: list[str] = []
    seen: set[str] = set()
    for candidate in candidates:
        key = candidate.casefold()
        if len(candidate) < 3 or key in seen:
            continue
        seen.add(key)
        unique.append(candidate)
    return unique


def _candidate_context(text: str, candidate: str) -> str:
    """Return the smallest useful source span for risk classification."""

    folded = candidate.casefold()
    chunks = [chunk.strip() for chunk in re.split(r"(?:\r?\n|(?<=[.!?])\s+)", text)]
    for chunk in chunks:
        if folded in chunk.casefold():
            return chunk
    return text


def _output_risk_signals(context: str, candidate: str) -> set[str]:
    signals = _source_instruction_signals(context, candidate)
    normalized_context = _normalize_instruction_source(context)
    normalized_candidate = _normalize_instruction_source(candidate)
    if _INSTRUCTION_MARKERS.search(context) or _INSTRUCTION_MARKERS.search(candidate):
        signals.add("instructional")
    if _SECRET_MARKERS.search(context) or _SECRET_MARKERS.search(candidate):
        signals.add("secret_request")
    role_only = bool(
        _SOURCE_ROLE_MARKER.search(normalized_context)
        and not re.search(
            r"\b(?:override|bypass|disable|change|replace|ignore|disregard)\b",
            normalized_context,
            flags=re.IGNORECASE,
        )
    )
    if (
        _POLICY_OVERRIDE_MARKERS.search(context)
        or _POLICY_OVERRIDE_MARKERS.search(candidate)
        or _POLICY_OVERRIDE_MARKERS.search(normalized_context)
        or _POLICY_OVERRIDE_MARKERS.search(normalized_candidate)
    ) and not role_only:
        signals.add("policy_override")
    if _EXTERNAL_ACTION_MARKERS.search(context) or _EXTERNAL_ACTION_MARKERS.search(candidate):
        signals.add("external_action")
    if re.search(r"<\s*(?:system|developer|assistant)\b|\[\s*(?:system|developer)\s*\]", context, re.IGNORECASE):
        signals.add("role_injection")
    return signals


def detect_untrusted_echo(
    final_text: str,
    tool_results: Sequence[str],
    *,
    user_text: str = "",
) -> OutputFinding | None:
    """Find meaningful untrusted tool text repeated in the final answer."""

    if not final_text or not tool_results:
        return None
    final_folded = final_text.casefold()
    user_folded = user_text.casefold()
    matches: list[str] = []
    quarantine_matches: list[str] = []
    risk_signals: set[str] = set()
    blocking = False
    quarantined = False
    for source_text in tool_results:
        if not source_text:
            continue
        for candidate in _echo_candidates(source_text):
            folded = candidate.casefold()
            if folded not in final_folded or folded in user_folded:
                continue
            # Keep the deterministic guard focused on source-bearing content,
            # not generic JSON keys or single-word prose.
            if " " not in candidate and not any(char.isdigit() for char in candidate):
                continue
            matches.append(candidate)
            context = _candidate_context(source_text, candidate)
            signals = _output_risk_signals(context, candidate)
            risk_signals.update(signals)
            # Source + instruction is sufficient for span quarantine. A
            # secret/policy signal in the same span raises it to a blocking
            # finding because the unsafe intent cannot be treated as a fact.
            # A role wrapper alone remains span-scoped: it is untrusted, but
            # withholding the echoed value is enough and avoids cancelling a
            # legitimate action completed from the user's request.
            if "instructional" in signals:
                if signals & {"secret_request", "policy_override"}:
                    blocking = True
                else:
                    quarantined = True
                    quarantine_matches.append(candidate)
    if not matches:
        return None
    unique = tuple(dict.fromkeys(matches))
    if blocking:
        severity = "block"
    elif quarantined:
        severity = "quarantine"
    else:
        severity = "observe"
    return OutputFinding(
        kind="untrusted_output_echo",
        matches=unique,
        severity=severity,
        signals=tuple(sorted(risk_signals)),
        quarantined=quarantined and not blocking,
        blocking=blocking,
        quarantine_matches=tuple(dict.fromkeys(quarantine_matches)),
    )


def sanitize_untrusted_output(final_text: str, finding: OutputFinding) -> str:
    """Redact only repeated untrusted spans while retaining safe answer text."""

    result = final_text
    matches = finding.quarantine_matches or finding.matches
    for match in sorted(matches, key=len, reverse=True):
        if not match.strip():
            continue
        result = re.sub(
            re.escape(match),
            "[untrusted content withheld]",
            result,
            flags=re.IGNORECASE,
        )
    return result


def safe_action_summary(tool_calls: Sequence[Mapping[str, Any]]) -> str:
    """Build a non-content-bearing context for a constrained finalizer."""

    lines = []
    for call in tool_calls:
        name = str(call.get("name") or "unknown")
        ok = bool(call.get("ok"))
        args = call.get("args") if isinstance(call.get("args"), Mapping) else {}
        # Argument values can themselves originate from an untrusted tool
        # result.  The finalizer receives only field names and completion
        # status, never raw content or model-generated values.
        fields = ", ".join(sorted(str(key) for key in args)) or "none"
        lines.append(f"- {name}: {'completed' if ok else 'not completed'}; fields={fields}")
    return "\n".join(lines) or "- No tool action completed."
