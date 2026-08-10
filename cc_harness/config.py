import ipaddress
import json
import logging
import os
import re
from enum import Enum
from pathlib import Path
from typing import TYPE_CHECKING, Literal

from dotenv import dotenv_values, load_dotenv
from pydantic import BaseModel, Field, field_validator, model_validator

if TYPE_CHECKING:
    # Runtime import would be circular (memory.config re-exports load_memory_config).
    # load_memory_config lazy-imports MemoryConfig inside its body instead.
    from cc_harness.memory.config import MemoryConfig


log = logging.getLogger(__name__)


class ConfigError(Exception):
    pass


class MCPServerConfig(BaseModel):
    type: Literal["stdio", "sse", "http", "streamable-http"] = "stdio"
    command: str | None = None
    args: list[str] = []
    url: str | None = None
    env: dict[str, str] = {}

    @model_validator(mode="after")
    def _validate_transport_fields(self) -> "MCPServerConfig":
        if self.transport_type == "stdio" and not (self.command and self.command.strip()):
            raise ConfigError("MCP stdio server requires a non-empty 'command'")
        if self.transport_type in ("sse", "http") and not (self.url and self.url.strip()):
            raise ConfigError(f"MCP {self.type} server requires a non-empty 'url'")
        return self

    @property
    def transport_type(self) -> Literal["stdio", "sse", "http"]:
        if self.type in ("http", "streamable-http"):
            return "http"
        return self.type  # type: ignore[return-value]


class AppConfig(BaseModel):
    openai_api_key: str
    openai_base_url: str
    openai_model: str
    mcp_servers: dict[str, MCPServerConfig]
    runtime_environment: dict[str, str] = Field(default_factory=dict, exclude=True, repr=False)

    model_config = {"extra": "ignore"}


def load_config(env_path: Path, mcp_json_path: Path) -> AppConfig:
    """Load .env (no-op if missing) + mcp.json + required env vars.

    Required: OPENAI_API_KEY, OPENAI_BASE_URL, OPENAI_MODEL.
    """
    if env_path.exists():
        load_dotenv(env_path, override=False)

    api_key = os.getenv("OPENAI_API_KEY")
    if not api_key:
        raise ConfigError("OPENAI_API_KEY is required (set in .env or env var)")

    base_url = os.getenv("OPENAI_BASE_URL")
    if not base_url:
        raise ConfigError("OPENAI_BASE_URL is required (set in .env)")

    model = os.getenv("OPENAI_MODEL")
    if not model:
        raise ConfigError("OPENAI_MODEL is required (set in .env)")

    if not mcp_json_path.exists():
        raise ConfigError(f"mcp.json not found at {mcp_json_path}")

    raw = json.loads(mcp_json_path.read_text(encoding="utf-8"))
    servers_raw = raw.get("mcpServers", {})
    servers = {name: MCPServerConfig(**cfg) for name, cfg in servers_raw.items()}

    return AppConfig(
        openai_api_key=api_key,
        openai_base_url=base_url,
        openai_model=model,
        mcp_servers=servers,
        runtime_environment={
            key: value
            for key, value in os.environ.items()
            if key.startswith(("MEMORY_", "EMBEDDING_"))
        },
    )


