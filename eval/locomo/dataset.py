"""Locomo dataset parsing.

A locomo sample:
  {sample_id, conversation: {session_name: [{speaker, dia_id, text}, ...]}, qa: [{question, answer, category, evidence}]}
"""
from dataclasses import dataclass


@dataclass
class Turn:
    session: str
    speaker: str
    dia_id: str
    text: str


@dataclass
class QA:
    question: str
    answer: str
    category: str
    evidence: list[str]


@dataclass
class Sample:
    sample_id: str
    conversation: dict[str, list[dict]]
    qa: list[dict]


def parse_sample(raw: dict) -> Sample:
    return Sample(
        sample_id=raw["sample_id"],
        conversation=raw.get("conversation", {}),
        qa=raw.get("qa", []),
    )


def iter_turns(sample: Sample):
    """Yield Turn in session_name order. Skip entries missing speaker/text."""
    for session_name in sorted(sample.conversation.keys()):
        for entry in sample.conversation[session_name]:
            if not isinstance(entry, dict):
                continue
            if "speaker" not in entry or "text" not in entry:
                continue
            yield Turn(
                session=session_name,
                speaker=entry["speaker"],
                dia_id=str(entry.get("dia_id", "")),
                text=entry["text"],
            )


def iter_qa(sample: Sample):
    for q in sample.qa:
        if not isinstance(q, dict):
            continue
        yield QA(
            question=q.get("question", ""),
            answer=q.get("answer", ""),
            category=q.get("category", "unknown"),
            evidence=q.get("evidence", []) or [],
        )


def build_session_index(conversation: dict) -> dict[str, str]:
    """证据引用(D1:3 / D2:12)→ 所在 session_name。

    conversation 含 session_1_date_time / session_1 ... session_N_date_time / session_N
    及顶层 speaker_a / speaker_b(可能是 'D1' / 'D2' 抽象,也可能是 'Caroline' / 'Melanie' 真名)。

    返回 {'D1:3': 'session_5', ...},只覆盖 D1 / D2 系列 refs。

    算法:
      1. 抽取 N(有多少个 session_*_date_time)
      2. 对每个 session_X(按 X 数值排),取 conversation[f'session_{X}']
      3. 对该 session 内每条 utterance:
         - 'dia_id' 在真实 LoCoMo 里已经是 'D1:1' 这种字符串(自带前缀);
           合成/旧版可能是 int(直接是序号)。两种都兼容。
         - 直接用 dia_id → session_name
    """
    out: dict[str, str] = {}
    session_keys = sorted(
        (k for k in conversation.keys() if k.startswith("session_") and k.endswith("_date_time")),
        key=lambda k: int(k[len("session_"):-len("_date_time")]),
    )
    for sk_date in session_keys:
        n = sk_date[len("session_"):-len("_date_time")]
        sk = f"session_{n}"
        for utt in conversation.get(sk, []):
            speaker = utt["speaker"]
            dia_id = utt["dia_id"]
            if isinstance(dia_id, str) and ":" in dia_id:
                # 真实 LoCoMo: dia_id 已经是 "D1:1" / "D2:5" 这种带前缀的 ref
                key = dia_id
            else:
                # 合成/旧版: dia_id 是 int,与 speaker 拼装
                key = f"{speaker}:{dia_id}"
            out[key] = sk
    return out


def infer_event_date(sample: dict | Sample) -> str | None:
    """从 locomo sample 抽锚定日期(ISO 字符串,前 10 字符 YYYY-MM-DD)。

    LoCoMo 对话里的时间表述全是相对值("yesterday" / "last week"),但评测
    ground truth (type=2) 给的是绝对日期。把 conversation 第一个 session 的
    session_N_date_time 作为锚定基准(按 N 数字升序,非字符串),让 runner
    把这块塞进 QA prompt,模型就能把相对时间映射回绝对日期。

    真实 locomo 数据 session_1_date_time 形如 '1:56 pm on 8 May, 2023',
    也有 '2023-05-08 18:00:00' / '2023-05-08T18:00:00' 之类。返回 ISO
    'YYYY-MM-DD',不可解析时返 None。

    Args:
        sample: 原始 dict(有 'conversation' key)或 Sample dataclass。

    Returns:
        ISO 字符串如 '2023-05-08',若缺/异常返 None。
    """
    if isinstance(sample, Sample):
        conv = sample.conversation
    elif isinstance(sample, dict):
        conv = sample.get("conversation", {})
    else:
        return None
    if not conv:
        return None
    # 按 session 数字升序(非字符串)找第一个 date_time 键
    candidates = []
    for k in conv.keys():
        if k.startswith("session_") and k.endswith("_date_time"):
            try:
                n = int(k[len("session_"):-len("_date_time")])
            except ValueError:
                continue
            candidates.append((n, k))
    if not candidates:
        return None
    candidates.sort()
    sk_date = candidates[0][1]
    raw = conv.get(sk_date)
    if not raw or not isinstance(raw, str):
        return None
    # 尝试解析多种格式
    from datetime import datetime
    raw = raw.strip()
    fmts = (
        "%Y-%m-%d %H:%M:%S",
        "%Y-%m-%dT%H:%M:%S",
        "%Y-%m-%d",
        "%I:%M %p on %d %B, %Y",        # 1:56 pm on 8 May, 2023
        "%H:%M on %d %B, %Y",             # 13:56 on 8 May, 2023(未实测, 兜底)
    )
    for fmt in fmts:
        try:
            dt = datetime.strptime(raw, fmt)
            return dt.strftime("%Y-%m-%d")
        except ValueError:
            continue
    return None
