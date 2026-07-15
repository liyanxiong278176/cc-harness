"""cc_harness.cli._shared — CLI 层共享 helpers。

包含:
    load_manifest_or_exit(cwd) — 加载 manifest,缺失 → 提示并 exit 1
    cli_session_id() — 生成一次性 CLI session id(`cli-{ts}-{hex[:8]}`,spec line 450)
    print_text() / print_error() — Rich + fallback 文本输出
    JsonOrText — TTY/JSON 检测的统一 sink(rich.Table ↔ plain text ↔ JSON)

约定:
    - stdout 用于普通输出(人类可读 / rich 表格)
    - stderr 用于错误与提示
    - 输出在非 TTY 环境(captured by capsys / pipe)统一降级为纯文本,
      便于测试和 shell 拼装(`jq`/`grep` 友好)。
    - `--json` flag 强制所有输出为 JSON,无论 TTY 与否。
"""
from __future__ import annotations

import json
import sys
import time
import uuid
from argparse import Namespace
from pathlib import Path
from typing import Any

from rich.console import Console

from cc_harness.project.manifest import load_manifest
from cc_harness.project.models import Manifest


# ---------------------------------------------------------------------------
# manifest loader
# ---------------------------------------------------------------------------


def load_manifest_or_exit(cwd: Path) -> Manifest:
    """加载项目 manifest;缺失 → stderr 提示并 `sys.exit(1)`。

    Args:
        cwd: 项目根目录。

    Returns:
        解析后的 Manifest。

    Side effects:
        - manifest 缺失:写 stderr(不带 rich markup)+ raise SystemExit(1)。
    """
    m = load_manifest(cwd)
    if m is None:
        sys.stderr.write(
            f"✗ No .cc-harness/project.yaml found in {cwd}. "
            f"Run `cc-harness init` first.\n"
        )
        sys.exit(1)
    return m


# ---------------------------------------------------------------------------
# session id — 一次性 CLI session 标识
# ---------------------------------------------------------------------------


def cli_session_id() -> str:
    """生成 CLI 会话 id:`cli-{int_ts}-{hex8}`(spec line 450)。

    透传到 TodoService.create/update → active_sessions。
    """
    return f"cli-{int(time.time())}-{uuid.uuid4().hex[:8]}"


# ---------------------------------------------------------------------------
# 输出 helpers
# ---------------------------------------------------------------------------


def print_text(console: Console, text: str) -> None:
    """打印普通输出(stdout)。TTY 走 rich,否则 plain text。

    关键:`[xxx]` 在 Rich 是 markup(颜色/样式),但 CLI 输出里
    `[todo_create]` 这种 literal bracket 标记必须保留 → 用 `markup=False`。
    """
    console.print(text, markup=False, highlight=False)


def print_error(console: Console, text: str) -> None:
    """打印错误到 stderr,使用 rich 红色加 ×(有 tty)/纯文本(无 tty)。

    注:Rich Console 默认写到 stdout;这里通过 `file=sys.stderr` 重定向。
    设计上 stderr 不太需要 rich 装饰 — 用 red prefix 的纯文本。
    """
    # stderr 不用 rich(避免 capsys.readouterr() 双 Console 冲突);
    # 简单写 ASCII 前缀,测试和 shell 友好。
    sys.stderr.write(f"✗ {text}\n")
    sys.stderr.flush()
    del console  # 接口对齐入口,实际不消费


# ---------------------------------------------------------------------------
# JsonOrText — TTY/JSON/纯文本统一 sink
# ---------------------------------------------------------------------------


class JsonOrText:
    """TTY/JSON/纯文本渲染器。

    用法:
        sink = JsonOrText(console, args)          # args.json 控制
        sink.print_table(tasks, columns=...)      # tty → rich Table,else plain
        sink.print_dict({"foo": 1})               # json → dict dump,else plain

    决策链:
        args.json=True              → 全部走 JSON(stdout)
        sys.stdout.isatty()=True    → Rich Table(tty=True)
        else                        → 纯文本(无 markup,空格分隔)

    TTY 检测在构造时锁定一次,避免测试中 monkeypatch sys.stdout 行为漂移。
    """

    def __init__(self, console: Console, args: Namespace) -> None:
        self.console = console
        self.json_mode = bool(getattr(args, "json", False))
        self.is_tty = sys.stdout.isatty()

    # --------------------------------------------------------------- #
    # 公开渲染
    # --------------------------------------------------------------- #

    def print_table(
        self,
        rows: list[Any],
        *,
        title: str,
        columns: list[tuple[str, str]],
    ) -> None:
        """渲染行列表。

        Args:
            rows: 对象列表(可用 `obj.attr` 或 dict 取值)。
            title: 顶部标题。
            columns: 列定义 — [(key, header), ...]。
                - key 在 rows 为 dataclass 时,通过 getattr 取值。
                - 在 rows 为 dict 时,通过 key 取值。
                - header 是列标题。
        """
        if self.json_mode:
            payload = [
                {c[0]: _extract(row, c[0]) for c in columns} for row in rows
            ]
            sys.stdout.write(json.dumps(payload, default=str, ensure_ascii=False))
            sys.stdout.write("\n")
            sys.stdout.flush()
            return

        if self.is_tty:
            from rich.table import Table

            table = Table(title=title, show_lines=False, header_style="bold")
            for _, header in columns:
                table.add_column(header)
            for row in rows:
                table.add_row(*(_format_cell(_extract(row, c[0])) for c, _ in columns))
            self.console.print(table)
            return

        # 纯文本降级:空格分隔(标题单独一行)
        if title:
            sys.stdout.write(f"=== {title} ===\n")
        for row in rows:
            cells = [_format_cell(_extract(row, c[0])) for c, _ in columns]
            sys.stdout.write("  ".join(cells) + "\n")

    def print_dict(self, payload: dict) -> None:
        """渲染单对象。JSON mode → json.dumps,else → key=value 行。"""
        if self.json_mode:
            sys.stdout.write(json.dumps(payload, default=str, ensure_ascii=False))
            sys.stdout.write("\n")
            sys.stdout.flush()
            return
        for k, v in payload.items():
            sys.stdout.write(f"{k}: {v}\n")

    def print_text(self, text: str) -> None:
        """渲染原始文本(不参与 json 决策 — 极简旁路,统一入口)。"""
        if self.json_mode:
            sys.stdout.write(json.dumps({"text": text}, ensure_ascii=False) + "\n")
            return
        sys.stdout.write(text)
        if not text.endswith("\n"):
            sys.stdout.write("\n")
        sys.stdout.flush()


def _extract(row: Any, key: str) -> Any:
    """从 dataclass / dict / 普通对象上取值。"""
    if isinstance(row, dict):
        return row.get(key)
    return getattr(row, key, None)


def _format_cell(value: Any) -> str:
    """单元格式化:None → '(none)',list → 'a,b,c',其他 str() 化。"""
    if value is None:
        return "(none)"
    if isinstance(value, list):
        if not value:
            return "(none)"
        return ",".join(str(v) for v in value)
    if isinstance(value, bool):
        return "true" if value else "false"
    return str(value)


__all__ = [
    "JsonOrText",
    "cli_session_id",
    "load_manifest_or_exit",
    "print_error",
    "print_text",
]
