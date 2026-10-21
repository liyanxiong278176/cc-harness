"""Audit paired coding trials and report quality/cost with task-cluster intervals."""

from __future__ import annotations

import argparse
import hashlib
import json
from collections import Counter
from pathlib import Path

import numpy as np


def read_rows(path):
    return (
        [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]
        if path.exists()
        else []
    )


def metrics(observations, calls):
    ids = {row["trial_id"] for row in observations}
    matching = [row for row in calls if row["trial_id"] in ids]
    successful = [row for row in matching if row.get("ok")]
    return {
        "trials": len(observations),
        "passed": sum(bool(row["ok"]) for row in observations),
        "pass_rate": sum(bool(row["ok"]) for row in observations) / max(1, len(observations)),
        "provider_attempts": len(matching),
        "provider_errors": len(matching) - len(successful),
        "summary_calls": sum(not row.get("tool_schema_present") for row in successful),
        "input_tokens": sum(row["usage"]["prompt_tokens"] for row in successful),
        "total_tokens": sum(row["usage"]["total_tokens"] for row in successful),
        "tiers": dict(
            Counter(tier for row in observations for tier in row.get("compaction_tiers", []))
        ),
    }


def compare(observations, calls):
    arms = {
        a: metrics([r for r in observations if r["condition"] == a], calls)
        for a in ("context_off", "context_on")
    }
    cases = sorted({r["case"] for r in observations})
    differences = []
    for case in cases:
        rs = [r for r in observations if r["case"] == case]
        if any(sum(r["condition"] == a for r in rs) != 3 for a in arms):
            continue
        differences.append(
            sum(r["ok"] for r in rs if r["condition"] == "context_on") / 3
            - sum(r["ok"] for r in rs if r["condition"] == "context_off") / 3
        )
    interval = None
    if len(differences) == len(cases) and cases:
        rng = np.random.default_rng(20261011)
        means = rng.choice(differences, size=(5000, len(cases)), replace=True).mean(axis=1)
        interval = [float(v) for v in np.quantile(means, [0.025, 0.975])]
    off, on = arms["context_off"], arms["context_on"]
    change = on["pass_rate"] - off["pass_rate"]
    saving = 1 - on["total_tokens"] / off["total_tokens"] if off["total_tokens"] else None
    return {
        "arms": arms,
        "task_count": len(cases),
        "pass_rate_difference": change,
        "task_cluster_bootstrap_95_interval": interval,
        "total_token_reduction": saving,
        "observed_target_met": bool(saving is not None and saving > 0 and change >= -0.03),
        "noninferiority_supported_by_interval": bool(interval and interval[0] >= -0.03),
    }


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("output", type=Path)
    parser.add_argument("--report", type=Path)
    args = parser.parse_args()
    out = args.output
    data = json.loads((out / "coding-dataset.json").read_text(encoding="utf-8"))
    replacement = out / "coding-replacements.json"
    replacements = (
        json.loads(replacement.read_text(encoding="utf-8"))
        if replacement.exists()
        else {"tasks": [], "excluded_cases": []}
    )
    tasks = [
        r for r in data["tasks"] if r["id"] not in replacements["excluded_cases"]
    ] + replacements["tasks"]
    expected = {
        f"{t['id']}-{a}-{i}" for t in tasks for a in ("context_off", "context_on") for i in range(3)
    }
    obs = read_rows(out / "coding-observations.jsonl")
    calls = read_rows(out / "coding-provider-calls.jsonl")
    errors = []
    counts = Counter(r["trial_id"] for r in obs)
    if set(counts) != expected or any(n != 1 for n in counts.values()):
        errors.append("Trial IDs do not exactly match the frozen plan once each")
    call_counts = Counter(row["call_id"] for row in calls)
    if any(count != 1 for count in call_counts.values()):
        errors.append("Duplicate provider call IDs")
    if any(row["trial_id"] not in expected for row in calls):
        errors.append("Provider calls outside frozen trial plan")
    successful_ids = {row["trial_id"] for row in calls if row.get("ok")}
    if any(not row.get("ok") for row in calls):
        errors.append("Provider errors occurred; inspect infrastructure failures before interpreting quality")
    enabled = metrics([r for r in obs if r["condition"] == "context_on"], calls)
    if not all(enabled["tiers"].get(tier, 0) for tier in ("snip", "prune", "summarize")):
        errors.append("All three compression tiers were not observed")
    if not enabled["summary_calls"]:
        errors.append("No actual auxiliary summary model call was observed")
    if expected - successful_ids:
        errors.append("Some planned trials have no successful provider call")
    root = Path(__file__).resolve().parents[1]
    manifest = json.loads((out / "manifest.json").read_text(encoding="utf-8"))
    relevant = {
        "context.py",
        "context_evidence.py",
        "config.py",
        "capability_runtime.py",
        "durable_runtime.py",
        "worker.py",
        "tokens.py",
    }
    for name, old_digest in manifest["source_hashes"].items():
        path = root / name
        if path.name in relevant and hashlib.sha256(path.read_bytes()).hexdigest() != old_digest:
            errors.append(f"Production source changed during evaluation: {name}")
    for call in calls:
        if not call.get("ok"):
            continue
        trace = json.loads((out / "calls" / f"{call['call_id']}.json").read_text(encoding="utf-8"))
        digest = hashlib.sha256(
            json.dumps(trace["request"], sort_keys=True, ensure_ascii=False).encode()
        ).hexdigest()
        if digest != call["request_sha256"] or trace["record"] != call:
            errors.append(f"Trace mismatch: {call['call_id']}")
    result = {
        "scope": "Internal constructed regression fixtures; not production or new independent tasks",
        "acceptance": "Total provider tokens including summary decrease; observed completion-rate decrease <= 3 percentage points. Interval reported separately.",
        "audit_errors": errors,
        "complete": not errors,
        "all_tasks": compare(obs, calls),
    }
    result["by_pressure_tier"] = {
        str(tier): compare([r for r in obs if int(r["case"].split("-")[1]) % 3 == tier], calls)
        for tier in range(3)
    }
    plan_path = out / "evaluation-plan.json"
    if plan_path.exists():
        plan = json.loads(plan_path.read_text(encoding="utf-8"))
        calibration = set(plan["calibration_cases"])
        result["not_used_in_this_calibration"] = compare(
            [row for row in obs if row["case"] not in calibration], calls
        )
    (out / "quality-analysis.json").write_text(
        json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    print(json.dumps(result, ensure_ascii=False, indent=2))
    if args.report:
        total = result["all_tasks"]
        off, on = total["arms"]["context_off"], total["arms"]["context_on"]
        interval = total["task_cluster_bootstrap_95_interval"]
        text = "# 三级上下文压缩修复与对照实测\n\n"
        text += "所有数据来自内部构造编程场景。原失败记录保留；新旧结果不混用。\n\n"
        text += "实现：在工具原位置保留有界、带来源的近期文件预览；摘要重建后保留原始调用 ID 对应的 assistant/tool 配对，跨保护边界的结果不再成为孤立消息；写入后撤销旧文件预览，保留最近命令结果。源事件及原文不变，证据不获得系统指令或权限。\n\n"
        text += "| 条件 | 通过任务次数 | 通过率 | 总 Token（包含摘要） | 摘要调用 |\n| --- | ---: | ---: | ---: | ---: |\n"
        for label, arm in [("关闭压缩", off), ("开启压缩", on)]:
            text += f"| {label} | {arm['passed']}/{arm['trials']} | {arm['pass_rate']:.1%} | {arm['total_tokens']:,} | {arm['summary_calls']} |\n"
        text += f"\n总 Token 变化：减少 {total['total_token_reduction']:.1%}；通过率差：{total['pass_rate_difference'] * 100:+.2f} 个百分点。\n\n"
        if interval:
            text += f"按任务聚合三次重复的 95% Bootstrap 区间：[{interval[0] * 100:.2f}, {interval[1] * 100:.2f}] 个百分点。重复执行不是独立任务。\n\n"
        text += f"预先采用的观察值目标（降幅不超过 3 个百分点）：{total['observed_target_met']}。区间是否支持该非劣界限：{total['noninferiority_supported_by_interval']}。证据完整性：{not errors}。\n\n"
        text += "本轮沿用此前冻结的 100 个双模块任务、三次重复、每条件最多 12 次模型调用、3 个调度段及原压缩压力阈值；这是一组已见过的回归任务，不能视作真实仓库或新独立留出集。先前以 15 题校准，正式结果不得挑选成功任务计算。报告仅在完整试验后解释。\n\n"
        text += f"完整分档结果、轨迹校验和原始记录：`{out.resolve().as_posix()}`。\n"
        args.report.parent.mkdir(parents=True, exist_ok=True)
        args.report.write_text(text, encoding="utf-8")


if __name__ == "__main__":
    main()
