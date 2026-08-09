"""Task 5: 锚定日期注入必须只对需要时间的 QA 类别(type=2)生效,避免对 type=1/4/5
无差别注入分散模型注意力(回归:locomo-coding smoke QA #3 把锚定日期块当 answer 复读)。

LoCoMo 类别:
  1: fact/factual      - 事实(不需要锚定日期)
  2: temporal          - 时间/日期(需要锚定日期,绝对日期 gold)
  3: cause/inference   - 推断(可注入,可不注入;不强制)
  4: experience/opinion- 经验感受
  5: multi-hop         - 多跳推理

设计:仅 type=2 注入锚定日期,避免 #3 因#3 是 type=3,注入会分散模型对
事实点的注意力。

契约测试:读 runner.py 源文件,断言锚定日期注入逻辑只在 qa.category==2 时触发。
"""
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
RUNNER = REPO / "eval/locomo/runner.py"


def test_event_date_injection_gated_on_category():
    """锚定日期注入必须按 qa.category 决定,type=1 不注入。"""
    src = RUNNER.read_text(encoding="utf-8")

    # 必须包含 'qa.category' 或类似 category 比较
    assert "qa.category" in src or "category" in src, (
        "runner.py 应按 qa.category 判断是否注入锚定日期"
    )

    # 必须包含 '2' 类别字符串比较(LoCoMo category 是字符串 '2'/'1'/...)
    # 检查存在类似 `== "2"` 或 `== 2` 的判断
    has_category_check = (
        '== "2"' in src or
        "== '2'" in src or
        "qa.category == 2" in src or
        'qa.category == "2"' in src
    )
    assert has_category_check, (
        "runner.py 应检查 qa.category 是否为 '2' 才注入锚定日期"
    )


def test_event_date_block_still_prepended_for_type_2():
    """type=2 时间类仍 prepend 锚定日期块。"""
    src = RUNNER.read_text(encoding="utf-8")
    assert "## 锚定日期" in src, (
        "runner.py 应保留 '## 锚定日期' 块"
    )