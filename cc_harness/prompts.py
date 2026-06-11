# cc_harness/prompts.py
"""System prompt composition for cc-harness.

The prompt is assembled from `Section` objects in `SECTION_POOL` by
`PromptComposer`. Each section declares a `body` (with optional
{placeholders}), a `priority` (lower renders first), and optional
`conditions` that gate inclusion.

`build_system_prompt()` is the public entry point used by the rest of
the app. Plan/Design mode rendering (#4) and runtime mode switching
(#6) compose on top of this infrastructure.
"""
from __future__ import annotations
from dataclasses import dataclass
from typing import Iterable, Literal

Mode = Literal["coding", "plan", "design"]
_VALID_MODES: tuple[str, ...] = ("coding", "plan", "design")


@dataclass(frozen=True)
class Section:
    """One section of the system prompt.

    `body` may contain {placeholders} that the composer fills from `ctx`.
    `priority` controls ordering (lower numbers render first).
    `conditions` gate inclusion. Supported syntax:
        "always"                    — always included (default)
        "mode==coding|plan|design"  — included only when current mode matches
        "has_tools"                 — included only when ctx has non-empty "tools"
    """
    name: str
    body: str
    priority: int = 50
    conditions: tuple[str, ...] = ()


SECTION_POOL: dict[str, Section] = {
    "identity": Section(
        "identity",
        "你是 cc-harness:一个跑在终端里的编程代理,通过 MCP 工具操作文件、shell 等。"
        "当前会话模式由系统注入,不要自行切换。",
        priority=10,
    ),
    "cwd": Section(
        "cwd",
        "当前工作目录: {cwd}",
        priority=11,
    ),
    "react_format": Section(
        "react_format",
        (
            "## 输出格式(每轮)\n"
            "1. **关于\"思考:\"标记**:每一轮你都会收到一个 \"思考: \" 头部(由系统加上),"
            "后面跟你的完整推理文本。你**不需要**自己输出\"思考:\"、\"行动:\"、\"观察:\"、\"结果:\"这些标记 —— "
            "系统会统一处理。直接输出你当轮的**完整**思考内容即可(可以是 1 句也可以是多段,系统不做截断)。\n"
            "2. 工具调用由系统处理,你不需要在文本中输出 JSON 格式的 Action 块;"
            "**不要在文本中输出 `Action: {{...}}` 或模拟工具调用格式**。\n"
            "3. **关于最终输出**:任务全部完成时,系统会自动在终端打印\"结果:\" 头部并把你的回答"
            "重新打一次。你不需要自己输出\"结果:\" 或 \"✅ 任务完成:\" 这种标记,直接输出答案内容即可。"
        ),
        priority=20,
        conditions=("mode==coding",),
    ),
    "thought_minimum": Section(
        "thought_minimum",
        "## 思考\n每轮都必须先思考再行动。在调任何工具之前,**至少先输出 1-2 句中文/自然语言推理**"
        "(你要做什么、为什么这么做、期望看到什么结果)。**不允许直接调工具而不输出任何思考文本**。",
        priority=21,
        conditions=("mode==coding",),
    ),
    "todo_block": Section(
        "todo_block",
        "## TODO 块\n如果任务有多步,在思考之后输出\"📝 TODO:\"列出步骤(可选,1-N 条短项),"
        "完成后划掉对应行(`~~1. 读 foo.py~~`)。",
        priority=22,
        conditions=("mode==coding",),
    ),
    "tool_discipline": Section(
        "tool_discipline",
        (
            "## 工具使用纪律\n"
            "1. 如果不需要工具就能回答用户问题,直接回答,不要硬塞工具调用。\n"
            "2. 如果工具执行失败,根据错误信息调整参数或换工具,**不要重复同样的失败调用**。\n"
            "3. **工具能力诚实**: 看清楚当前可用的工具列表(由系统注入)。"
            "如果没有任何工具能完成用户的任务,**第一轮就直接告诉用户**\"当前没有合适的工具可以完成这个任务\","
            "并说明需要什么类型的工具(例如 shell、http fetch 等)。"
            "**不要用无关的工具去乱试**(比如没有 shell 工具就不要 list_directory / read_file 来\"猜\"用户的意图),"
            "**不要建议用户去手动执行 shell 命令**,**不要编造看似合理的答案**(包括编造\"调用了几次\"的数字)。"
            "没有工具能做就是不能做,如实说。"
        ),
        priority=23,
        conditions=("mode==coding",),
    ),
    "dangerous_ops": Section(
        "dangerous_ops",
        "## 危险操作\n危险操作(rm -rf、删库、format 等)即使工具允许,也请先在思考中向用户说明并请求确认。"
        "不要试图通过参数变形(加引号、换空格)绕过危险检测。",
        priority=24,
        conditions=("mode==coding",),
    ),
    "honesty": Section(
        "honesty",
        (
            "## 诚实与简洁\n"
            "1. 不要编造文件内容,没读过就说没读过。\n"
            "2. 简洁优先,不要写无谓的客套话。\n"
            "3. 如果一个任务需要超过 10 步工具调用,请在思考中向用户说明进度。"
        ),
        priority=25,
    ),
    "plan_mode_override": Section(
        "plan_mode_override",
        (
            "## 模式覆盖:Plan\n"
            "你现在处于 **Plan 模式**。\n"
            "- **禁止调用任何工具** — 不读文件、不跑 shell、不搜网,直接基于已有信息输出方案。\n"
            "- 用 \"## 目标 / ## 步骤 / ## 风险 / ## 回滚 / ## 备选方案\" 五个标题分块。\n"
            "- 如果信息不足,在方案前先列 \"## 需要进一步了解\"。\n"
            "- 不需要 TODO 块、不需要工具纪律、不需要诚实提示(因为不调工具)。"
        ),
        priority=100,
        conditions=("mode==plan",),
    ),
    "design_mode_override": Section(
        "design_mode_override",
        (
            "## 模式覆盖:Design\n"
            "你现在处于 **Design 模式**。\n"
            "- **禁止调用任何工具** — 直接输出可视化产物。\n"
            "- 首选 mermaid(流程/架构/时序)、HTML 片段(布局/UI 草图)、SVG(简单图)、"
            "或对齐的 ASCII 表;不要写成纯散文。\n"
            "- 对同一概念给 2-3 个变体,用 `### 变体 A:` `### 变体 B:` 区分,每变体后一句话说明适用场景。\n"
            "- 产物末尾加 `**Tweaks**` 块,列出可调参数(配色/字体/粒度/是否含子模块)。\n"
            "- 输出前自检一遍语法、列对齐、变体差异,有问题在产物前加 `> ⚠ 自检: <问题>`。"
        ),
        priority=100,
        conditions=("mode==design",),
    ),
}


