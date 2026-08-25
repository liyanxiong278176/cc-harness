"""权限闸门:hard-deny 先于 allowlist 和 allow / ask 决策。

工具分级:
  allow — 工作区内 fs-read/list、git-read、context7 查文档
  ask   — run_command(任何 shell)、fs-write、工作区外 fs-read、网络工具、git-write、未知工具
  deny  — 未显式授权的工作区外路径、敏感凭据路径

工作区边界是不可询问的 hard-deny;额外目录只能由启动配置显式加入。
会话 allowlist(进程内)记录用户选 "always" 的 (tool, 规范化键),命中则 allow。
"""
from __future__ import annotations

import os
from collections.abc import Iterator
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from urllib.parse import unquote, urlparse
from urllib.request import url2pathname

from cc_harness.security import build_action_plan


class Action(str, Enum):
    ALLOW = "allow"
    ASK = "ask"
    DENY = "deny"


@dataclass(frozen=True)
class Decision:
    action: Action
    rule_id: str
    reason: str
    evidence: dict = field(default_factory=dict)

    @property
    def allow(self) -> bool:
        return self.action is Action.ALLOW

    @property
    def deny(self) -> bool:
        return self.action is Action.DENY


# --- 工具分级(按名字模式)---
_FS_READ = ("read", "list", "search", "info", "stat", "grep", "glob")
_FS_WRITE = ("write", "edit", "move", "rename", "delete", "remove", "create", "mkdir", "touch")
_NET = ("fetch", "bing", "http", "url", "request", "curl", "wget")

_PATH_KEYS = {
    "path", "file_path", "filePath", "filename", "uri", "cwd",
    "source", "destination", "src", "dst", "old_path", "new_path",
}
_PATH_LIST_KEYS = {"paths", "files"}
_SENSITIVE_DIR_NAMES = {".ssh", ".aws", ".azure", ".cc-harness", ".gnupg", ".kube"}
_SENSITIVE_FILE_NAMES = {
    ".env", ".git-credentials", ".netrc", ".npmrc", ".pypirc",
    "id_dsa", "id_ecdsa", "id_ed25519", "id_rsa",
}
_NON_SECRET_ENV_SUFFIXES = (".example", ".sample", ".template")


def _classify(name: str) -> str:
    n = name.lower()
    if n == "run_command":
        return "shell"
    if n in ("read", "glob", "grep"):
        return "fs_read"
    if n in ("edit", "write"):
        return "fs_write"
    if n == "memory_save":
        return "fs_write"
    if n == "memory_recall":
        return "fs_read"
    if "context7" in n:
        return "docs"
    if "git" in n:
        # git 读类放行,写类询问
        if any(k in n for k in ("log", "status", "diff", "show", "branch", "list")):
            return "git_read"
        return "git_write"
    if "filesystem" in n or "_fs_" in n:
        if any(k in n for k in _FS_READ):
            return "fs_read"
        if any(k in n for k in _FS_WRITE):
            return "fs_write"
        return "fs_other"
    if any(k in n for k in _NET):
        return "network"
    return "unknown"


def _extract_path(args: dict) -> str | None:
    for k in ("path", "file_path", "filePath", "filename", "uri"):
        v = args.get(k)
        if isinstance(v, str) and v:
            return v
    return None


def _path_from_value(key: str, value: str) -> str | None:
    """Return a local path, ignoring non-file URIs in generic ``uri`` fields."""
    if key != "uri":
        return value
    parsed = urlparse(value)
    if parsed.scheme and parsed.scheme != "file":
        return None
    if parsed.scheme == "file":
        path = unquote(parsed.path)
        if parsed.netloc and parsed.netloc not in ("", "localhost"):
            path = f"//{parsed.netloc}{path}"
        return url2pathname(path)
    return value


def _extract_paths(args: dict) -> Iterator[str]:
    """Yield every declared local path so secondary destinations cannot bypass policy."""
    for key, value in args.items():
        if key in _PATH_KEYS and isinstance(value, str) and value:
            path = _path_from_value(key, value)
            if path:
                yield path
        elif key in _PATH_LIST_KEYS and isinstance(value, list):
            for item in value:
                if isinstance(item, str) and item:
                    yield item


