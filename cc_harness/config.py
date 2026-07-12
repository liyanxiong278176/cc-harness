import json
import os
from enum import Enum
from pathlib import Path
from typing import Literal, TYPE_CHECKING

from dotenv import load_dotenv
from pydantic import BaseModel, model_validator

if TYPE_CHECKING:
    # Runtime import would be circular (memory.config re-exports load_memory_config).
    # load_memory_config lazy-imports MemoryConfig inside its body instead.
    from cc_harness.memory.config import MemoryConfig


class MCPServerConfig(BaseModel):
    type: Literal["stdio", "sse", "http", "streamable-http"] = "stdio"
    command: str | None = None
    args: list[str] = []
    url: str | None = None
    env: dict[str, str] = {}

    @property
    def transport_type(self) -> Literal["stdio", "sse", "http"]:
        if self.type in ("http", "streamable-http"):
            return "http"
        return self.type  # type: ignore[return-value]


class ConfigError(Exception):
    pass


class AppConfig(BaseModel):
    openai_api_key: str
    openai_base_url: str
    openai_model: str
    mcp_servers: dict[str, MCPServerConfig]

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
    )


class PolicyConfig(BaseModel):
    """权限闸门配置。M1 只暴露 enabled(杀手开关)。
    审计路径固定 <项目根>/logs/policy.jsonl(agent 写死),不在此配置。"""
    enabled: bool = True

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


class SandboxConfig(BaseModel):
    """沙箱执行器配置(policy.yaml 的 executor.sandbox 段)。

    RESERVED(deferred — SDK 锁定时消费):cpu / memory_mb / egress_allow / vault 四字段
    解析但暂未传入 Sandbox.create。真 SDK kwargs 是 resource= / network_policy= /
    credential_proxy=(Task 12 WebSearch 发现锁定);当前 _ensure_sandbox 的 create kwargs
    仍是 placeholder(mounts=/workdir=)。server_host / server_port 已在 Gap 1 后生效(经
    ensure_server + ConnectionConfig domain)。timeout_s 同样已消费。
    """
    server_host: str = "127.0.0.1"   # 用 127.0.0.1 非 localhost(Windows IPv6 ::1 连不上绑 127.0.0.1 的 server)
    server_port: int = 8000
    image: str = "cc-harness-runtime:local"
    timeout_s: int = 120          # 沙箱命令超时(比 native 30s 长,含容器开销)
    cpu: int = 2                  # RESERVED → SDK resource=(Task 12 锁定)
    memory_mb: int = 2048         # RESERVED → SDK resource=(Task 12 锁定)
    egress_allow: list[str] = ["api.deepseek.com", "api.siliconflow.cn",
                               "pypi.org", "github.com"]   # RESERVED → SDK network_policy=
    vault: bool = True            # RESERVED → SDK credential_proxy=(Credential Vault;失败退化 strip_secrets)
    # hard 模式(报错不降级,红队严格测)Plan 2 红队适配时实现;当前仅 native 降级生效
    # (tools.run_command 无条件 catch SandboxUnavailableError → 降级,不读本字段)。
    fallback_on_error: str = "native"   # native(降级) | hard(报错)

    model_config = {"extra": "ignore"}


class ExecutorConfig(BaseModel):
    """执行后端配置。缺省 native(现状);sandbox 启用 OpenSandbox。"""
    enabled: bool = True          # 总开关:false = 强制 native(紧急回退)
    backend: ExecutorBackend = ExecutorBackend.NATIVE
    sandbox: SandboxConfig = SandboxConfig()

    model_config = {"extra": "ignore"}


def load_executor_config(path: Path) -> ExecutorConfig:
    """读 policy.yaml 的 `executor:` 段;文件/段缺失→默认(native)。

    env CC_HARNESS_SANDBOX_FALLBACK=hard|native 覆盖 sandbox.fallback_on_error
    (红队 allow 模式 wrapper 注,绑死:测沙箱时沙箱挂了不降级 → 防 L8 失真 +
    防 CI secret 经降级路径泄露)。hard 优先级高于 policy.yaml。
    """
    if not path.exists():
        cfg = ExecutorConfig()
    else:
        import yaml
        raw = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
        cfg = ExecutorConfig(**(raw.get("executor") or {}))
    fallback_env = os.getenv("CC_HARNESS_SANDBOX_FALLBACK", "").strip().lower()
    if fallback_env in ("hard", "native"):
        cfg.sandbox.fallback_on_error = fallback_env
    # env override backend(allow 红队强制 sandbox:CI 无 policy.yaml 时默认 native,
    # allow 模式不强制 sandbox → 命令进 NativeExecutor 在宿主跑 → L8 假数据 + 真泄露)。
    backend_env = os.getenv("CC_HARNESS_EXECUTOR_BACKEND", "").strip().lower()
    if backend_env in ("sandbox", "native"):
        cfg.backend = ExecutorBackend(backend_env)
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

    model_config = {"extra": "ignore"}

    @model_validator(mode="after")
    def _validate(self) -> "ContextConfig":
        for t in (self.tier1_threshold, self.tier2_threshold, self.tier3_threshold):
            assert 0 < t < 1, f"threshold {t} not in (0,1)"
        assert self.tier1_threshold < self.tier2_threshold < self.tier3_threshold, \
            "thresholds must be strictly increasing"
        assert self.protect_zone_tokens >= 0 and self.context_window > 0
        return self


def load_context_config(path: Path | None = None) -> ContextConfig:
    """从 CONTEXT_* env 构造;缺省默认(1M 窗口)。

    path 暂不读(policy.yaml 无 context 段);env 覆盖:CONTEXT_WINDOW /
    CONTEXT_TIER1/2/3 / CONTEXT_PROTECT_TOKENS。
    """
    cw = os.getenv("CONTEXT_WINDOW")
    t1, t2, t3 = os.getenv("CONTEXT_TIER1"), os.getenv("CONTEXT_TIER2"), os.getenv("CONTEXT_TIER3")
    pt = os.getenv("CONTEXT_PROTECT_TOKENS")
    kw: dict = {}
    if cw:
        kw["context_window"] = int(cw)
    if t1:
        kw["tier1_threshold"] = float(t1)
    if t2:
        kw["tier2_threshold"] = float(t2)
    if t3:
        kw["tier3_threshold"] = float(t3)
    if pt:
        kw["protect_zone_tokens"] = int(pt)
    return ContextConfig(**kw)


def load_memory_config(path: Path) -> "MemoryConfig":  # type: ignore[name-defined]
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
        v = os.getenv(env_name)
        if v is not None and v.strip():
            kw[key] = cast(v)
    # 布尔型 env 覆盖
    for key, env_name in [
        ("layered_inject", "MEMORY_LAYERED_INJECT"),
        ("capture_enabled", "MEMORY_CAPTURE_ENABLED"),
        ("pipeline_enabled", "MEMORY_PIPELINE_ENABLED"),
        ("offload_enabled", "MEMORY_OFFLOAD_ENABLED"),
        ("offload_canvas_inject", "MEMORY_OFFLOAD_CANVAS_INJECT"),
    ]:
        v = os.getenv(env_name)
        if v is not None and v.strip():
            kw[key] = v.strip().lower() in ("1", "true", "yes", "on")
    return MemoryConfig(**kw)