def load_layered_config(
    project_root: Path,
    *,
    user_root: Path | None = None,
    environ: dict[str, str] | None = None,
) -> AppConfig:
    """Load installable CLI configuration without mutating ``os.environ``.

    Precedence for model values is process environment, project ``.env``,
    then user ``~/.cc-harness/.env``. MCP servers are merged user-first and
    project-last so a project can override a same-named user server. Missing
    MCP files are valid and mean no configured MCP servers.
    """
    project_root = Path(project_root).resolve()
    user_root = Path(user_root or (Path.home() / ".cc-harness")).resolve()
    process_env = dict(os.environ if environ is None else environ)
    user_env = {
        k: str(v) for k, v in dotenv_values(user_root / ".env").items()
        if v is not None
    }
    project_env = {
        k: str(v) for k, v in dotenv_values(project_root / ".env").items()
        if v is not None
    }

    def value(name: str) -> str | None:
        return process_env.get(name) or project_env.get(name) or user_env.get(name)

    layered_runtime_env = {
        key: selected
        for key in set(user_env) | set(project_env) | set(process_env)
        if key.startswith(("MEMORY_", "EMBEDDING_"))
        and (selected := value(key)) is not None
    }

    api_key = value("OPENAI_API_KEY")
    base_url = value("OPENAI_BASE_URL")
    model = value("OPENAI_MODEL")
    missing = [
        name for name, val in (
            ("OPENAI_API_KEY", api_key),
            ("OPENAI_BASE_URL", base_url),
            ("OPENAI_MODEL", model),
        ) if not val
    ]
    if missing:
        raise ConfigError("missing model configuration: " + ", ".join(missing))

    merged_servers: dict[str, dict] = {}
    for path in (user_root / "mcp.json", project_root / "mcp.json"):
        if not path.is_file():
            continue
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise ConfigError(f"invalid MCP config at {path}: {exc}") from exc
        servers = raw.get("mcpServers", {})
        if not isinstance(servers, dict):
            raise ConfigError(f"invalid MCP config at {path}: mcpServers must be an object")
        merged_servers.update(servers)

    try:
        parsed_servers = {
            name: MCPServerConfig(**server)
            for name, server in merged_servers.items()
        }
    except Exception as exc:
        if isinstance(exc, ConfigError):
            raise
        raise ConfigError(f"invalid MCP server configuration: {exc}") from exc

    return AppConfig(
        openai_api_key=api_key,
        openai_base_url=base_url,
        openai_model=model,
        mcp_servers=parsed_servers,
        runtime_environment=layered_runtime_env,
    )


class PolicyConfig(BaseModel):
    """权限闸门配置。M1 只暴露 enabled(杀手开关)。
    审计路径固定 <项目根>/.cc-harness/logs/policy.jsonl(agent 写死),不在此配置。

    E1 D7:e1_decompose_enabled 控制 Decomposer hint 是否注入到 system prompt
    (从 main.py 透传到 repl.py → agent.py run_turn,作为 _e1_extra["e1_decompose_hint"]
    的 AND 守卫,与 iter_count==0 / mode==coding 共同三重 gate)。默认 True
    (向后兼容;不写该字段也 default True — extra:ignore 已就位)。
    """
    enabled: bool = True
    e1_decompose_enabled: bool = True

    model_config = {"extra": "ignore"}


def load_policy_config(path: Path) -> PolicyConfig:
    """从可选 policy.yaml 加载;文件不存在返回默认。"""
    if not path.exists():
        return PolicyConfig()
    import yaml
    raw = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    return PolicyConfig(**raw)


class L2Config(BaseModel):
    """L2 输入防御配置。从 policy.yaml 的 `l2:` 段读;缺省全开。"""
    enabled: bool = True
    heuristic_on: bool = True

    model_config = {"extra": "ignore"}


def load_l2_config(path: Path) -> L2Config:
    """读 policy.yaml 的 `l2:` 子段(与 L4 的 PolicyConfig 独立)。文件/段缺失→默认。"""
    if not path.exists():
        return L2Config()
    import yaml
    raw = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    return L2Config(**(raw.get("l2") or {}))


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


class ExecutorBackend(str, Enum):
    NATIVE = "native"
    SANDBOX = "sandbox"


class SandboxVaultCredential(BaseModel):
    name: str = Field(min_length=1, pattern=r"^[a-zA-Z0-9_.-]+$")
    env_var: str = Field(min_length=1, pattern=r"^[A-Z_][A-Z0-9_]*$")


class SandboxVaultBinding(BaseModel):
    name: str = Field(min_length=1, pattern=r"^[a-zA-Z0-9_.-]+$")
    credential: str = Field(min_length=1)
    hosts: list[str] = Field(min_length=1)
    auth_type: Literal["bearer", "apiKey"] = "bearer"
    header_name: str | None = None
    schemes: list[Literal["https", "http"]] = ["https"]
    methods: list[str] | None = None
    paths: list[str] | None = None

    @model_validator(mode="after")
    def _validate_auth(self) -> "SandboxVaultBinding":
        if self.auth_type == "apiKey" and not self.header_name:
            raise ValueError("apiKey vault bindings require header_name")
        if self.auth_type == "bearer" and self.header_name is not None:
            raise ValueError("bearer vault bindings cannot set header_name")
        return self