def _resolve(target: str, project_root: Path) -> Path:
    """展开 ~ / 环境变量 / 相对路径,返回绝对路径(不要求存在)。"""
    expanded = os.path.expandvars(os.path.expanduser(target))
    p = Path(expanded)
    if not p.is_absolute():
        p = (project_root / p)
    try:
        return p.resolve(strict=False)
    except Exception:
        return p


def _is_outside(target: str, project_root: Path) -> bool:
    """True 若 target 解析后落在 project_root 之外。

    resolve() 会把 `src/../../.ssh/x` 折叠成 `<root>/../.ssh/x` 的绝对形式,
    再用 is_relative_to 判归属(Python 3.9+,本仓库 3.11 可用)。
    """
    root = project_root.resolve(strict=False)
    return not _resolve(target, root).is_relative_to(root)


def is_sensitive_path(path: Path) -> bool:
    """Identify credential locations that models must access through a broker."""
    lowered_parts = {part.lower() for part in path.parts}
    if lowered_parts & _SENSITIVE_DIR_NAMES:
        return True
    name = path.name.lower()
    if ".git" in lowered_parts and name in {"config", "config.worktree"}:
        return True
    if (
        (name == ".env" or name.startswith(".env."))
        and not name.endswith(_NON_SECRET_ENV_SUFFIXES)
    ):
        return True
    return name in _SENSITIVE_FILE_NAMES


class Allowlist:
    """会话内 allowlist:存 (tool, 规范化键)。规范化键 = shell 的 command / fs 的 resolved path / 其它为空。"""

    def __init__(self) -> None:
        self._entries: set[tuple[str, str]] = set()

    @staticmethod
    def _key(tool_name: str, args: dict, project_root: Path | None = None) -> str:
        cls = _classify(tool_name)
        if cls == "shell":
            return args.get("command", "")
        if cls in ("fs_read", "fs_write", "fs_other"):
            p = _extract_path(args)
            if not p:
                return ""
            # project_root 缺省时退化为原始路径字符串(shell/docs 类不依赖它)
            return str(_resolve(p, project_root)) if project_root is not None else p
        return ""

    def add(self, tool_name: str, args: dict, project_root: Path | None = None) -> None:
        self._entries.add((tool_name, self._key(tool_name, args, project_root)))

    def hits(self, tool_name: str, args: dict, project_root: Path | None = None) -> bool:
        return (tool_name, self._key(tool_name, args, project_root)) in self._entries


