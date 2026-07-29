"""Gold-set 校准工具:Cohen's κ + 失败驱动收集 + 回归语义(Task 10 of eval-v2)。

公开 API:
- cohen_kappa(judge, gold) -> float
    两标注序列的 Cohen's κ(类别任意)。空 / 长度不匹配 → 0.0;pe==1 → 1.0。
- collect_failures(results_json, calibration_yaml) -> int
    eval results JSON(score=0 + critical/high + embedding 去重)→ 追加 pending 区。
    返新增数。embed 失败 fail-open(等同不视作重复,候选全收)。
- regression_run(gold_results_json, baseline_yaml) -> dict
    上一次 baseline 的 hold 集合 vs 本次 gold-set 重跑结果 →
    {new_breaks: [...], fixed: [...]}。new_breaks 是回归(之前 hold 现在 break)。

去重用 `_dedup(prompt, existing) -> float` 包装直接调 `curate_attacks.embed`,
避免 `compute_similarities` 的 AttackCandidate 签名耦合。
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

import numpy as np
import yaml

# tools dir 不在 sys.path,显式插入让 calibration_schema/curate_attacks 可导入
sys.path.insert(0, str(Path(__file__).resolve().parent))
from calibration_schema import load_calibration_set  # noqa: E402


DEDUP_THRESHOLD = 0.85  # match curate_attacks.DEFAULT_MAX_SIM


def cohen_kappa(judge: list, gold: list) -> float:
    """两标注序列的 Cohen's κ(类别可任意)。len 不同 / 空 → 0。"""
    n = min(len(judge), len(gold))
    if n == 0:
        return 0.0
    labels = list(set(judge[:n]) | set(gold[:n]))
    po = sum(j == g for j, g in zip(judge[:n], gold[:n])) / n
    pe = sum(
        (sum(1 for j in judge[:n] if j == label) / n)
        * (sum(1 for g in gold[:n] if g == label) / n)
        for label in labels
    )
    return 1.0 if pe == 1 else (po - pe) / (1 - pe)


def _dedup(prompt: str, existing_prompts: list[str]) -> float:
    """Embed prompt + existing,返 max cosine sim vs existing。

    - 空 existing → 0.0(等同不重复)
    - embed 失败 / 异常 → 0.0(fail-open,等同不视作重复)
    - malformed 2-D 响应 → 0.0(防御性)
    """
    if not existing_prompts:
        return 0.0
    try:
        # Lazy import: 不污染顶层依赖;embed 需要 EMBEDDING_BASE_URL/API_KEY/MODEL
        from curate_attacks import embed
        embs = embed([prompt] + list(existing_prompts))
        if embs.ndim != 2 or embs.shape[0] != len(existing_prompts) + 1:
            return 0.0  # malformed API response
        v = embs[0]
        sims = [
            float(np.dot(v, e) / (np.linalg.norm(v) * np.linalg.norm(e)))
            for e in embs[1:]
        ]
        return max(sims) if sims else 0.0
    except Exception:
        return 0.0  # fail-open


def collect_failures(
    results_json: Path,
    calibration_yaml: Path,
    severity_filter=("critical", "high"),
) -> int:
    """eval results → score=0 + severity∈filter + dedup → 追加 pending 区。返新增数。

    候选 6 维 schema(id/prompt/severity/hold_broke/borderline/leak_type)与
    calibration_schema.validate_entry 一致;外加 metadata(category/source)。
    """
    cs = load_calibration_set(calibration_yaml)
    existing = {e.get("prompt") for e in cs["baseline"] + cs["pending"]}
    static_texts = [e["prompt"] for e in cs["baseline"] + cs["pending"]]

    data = json.loads(Path(results_json).read_text(encoding="utf-8"))
    raw = (data.get("results") or {}).get("results") or []

    candidates: list[dict] = []
    for r in raw:
        tc = r.get("testCase") or {}
        md = tc.get("metadata") or {}
        sev = md.get("severity", "medium")
        if r.get("score", 1.0) != 0.0 or sev not in severity_filter:
            continue
        prompt = (tc.get("vars") or {}).get("prompt", "")
        if not prompt or prompt in existing:
            continue
        candidates.append({
            "id": f"fail-{len(cs['pending']) + len(candidates)}",
            "prompt": prompt,
            "severity": sev,
            "hold_broke": 0,
            "borderline": False,
            "leak_type": "none",
            "category": md.get("category", "?"),
            "source": "failure-driven",
        })

    if not candidates:
        return 0

    # Embedding 去重:每个 candidate vs static_texts;fail-open embed 挂 → 全收
    kept = [
        c for c in candidates
        if _dedup(c["prompt"], static_texts) < DEDUP_THRESHOLD
    ]

    cs["pending"].extend(kept)
    Path(calibration_yaml).write_text(
        yaml.dump(cs, allow_unicode=True, sort_keys=False),
        encoding="utf-8",
    )
    return len(kept)


def regression_run(gold_results_json: Path, baseline_yaml: Path) -> dict:
    """读上一次 baseline 的 hold 集合,对比本次 → {new_breaks, fixed}。

    - new_breaks: 之前 hold 但本次 break(回归)
    - fixed: 之前不在 hold 集合、本次 hold(修复,bonus)

    baseline_yaml 走 load_calibration_set,从 baseline 区抽 hold_broke==1 的 id;
    gold_results_json 的 result 用 testId(id 兜底)关联。
    """
    cs = load_calibration_set(baseline_yaml)
    baseline_hold_ids = {
        e["id"] for e in cs["baseline"] if e.get("hold_broke") == 1
    }

    if not baseline_hold_ids:
        return {"new_breaks": [], "fixed": []}

    data = json.loads(Path(gold_results_json).read_text(encoding="utf-8"))
    raw = (data.get("results") or {}).get("results") or []

    # Map: testId -> success (True = held, False = broke)
    result_set: dict[str, bool] = {}
    for r in raw:
        tid = r.get("testId") or r.get("id")
        if tid is None:
            continue
        result_set[str(tid)] = bool(r.get("success"))

    new_breaks = sorted(
        tid for tid in baseline_hold_ids
        if tid in result_set and result_set[tid] is False
    )
    fixed = sorted(
        tid for tid, ok in result_set.items()
        if ok is True and tid not in baseline_hold_ids
    )

    return {"new_breaks": new_breaks, "fixed": fixed}