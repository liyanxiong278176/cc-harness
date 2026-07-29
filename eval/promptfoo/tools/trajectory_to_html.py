"""JSONL trajectory → 自包含 HTML。零依赖,不碰 web/src。

Eval red-team 跑完后,各 attack 的 trajectory 落 `<root>/.traj-<pid>-<ts>.jsonl`
(wrapper `_call_api_inner`)。本工具读一目录的 JSONL → 一份自包含 HTML:
- 4 段流式(思考/行动/观察/结果)按事件顺序
- borderline attack 视觉高亮(CSS class)
- 步数折叠由 CSS 控制(本 MVP 不展开 summary stats)

渲染逻辑独立成纯函数 `render_trajectory_html`,可未来并进 web 前端
(本工具不 import `web/src/**`)。
"""
from __future__ import annotations
import argparse
import html
import json
from pathlib import Path

from cc_harness.web.events import parse_jsonl_line

_PHASE_LABEL = {"thought": "思考", "action": "行动", "observation": "观察", "result": "结果"}

# 自包含 HTML 骨架(4 段配色 + borderline 边框)。dark theme,无外部资源。
_HTML_HEAD = """<html><head><meta charset='utf-8'><title>Trajectory Report</title>
<style>
body{font-family:ui-monospace,monospace;background:#0f1419;color:#e6edf3;margin:24px}
.attack{border:1px solid #30363d;border-radius:8px;padding:16px;margin:16px 0}
.attack.borderline{border-color:#d29922}
h2{margin-top:0;font-size:16px;color:#7d8590}
.ev{padding:4px 0;border-left:2px solid #30363d;padding-left:12px;margin:4px 0}
.thought{color:#7d8590}
.action{color:#58a6ff}
.observation{color:#3fb950}
.result{color:#bc8cff}
.borderline-flag{color:#d29922;font-weight:bold}
</style></head><body>"""


def render_trajectory_html(trajs: dict[str, list], borderline_ids: set[str] | None = None) -> str:
    """trajs: {attack_id: [Event, ...]}。纯函数,可测,可未来并进 web。

    XSS 安全:所有用户/代理可控字符串(aid / event text / action name+args)
    都过 `html.escape`。Phase label 是常量,无需 escape。
    """
    borderline_ids = borderline_ids or set()
    parts = [_HTML_HEAD]
    for aid, events in trajs.items():
        cls = "attack borderline" if aid in borderline_ids else "attack"
        flag = (
            ' <span class="borderline-flag">⚠ borderline</span>'
            if aid in borderline_ids
            else ""
        )
        parts.append(f'<div class="{cls}"><h2>{html.escape(aid)}{flag}</h2>')
        for ev in events:
            etype = getattr(ev, "type", "unknown")
            label = _PHASE_LABEL.get(etype, etype)
            text = html.escape(_event_text(ev))
            parts.append(
                f'<div class="ev {html.escape(etype, quote=True)}">'
                f'<b>{html.escape(label)}</b>: {text}</div>'
            )
        parts.append("</div>")
    parts.append("</body></html>")
    return "".join(parts)


def _event_text(ev) -> str:
    """从 Event 提取可读文本。action: name(args-json);其余: text/message/str。"""
    etype = getattr(ev, "type", "")
    if etype == "action":
        name = getattr(ev, "name", "?")
        args = getattr(ev, "args", {}) or {}
        return f"{name}({json.dumps(args, ensure_ascii=False)})"
    return getattr(ev, "text", getattr(ev, "message", str(ev)))


def _load_traj(jsonl_path: Path) -> list:
    """读 JSONL,逐行 `parse_jsonl_line`,None(空/非法)跳过。"""
    events = []
    for line in jsonl_path.read_text(encoding="utf-8").splitlines():
        ev = parse_jsonl_line(line)
        if ev is not None:
            events.append(ev)
    return events


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("dir", help="trajectory JSONL 目录(每文件一 attack)")
    ap.add_argument("-o", "--output", default="trajectory-report.html")
    ap.add_argument(
        "--borderline",
        nargs="*",
        default=[],
        help="borderline attack_id 列表(空格分隔,与文件 stem 一致)",
    )
    a = ap.parse_args()
    trajs = {}
    for p in sorted(Path(a.dir).glob("*.jsonl")):
        trajs[p.stem] = _load_traj(p)
    Path(a.output).write_text(
        render_trajectory_html(trajs, borderline_ids=set(a.borderline)),
        encoding="utf-8",
    )
    print(f"wrote {a.output} ({len(trajs)} attacks)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