class PolicyEngine:
    def __init__(
        self,
        project_root: Path,
        *,
        enabled: bool = True,
        additional_roots: list[Path] | tuple[Path, ...] | None = None,
        provenance_mode: bool = False,
    ) -> None:
        self.project_root = project_root.resolve(strict=False)
        self.additional_roots = tuple(
            Path(root).resolve(strict=False) for root in (additional_roots or ())
        )
        self.enabled = enabled
        # Opt-in so existing callers preserve the historical permission
        # prompts.  Strict benchmark/production profiles enable it explicitly;
        # it extends this policy engine rather than creating a second gate.
        self.provenance_mode = provenance_mode
        self.allowlist = Allowlist()

    def evaluate(self, tool_name: str, args: dict, ctx: dict) -> Decision:
        root = Path(ctx.get("project_root", self.project_root)).resolve(strict=False)
        cls = _classify(tool_name)
        allowed_roots = (root, *self.additional_roots)

        provenance_decision: Decision | None = None
        provenance_capability = None
        provenance_evidence: dict = {}
        if bool(ctx.get("provenance_mode", self.provenance_mode)):
            plan = build_action_plan(
                tool_name,
                args,
                messages=ctx.get("messages"),
                tool_results=ctx.get("tool_results"),
                tool_result_records=ctx.get("tool_result_records"),
                capability_metadata=ctx.get("capability_metadata"),
            )
            provenance_capability = plan.capability
            evidence = plan.audit_record()
            provenance_evidence = evidence
            # Credentials and policy/permission controls remain hard denied.
            # This is deliberately narrower than the old "any unknown field"
            # deny, so ordinary external-write requests can reach confirmation
            # instead of being misclassified as an attack.
            if plan.capability.credential and plan.has_untrusted_fields:
                provenance_decision = Decision(
                    Action.DENY,
                    "untrusted_credential_argument",
                    "拒绝使用无法追溯来源的凭据或敏感参数",
                    evidence,
                )
            elif plan.capability.high_risk and plan.untrusted_tool_fields:
                provenance_decision = Decision(
                    Action.DENY,
                    "untrusted_tool_argument",
                    "拒绝把带指令污染的工具结果带入高风险动作参数",
                    evidence,
                )
            elif plan.hard_boundary_fields:
                provenance_decision = Decision(
                    Action.DENY,
                    "untrusted_security_control",
                    "拒绝让不可信字段改变策略、权限或秘密边界",
                    evidence,
                )
            # A declared read capability may continue with tainted values;
            # taint is retained in the audit record and cannot grant a write.
            elif plan.capability.effect.value != "read" and plan.has_untrusted_fields:
                provenance_decision = Decision(
                    Action.ASK,
                    "untrusted_action_confirmation",
                    "动作参数来源不完整，需要用户确认后继续",
                    evidence,
                )
            elif plan.capability.high_risk and not plan.fields:
                provenance_decision = Decision(
                    Action.ASK,
                    "provenance_unknown",
                    "高风险工具缺少可验证的参数来源，需要用户明确授权",
                    evidence,
                )

        # Hard safety runs before every convenience control. Neither an allowlist
        # entry nor enabled=false may approve an undeclared root or credential path.
        for target in _extract_paths(args):
            resolved = _resolve(target, root)
            containing_root = next(
                (allowed for allowed in allowed_roots if resolved.is_relative_to(allowed)),
                None,
            )
            if containing_root is None:
                return Decision(
                    Action.DENY,
                    "path_outside_allowed_roots",
                    f"拒绝访问未授权工作区外路径: {target}",
                )
            # Ancestors of an explicitly authorized root are not addressable by
            # the model. Only credential locations inside that root are sensitive.
            if is_sensitive_path(resolved.relative_to(containing_root)):
                return Decision(
                    Action.DENY,
                    "sensitive_credential_path",
                    f"拒绝直接访问敏感凭据路径: {target}",
                )

        if provenance_decision is not None:
            return provenance_decision

        if not self.enabled:
            return Decision(Action.ALLOW, "policy_prompts_disabled", "普通权限询问已关闭")

        # allowlist 命中 → allow
        if self.allowlist.hits(tool_name, args, root):
            return Decision(Action.ALLOW, "allowlist", "会话 allowlist 命中")

        # All declared paths passed hard safety above. Read-like tools can now
        # be allowed without relying on tool-name classification for containment.
        if cls in ("docs", "git_read", "fs_read", "fs_other"):
            return Decision(Action.ALLOW, f"{cls}_allow", "", provenance_evidence)

        if provenance_capability is not None:
            if provenance_capability.effect.value == "read":
                return Decision(
                    Action.ALLOW,
                    "declared_read_capability_allow",
                    "",
                    provenance_evidence,
                )

        # shell / fs_write / network / git_write / unknown → ask
        reason = {
            "shell": "执行 shell 命令需用户确认",
            "fs_write": "写/改文件需用户确认",
            "network": "网络访问需用户确认",
            "git_write": "git 写操作需用户确认",
            "unknown": "未知工具,需用户确认",
        }.get(cls, "该操作需用户确认")
        # 丰富原因:命中 is_dangerous 正则时提示
        if cls == "shell":
            try:
                from cc_harness.tools import is_dangerous
                if is_dangerous(tool_name, args):
                    reason += "(命中危险命令模式)"
            except Exception:
                pass
        return Decision(Action.ASK, f"{cls}_ask", reason)

    def evaluate_shadow(self, tool_name: str, args: dict, ctx: dict) -> Decision:
        """Evaluate the counterfactual without executing any tool.

        This is intentionally a policy-only diagnostic.  It keeps path and
        credential hard checks, but disables provenance-derived denials so a
        development report can tell whether the provenance rule was the
        direct gate.  Callers must never dispatch the returned decision as an
        authorization result.
        """

        shadow_ctx = dict(ctx)
        shadow_ctx["provenance_mode"] = False
        decision = self.evaluate(tool_name, args, shadow_ctx)
        evidence = dict(decision.evidence)
        evidence["shadow"] = True
        evidence["shadow_of_provenance_mode"] = bool(
            ctx.get("provenance_mode", self.provenance_mode)
        )
        return Decision(
            decision.action,
            f"shadow:{decision.rule_id}",
            decision.reason,
            evidence,
        )
