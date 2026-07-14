"""L0 capture 时的轻量信息抽取(Phase 3 Q1 uplift)。

为 conversation 表的每行附加结构化字段(dates / entities / keywords),
后续 recall 可用这些字段做精确过滤 / 排序 / 补充召回。

原则:
- 纯正则,无外部依赖(不引 spacy / jieba 等)
- 容错优先:能抽就抽,不能抽就空 list
- 不做语义判断(例如不区分 "7 May 2023" 是"事件日期"还是"提及日期")
"""
from __future__ import annotations
import re
from collections import Counter


# --- 日期 ---

# ISO / 数字日期: 2024-01-15, 2024/1/5, 7/5/2023, 7-5-23
_RE_DATE_NUMERIC = re.compile(
    r"\b\d{4}[-/]\d{1,2}[-/]\d{1,2}\b"
    r"|\b\d{1,2}[-/]\d{1,2}[/-]\d{2,4}\b"
)

# 月份名 + 日: "7 May 2023", "May 7", "January 15th 2024"
# 注意: 不能在 | 前后加空格,会被 regex 当 part of alternation(match ' May' 带前导空格)
_MONTHS = (
    "January|February|March|April|May|June|July|August|"
    "September|October|November|December|"
    "Jan|Feb|Mar|Apr|Jun|Jul|Aug|Sep|Sept|Oct|Nov|Dec"
)
_RE_DATE_NAMED = re.compile(
    rf"\b(?:\d{{1,2}}\s+)?(?:{_MONTHS})(?:\s+\d{{1,2}})?"
    rf"(?:\s*,?\s*\d{{4}})?\b",
    flags=re.IGNORECASE,
)

# 相对日期: "last May", "yesterday", "last week", "5 days ago", "two weeks ago"
_RE_DATE_RELATIVE = re.compile(
    r"\b(?:yesterday|today|tomorrow|tonight|"
    r"(?:last|next|this)\s+(?:week|month|year|"
    r"Monday|Tuesday|Wednesday|Thursday|Friday|Saturday|Sunday|"
    r"spring|summer|fall|autumn|winter))\b",
    flags=re.IGNORECASE,
)
_RE_DATE_N_DAYS_AGO = re.compile(
    r"\b\d+\s+(?:days?|weeks?|months?|years?)\s+ago\b",
    flags=re.IGNORECASE,
)


def extract_dates(text: str) -> list[str]:
    """从 text 抽日期/时间表达。

    返回 list[str],不保证 unique(同 expression 多次出现返多次)。
    失败(非 str / 空)返 []。
    """
    if not isinstance(text, str) or not text:
        return []
    found: list[str] = []
    for pat in (_RE_DATE_NUMERIC, _RE_DATE_NAMED, _RE_DATE_RELATIVE, _RE_DATE_N_DAYS_AGO):
        found.extend(m.group(0) for m in pat.finditer(text))
    return found


# --- 实体(人名/地名/缩写) ---

# 大写英文词序列:Melanie Smith / Caroline / Blue Horizons
_RE_ENT_CAPITALIZED = re.compile(
    r"\b[A-Z][a-z]+(?:\s+[A-Z][a-z]+){0,3}\b"
)
# 全大写缩写(2+ 字母):LGBTQ, USA, AI
_RE_ENT_ACRONYM = re.compile(r"\b[A-Z]{2,}\b")
# 中文姓名/地名 2-4 字(粗略 — 没法不引 jieba 做到精确)
_RE_ENT_CN = re.compile(r"[一-鿿]{2,4}")


def extract_entities(text: str) -> list[str]:
    """抽人名/地名/缩写。

    返回 list[str],不保证 unique。失败返 []。
    注意: 中文 2-4 字是粗启发式,可能误判,后续可加 NER 替换。
    """
    if not isinstance(text, str) or not text:
        return []
    found: list[str] = []
    found.extend(m.group(0) for m in _RE_ENT_CAPITALIZED.finditer(text))
    found.extend(m.group(0) for m in _RE_ENT_ACRONYM.finditer(text))
    found.extend(m.group(0) for m in _RE_ENT_CN.finditer(text))
    return found


# --- 关键词 ---

# 简单英文 stopword 集(避免无穷列表,只覆盖最常见的)
_EN_STOPWORDS = frozenset("""
    a an the and or but if then else is are was were be been being am
    have has had do does did doing done will would should could may
    might can could shall must
    i you he she it we they me him her us them my your his hers its
    our their mine yours hers his theirs
    this that these those there here
    what when where who whom whose why how which
    not no nor so too very just also only even still yet
    in on at by for from to of with about as into through during
    before after above below between out off over under up down
    s t d re ve ll m o y don doesn didn isn aren wasn hasn haven
    am is are was were be been being
""".split())


def _tokenize_en(text: str) -> list[str]:
    """英文按非字母数字切 + lower + 去 stopword + 长度>=3。"""
    tokens = re.findall(r"[A-Za-z][A-Za-z0-9]{2,}", text.lower())
    return [t for t in tokens if t not in _EN_STOPWORDS]


def _tokenize_zh(text: str) -> list[str]:
    """中文按 2-gram 切(简单字符 n-gram,够 smoke 用)。"""
    text = re.sub(r"[^一-鿿]+", " ", text)
    text = text.strip()
    if len(text) < 2:
        return []
    # 2-gram + 单字(>=2 字)
    grams: list[str] = []
    for i in range(len(text) - 1):
        grams.append(text[i:i+2])
    return grams


def extract_keywords(text: str, n: int = 5) -> list[str]:
    """抽 top-n 关键词(英文 token + 中文 2-gram 频率)。

    返回 list[str],长度 <= n,按频率降序。失败返 []。
    """
    if not isinstance(text, str) or not text or n <= 0:
        return []
    tokens = _tokenize_en(text) + _tokenize_zh(text)
    if not tokens:
        return []
    counts = Counter(tokens)
    return [w for w, _ in counts.most_common(n)]