class SandboxConfig(BaseModel):
    """Sandbox executor settings enforced through OpenSandbox 0.1.15."""
    server_host: str = "127.0.0.1"   # 用 127.0.0.1 非 localhost(Windows IPv6 ::1 连不上绑 127.0.0.1 的 server)
    server_port: int = 8000
    server_config_path: Path | None = None
    require_external_attestation: bool = True
    image: str = "cc-harness-runtime:local"
    timeout_s: int = Field(default=120, ge=1, le=3600)
    cpu: int = Field(default=2, ge=1, le=64)
    memory_mb: int = Field(default=2048, ge=128, le=131072)
    pids_limit: int = Field(default=256, ge=32, le=4096)
    egress_allow: list[str] = ["api.deepseek.com", "api.siliconflow.cn",
                               "pypi.org", "github.com"]
    vault: bool = False
    vault_credentials: list[SandboxVaultCredential] = []
    vault_bindings: list[SandboxVaultBinding] = []
    # Compatibility field for old policy files. Runtime fallback is always hard;
    # explicit host execution uses executor.backend=native instead.
    fallback_on_error: str = "hard"

    model_config = {"extra": "ignore"}

    @field_validator("egress_allow")
    @classmethod
    def _validate_egress_allow(cls, values: list[str]) -> list[str]:
        normalized: list[str] = []
        for raw in values:
            value = raw.strip().lower().rstrip(".")
            candidate = value.removeprefix("*.")
            if (
                not candidate
                or "://" in value
                or "/" in value
                or ":" in value
                or value == "*"
                or candidate == "localhost"
            ):
                raise ValueError(f"invalid sandbox egress domain: {raw!r}")
            try:
                ipaddress.ip_address(candidate)
            except ValueError:
                pass
            else:
                raise ValueError(f"sandbox egress rules must use domains, not IPs: {raw!r}")
            labels = candidate.split(".")
            if len(labels) < 2 or any(
                not re.fullmatch(r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?", label)
                for label in labels
            ):
                raise ValueError(f"invalid sandbox egress domain: {raw!r}")
            if value not in normalized:
                normalized.append(value)
        return normalized

    @model_validator(mode="after")
    def _validate_vault(self) -> "SandboxConfig":
        if not self.vault and (self.vault_credentials or self.vault_bindings):
            raise ValueError("vault credentials and bindings require vault=true")
        if self.vault and (not self.vault_credentials or not self.vault_bindings):
            raise ValueError("vault=true requires credentials and bindings")
        credential_names = [item.name for item in self.vault_credentials]
        binding_names = [item.name for item in self.vault_bindings]
        if len(credential_names) != len(set(credential_names)):
            raise ValueError("vault credential names must be unique")
        if len(binding_names) != len(set(binding_names)):
            raise ValueError("vault binding names must be unique")
        known = set(credential_names)
        for binding in self.vault_bindings:
            if binding.credential not in known:
                raise ValueError(
                    f"vault binding {binding.name!r} references unknown credential"
                )
        return self


class ExecutorConfig(BaseModel):
    """Execution backend config; sandbox is the fail-closed default."""
    enabled: bool = True          # compatibility only; false cannot select host execution
    backend: ExecutorBackend = ExecutorBackend.SANDBOX
    sandbox: SandboxConfig = SandboxConfig()

    model_config = {"extra": "ignore"}


def load_executor_config(path: Path) -> ExecutorConfig:
    """Read executor config; missing configuration defaults to sandbox.

    ``CC_HARNESS_EXECUTOR_BACKEND=native`` is an explicit host-execution opt-in.
    The legacy fallback setting is accepted but normalized to fail-closed.
    """
    if not path.exists():
        cfg = ExecutorConfig()
    else:
        import yaml
        raw = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
        cfg = ExecutorConfig(**(raw.get("executor") or {}))
    fallback_env = os.getenv("CC_HARNESS_SANDBOX_FALLBACK", "").strip().lower()
    if fallback_env == "native" or cfg.sandbox.fallback_on_error == "native":
        log.warning(
            "sandbox native fallback is retired; use executor.backend=native "
            "for explicit host execution"
        )
    cfg.sandbox.fallback_on_error = "hard"
    backend_env = os.getenv("CC_HARNESS_EXECUTOR_BACKEND", "").strip().lower()
    if backend_env in ("sandbox", "native"):
        cfg.backend = ExecutorBackend(backend_env)
    sandbox_port = os.getenv("CC_HARNESS_SANDBOX_SERVER_PORT", "").strip()
    if sandbox_port:
        try:
            parsed_port = int(sandbox_port)
        except ValueError as exc:
            raise ValueError("CC_HARNESS_SANDBOX_SERVER_PORT must be an integer") from exc
        if not 1 <= parsed_port <= 65535:
            raise ValueError("CC_HARNESS_SANDBOX_SERVER_PORT must be between 1 and 65535")
        cfg.sandbox.server_port = parsed_port
    sandbox_config = os.getenv("CC_HARNESS_SANDBOX_SERVER_CONFIG_PATH", "").strip()
    if sandbox_config:
        cfg.sandbox.server_config_path = Path(sandbox_config).expanduser().resolve()
    return cfg


class ContextConfig(BaseModel):
    """4-tier 上下文压缩配置(Plan3)。context_window=1M(deepseek-v4-flash 真实窗口)。

    threshold = 占窗口比例,触发各 tier:tier1(0.6)Snip / tier2(0.8)Prune /
    tier3(0.95)Summarize。protect_zone_tokens = 最近 N token 不压缩。
    """
    enabled: bool = True
    context_window: int = 1_000_000            # deepseek-v4-flash 真实窗口
    tier1_threshold: float = 0.6
    tier2_threshold: float = 0.8
    tier3_threshold: float = 0.95
    protect_zone_tokens: int = 8_192
    protected_tool_patterns: list[str] = []
    snip_head_lines: int = 5
    snip_tail_lines: int = 1
    summarize_max_output_tokens: int = 2_000
    fail_closed: bool = True
    context_window_source: str = "legacy-default"
    context_window_verified: bool = False

    model_config = {"extra": "ignore"}

    @model_validator(mode="after")
    def _validate(self) -> "ContextConfig":
        for threshold in (
            self.tier1_threshold, self.tier2_threshold, self.tier3_threshold,
        ):
            if not 0 < threshold < 1:
                raise ValueError(f"threshold {threshold} not in (0,1)")
        if not self.tier1_threshold < self.tier2_threshold < self.tier3_threshold:
            raise ValueError("thresholds must be strictly increasing")
        # Plan3 tier1:若调整上限,MemoryConfig.offload_ratio validator 上限也需同步(< tier1)
        if self.protect_zone_tokens < 0:
            raise ValueError("protect_zone_tokens must be non-negative")
        if self.context_window <= 0:
            raise ValueError("context_window must be positive")
        return self


def load_context_config(
    path: Path | None = None,
    *,
    model: str | None = None,
    require_known: bool = False,
    environ: dict[str, str] | None = None,
) -> ContextConfig:
    """从 CONTEXT_* env 构造;缺省默认(1M 窗口)。

    path 暂不读(policy.yaml 无 context 段);env 覆盖:CONTEXT_ENABLED / CONTEXT_WINDOW /
    CONTEXT_TIER1/2/3 / CONTEXT_PROTECT_TOKENS。
    """
    env = os.environ if environ is None else environ
    enabled = env.get("CONTEXT_ENABLED")
    cw = env.get("CONTEXT_WINDOW")
    t1, t2, t3 = env.get("CONTEXT_TIER1"), env.get("CONTEXT_TIER2"), env.get("CONTEXT_TIER3")
    pt = env.get("CONTEXT_PROTECT_TOKENS")
    kw: dict = {}
    if enabled is not None and enabled.strip():
        kw["enabled"] = enabled.strip().lower() in ("1", "true", "yes", "on")
    if cw:
        kw["context_window"] = int(cw)
        kw["context_window_source"] = "CONTEXT_WINDOW"
        kw["context_window_verified"] = True
    elif model:
        from cc_harness.model_capabilities import get_model_capability

        capability = get_model_capability(model)
        if capability is None:
            if require_known:
                raise ConfigError(
                    f"unknown context window for model {model!r}; set CONTEXT_WINDOW explicitly"
                )
        else:
            kw["context_window"] = capability.context_window
            kw["context_window_source"] = capability.source
            kw["context_window_verified"] = capability.verified
    if t1:
        kw["tier1_threshold"] = float(t1)
    if t2:
        kw["tier2_threshold"] = float(t2)
    if t3:
        kw["tier3_threshold"] = float(t3)
    if pt:
        kw["protect_zone_tokens"] = int(pt)
    return ContextConfig(**kw)


def load_memory_config(
    path: Path, *, environ: dict[str, str] | None = None
) -> "MemoryConfig":  # type: ignore[name-defined]
    """读 policy.yaml 的 `memory:` 段 + MEMORY_* env 覆盖;path 缺失→默认 MemoryConfig()。

    与 load_l2_config / load_policy_config 风格一致。MemoryConfig 定义在
    `cc_harness.memory.config`,此处**函数体内** lazy import 以避免循环依赖
    (memory/config.py 末尾 re-export 本函数)。env 覆盖优先于 yaml。

    env: MEMORY_PIPELINE_EVERY_N / MEMORY_SCENARIO_MIN_ATOMS / MEMORY_PERSONA_TRIGGER_N
    / MEMORY_RECALL_TOP_K / MEMORY_RECALL_TIMEOUT_S / MEMORY_LAYERED_INJECT
    / MEMORY_CAPTURE_ENABLED / MEMORY_PIPELINE_ENABLED
    / MEMORY_OFFLOAD_ENABLED / MEMORY_OFFLOAD_THRESHOLD / MEMORY_OFFLOAD_RATIO
    / MEMORY_MERMAID_MAX_TOKEN_RATIO / MEMORY_OFFLOAD_CANVAS_INJECT。
    """
    from cc_harness.memory.config import MemoryConfig  # lazy: dodge circular import
    env = os.environ if environ is None else environ
    kw: dict = {}
    if path.exists():
        import yaml
        raw = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
        kw = dict(raw.get("memory") or {})
    # 数值型 env 覆盖(无论 yaml 是否存在都生效,与 load_executor_config 一致)
    for key, env_name, cast in [
        ("pipeline_every_n", "MEMORY_PIPELINE_EVERY_N", int),
        ("scenario_min_atoms", "MEMORY_SCENARIO_MIN_ATOMS", int),
        ("persona_trigger_every_n", "MEMORY_PERSONA_TRIGGER_N", int),
        ("recall_top_k", "MEMORY_RECALL_TOP_K", int),
        ("recall_timeout_s", "MEMORY_RECALL_TIMEOUT_S", float),
        ("offload_threshold", "MEMORY_OFFLOAD_THRESHOLD", int),
        ("offload_ratio", "MEMORY_OFFLOAD_RATIO", float),
        ("mermaid_max_token_ratio", "MEMORY_MERMAID_MAX_TOKEN_RATIO", float),
    ]:
        v = env.get(env_name)
        if v is not None and v.strip():
            kw[key] = cast(v)
    # 布尔型 env 覆盖
    for key, env_name in [
        ("enabled", "MEMORY_ENABLED"),
        ("layered_inject", "MEMORY_LAYERED_INJECT"),
        ("capture_enabled", "MEMORY_CAPTURE_ENABLED"),
        ("pipeline_enabled", "MEMORY_PIPELINE_ENABLED"),
        ("offload_enabled", "MEMORY_OFFLOAD_ENABLED"),
        ("offload_canvas_inject", "MEMORY_OFFLOAD_CANVAS_INJECT"),
    ]:
        v = env.get(env_name)
        if v is not None and v.strip():
            kw[key] = v.strip().lower() in ("1", "true", "yes", "on")
    for key, env_name, cast in [
        ("db_base_dir", "MEMORY_DB_DIR", Path),
        ("embedding_base_url", "EMBEDDING_BASE_URL", str),
        ("embedding_api_key", "EMBEDDING_API_KEY", str),
        ("embedding_model", "EMBEDDING_MODEL", str),
        ("embedding_dim", "EMBEDDING_DIM", int),
    ]:
        v = env.get(env_name)
        if v is not None and str(v).strip():
            kw[key] = cast(v)
    return MemoryConfig(**kw)
