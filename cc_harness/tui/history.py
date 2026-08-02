"""输入历史管理 + Ctrl+R 反向搜。"""
from __future__ import annotations


class History:
    def __init__(self, max_size: int = 1000) -> None:
        self.entries: list[str] = []
        self.max_size = max_size

    def append(self, entry: str) -> None:
        if not entry.strip():
            return
        # 去重:与最后一条相同就不重复
        if self.entries and self.entries[-1] == entry:
            return
        self.entries.append(entry)
        if len(self.entries) > self.max_size:
            self.entries = self.entries[-self.max_size :]

    def search(self, query: str) -> list[str]:
        """substr 反向搜,最新匹配在前。"""
        if not query:
            return list(reversed(self.entries[-10:]))
        matches = [e for e in self.entries if query in e]
        return list(reversed(matches))