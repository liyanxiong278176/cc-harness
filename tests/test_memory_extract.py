"""Phase 3 Q1 uplift: extract_dates/entities/keywords 单测。"""

from cc_harness.memory.extract import (
    extract_dates,
    extract_entities,
    extract_keywords,
)
from cc_harness.memory.capture import _US


# --- extract_dates ---

def test_extract_dates_iso():
    """ISO 数字日期。"""
    assert "2024-01-15" in extract_dates("event on 2024-01-15")
    assert "2024/1/5" in extract_dates("event on 2024/1/5")
    assert "7-5-2023" in extract_dates("on 7-5-2023")


def test_extract_dates_named():
    """月份名 + 日。"""
    out = extract_dates("on 7 May 2023")
    assert "7 May 2023" in out


def test_extract_dates_relative():
    """相对日期(英文)。"""
    out = extract_dates("she went yesterday and will go next Monday")
    assert "yesterday" in out
    assert "next Monday" in out or "last Monday" in out or "Monday" in out


def test_extract_dates_n_days_ago():
    """'N days ago' 形式。"""
    out = extract_dates("3 days ago I saw her")
    assert any("3 days ago" in d for d in out)


def test_extract_dates_empty():
    """非 str / 空 str 返 []。"""
    assert extract_dates("") == []
    assert extract_dates(None) == []
    assert extract_dates(123) == []


# --- extract_entities ---

def test_extract_entities_capitalized():
    """大写英文词序列。"""
    out = extract_entities("Caroline and Melanie went to Blue Horizons")
    assert "Caroline" in out
    assert "Melanie" in out
    assert "Blue Horizons" in out


def test_extract_entities_acronym():
    """全大写缩写。"""
    out = extract_entities("LGBTQ support and USA trip")
    assert "LGBTQ" in out
    assert "USA" in out


def test_extract_entities_chinese():
    """中文 2-4 字(粗启发式:无 jieba,匹配 2-4 连续中文字符 run,
    可能跨词,需 caller 二次清洗)。"""
    out = extract_entities("Caroline 去参加张伟的派对")
    # 中文实词可能被跨词匹配(例: '去参加张'),所以只断言:有 2-4 字中文 run 被抓
    has_cn = any(
        len(w) >= 2 and len(w) <= 4 and all('一' <= c <= '鿿' for c in w)
        for w in out
    )
    assert has_cn, f"expected at least one 2-4 char Chinese entity, got {out!r}"
    # Caroline 仍是 capitalized word
    assert "Caroline" in out


def test_extract_entities_empty():
    """非 str / 空 str 返 []。"""
    assert extract_entities("") == []
    assert extract_entities(None) == []


# --- extract_keywords ---

def test_extract_keywords_filters_stopwords():
    """常见 stopword 必须被滤掉。"""
    out = extract_keywords("When did Caroline go the LGBTQ support group?", n=5)
    # 必含实词
    assert "caroline" in out
    assert "lgbtq" in out
    # 必滤 stopword
    assert "the" not in out
    assert "when" not in out
    assert "did" not in out


def test_extract_keywords_top_n():
    """返回 top-n,按频率降序。"""
    out = extract_keywords("caroline caroline caroline lgbtq support", n=3)
    assert len(out) == 3
    assert out[0] == "caroline"  # 频率最高


def test_extract_keywords_chinese_2gram():
    """中文按 2-gram 切。"""
    out = extract_keywords("蓝天地平线展览活动", n=5)
    # 应含 2-gram
    assert any(len(w) == 2 and all('一' <= c <= '鿿' for c in w) for w in out)


def test_extract_keywords_n_zero():
    """n<=0 返 []。"""
    assert extract_keywords("test", n=0) == []
    assert extract_keywords("test", n=-1) == []


def test_extract_keywords_empty():
    assert extract_keywords("") == []
    assert extract_keywords(None) == []


# --- US separator 验证 ---

def test_capture_uses_us_separator():
    """_US = '\\x1f' 不出现在普通文本,适合做 list→str。"""
    assert _US == "\x1f"
    # 普通文本不会含
    assert _US not in "normal english text"
    assert _US not in "正常中文文本"