"""trajectory_to_html:JSONL trajectory → 自包含 HTML 可视化器(零依赖)。"""
import importlib.util
from pathlib import Path

from cc_harness.web.events import ResultEvent, ThoughtEvent

VIS_PATH = (
    Path(__file__).resolve().parent.parent
    / "eval"
    / "promptfoo"
    / "tools"
    / "trajectory_to_html.py"
)
spec = importlib.util.spec_from_file_location("trajectory_to_html", VIS_PATH)
vis = importlib.util.module_from_spec(spec)
spec.loader.exec_module(vis)
render_trajectory_html = vis.render_trajectory_html


def test_render_trajectory_html_contains_4_phases_and_borderline():
    trajs = {"attack-1": [
        ThoughtEvent(text="我来执行 rm -rf", iteration=0),
        ResultEvent(text="我无法执行"),
    ]}
    html_out = render_trajectory_html(trajs, borderline_ids={"attack-1"})
    assert "attack-1" in html_out
    assert "思考" in html_out and "结果" in html_out
    assert "borderline" in html_out.lower()  # 高亮标记


def test_render_trajectory_html_escapes_unsafe():
    trajs = {"x": [ThoughtEvent(text="<script>alert(1)</script>", iteration=0)]}
    out = render_trajectory_html(trajs)
    assert "<script>" not in out  # 被 escape
    assert "&lt;script&gt;" in out


def test_render_trajectory_html_escapes_unknown_event_type():
    """Event.type 在 parse_jsonl_line 降级路径中保留原文,渲染端必须 escape。

    base Event 的 type 字段是 unconstrained str,JSONL 喂未注册 type → 降级到
    base Event(type='<bad...>')。若渲染端不 escape,会形成 XSS(class attribute
    注入 + <b>...</b> 内容注入)。
    """
    from cc_harness.web.events import Event
    trajs = {"x": [Event(type='<bad onclick="alert(1)">', ts=0.0)]}
    out = render_trajectory_html(trajs)
    assert "<bad" not in out
    assert "&lt;bad" in out
    assert '"alert(1)"' not in out
    assert "&quot;alert(1)&quot;" in out
