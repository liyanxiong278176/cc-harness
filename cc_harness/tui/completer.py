"""Tab 补全:slash 命令 + @path 文件。"""
from __future__ import annotations

from pathlib import Path


SLASH_COMMANDS = [
    "/help", "/exit", "/clear", "/context", "/memory",
    "/config", "/tools", "/mcp", "/hitl",
    "/policy", "/audit", "/index", "/search", "/plan",
    "/team", "/model", "/usage", "/skill",
    "/snapshot", "/restore", "/theme", "/resume",
    "/reset", "/version",
]


class Completer:
    """完成 slash 命令和 @path 文件路径。"""

    def __init__(self, cwd: str) -> None:
        self.cwd = Path(cwd)

    def complete(self, prefix: str) -> list[str]:
        """根据 prefix 性质补全。

        - "/...":在 SLASH_COMMANDS 里前缀匹配
        - "@...":把 prefix[1:] 当相对路径在 cwd 下展开
        - 其他:返回 []
        """
        if prefix.startswith("/"):
            return [c for c in SLASH_COMMANDS if c.startswith(prefix)]
        if prefix.startswith("@"):
            return self._complete_path(prefix[1:])
        return []

    def _complete_path(self, prefix: str) -> list[str]:
        """@path 补全:支持相对路径与文件名部分匹配。"""
        # 空 prefix → 列 cwd 顶层文件/目录(用作 "show all")
        if not prefix:
            try:
                return ["@" + p.name for p in self.cwd.iterdir()]
            except OSError:
                return []
        # prefix 含分隔符或定位到目录 → 列该目录全部
        # 否则 → 按文件名 startswith 过滤(保留 prefix 文字)
        try:
            target = self.cwd / prefix
        except (OSError, ValueError):
            return []
        if target.is_dir():
            try:
                return [f"@{prefix}{p.name}" for p in target.iterdir()]
            except OSError:
                return []
        # 文件模式:列父目录下名字以 target.name 开头的所有条目
        parent = target.parent
        partial = target.name
        if not parent.exists():
            return []
        try:
            return [
                f"@{prefix}{p.name}"
                for p in parent.iterdir()
                if p.name.startswith(partial)
            ]
        except OSError:
            return []