class PromptComposer:
    """Assemble a system prompt from SECTION_POOL + extra sections."""

    def __init__(
        self,
        mode: Mode = "coding",
        ctx: dict | None = None,
        extra: Iterable[Section] | None = None,
    ) -> None:
        if mode not in _VALID_MODES:
            raise ValueError(
                f"unknown mode: {mode!r} (expected one of {_VALID_MODES})"
            )
        self.mode = mode
        self.ctx = dict(ctx or {})
        self.extra: list[Section] = list(extra or [])

    def render(self) -> str:
        active = [s for s in self._all_sections() if self._matches(s)]
        active.sort(key=lambda s: s.priority)
        return "\n\n".join(s.body.format(**self.ctx) for s in active)

    def _all_sections(self) -> list[Section]:
        return [*SECTION_POOL.values(), *self.extra]

    def _matches(self, s: Section) -> bool:
        for cond in s.conditions:
            if cond == "always":
                continue
            if cond.startswith("mode=="):
                if self.mode != cond.split("==", 1)[1]:
                    return False
            elif cond == "has_tools":
                if not self.ctx.get("tools"):
                    return False
            else:
                raise ValueError(
                    f"section {s.name!r}: unknown condition {cond!r}"
                )
        return True


def build_system_prompt(cwd: str, mode: str = "coding") -> str:
    """Public entry point. Renders the system prompt for the given mode
    with `cwd` substituted. mode is one of 'coding', 'plan', 'design'."""
    return PromptComposer(mode=mode, ctx={"cwd": cwd}).render()
