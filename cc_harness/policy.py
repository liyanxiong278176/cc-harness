"""Claude Code 式权限闸门:决策只有 allow / ask 两档,无 deny。

工具分级:
  allow — 工作区内 fs-read/list、git-read、context7 查文档
  ask   — run_command(任何 shell)、fs-write、工作区外 fs-read、网络工具、git-write、未知工具

"工作区外读 → ask" 是结构性判断(路径归属),不是敏感路径黑名单。
会话 allowlist(进程内)记录用户选 "always" 的 (tool, 规范化键),命中则 allow。
"""
from __future__ import annotations
import os
from dataclasses import dataclass
from enum import Enum
from pathlib import Path


class Action(str, Enum):
    ALLOW = "allow"
    ASK = "ask"


@dataclass(frozen=True)
class Decision:
    action: Action
    rule_id: str
    reason: str

    @property
    def allow(self) -> bool:
        return self.action is Action.ALLOW


# --- 工具分级(按名字模式)---
_FS_READ = ("read", "list", "search", "info", "stat", "grep", "glob")
_FS_WRITE = ("write", "edit", "move", "rename", "delete", "remove", "create", "mkdir", "touch")
_NET = ("fetch", "bing", "http", "url", "request", "curl", "wget")


def _classify(name: str) -> str:
    n = name.lower()
    if n == "run_command":
        return "shell"
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
    def __init__(self, project_root: Path, *, enabled: bool = True) -> None:
        self.project_root = project_root.resolve(strict=False)
        self.enabled = enabled
        self.allowlist = Allowlist()

    def evaluate(self, tool_name: str, args: dict, ctx: dict) -> Decision:
        if not self.enabled:
            return Decision(Action.ALLOW, "policy_disabled", "闸门已关闭(policy.yaml enabled=false)")
        root = Path(ctx.get("project_root", self.project_root)).resolve(strict=False)
        cls = _classify(tool_name)

        # allowlist 命中 → allow
        if self.allowlist.hits(tool_name, args, root):
            return Decision(Action.ALLOW, "allowlist", "会话 allowlist 命中")

        if cls == "docs" or cls == "git_read":
            return Decision(Action.ALLOW, f"{cls}_allow", "")

        if cls in ("fs_read", "fs_other"):
            target = _extract_path(args)
            if target and _is_outside(target, root):
                return Decision(Action.ASK, "fs_outside_workspace",
                                f"读取工作区外路径需确认: {target}")
            return Decision(Action.ALLOW, f"{cls}_allow", "")

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
