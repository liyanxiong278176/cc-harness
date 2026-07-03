import json
import os
from enum import Enum
from pathlib import Path
from typing import Literal

from dotenv import load_dotenv
from pydantic import BaseModel


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
    """读 policy.yaml 的 `executor:` 段;文件/段缺失→默认(native)。"""
    if not path.exists():
        return ExecutorConfig()
    import yaml
    raw = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    return ExecutorConfig(**(raw.get("executor") or {}))
