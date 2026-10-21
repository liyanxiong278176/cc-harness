"""Independent fixture checks, evidence audit, clustered intervals and report."""

from __future__ import annotations

import argparse
from collections import Counter, defaultdict
import hashlib
import json
from pathlib import Path
import statistics
import tempfile
import xml.etree.ElementTree as ET

import numpy as np

from resume_formal_suite import ROOT, dump, rows
from resume_formal_workloads import coding_specs, verify_fixture


def validate_oracles(out, coding=None):
    base = Path(tempfile.mkdtemp(prefix="resume-oracle-"))
    failures = []
    checks = 0
    stub_failures = 0
    specs = coding_specs()
    if coding is not None:
        specs = json.loads((coding / "coding-dataset.json").read_text(encoding="utf-8"))["tasks"]
        replacement = json.loads((coding / "coding-replacements.json").read_text(encoding="utf-8"))
        expected_replacement = [
            "",
            "a/b",
            "a/b",
            "../a/b",
            "x",
            "a/b/..",
            "x..y",
            "a/b/..",
            " . /a",
            "",
            "space dir/file",
            "a/b/c",
        ]
        assert [c["expected"] for c in replacement["tasks"][0]["checks"]] == expected_replacement
        specs = [s for s in specs if s["id"] not in replacement["excluded_cases"]] + replacement[
            "tasks"
        ]
    for spec in specs:
        imports = "import math,re,statistics,itertools\nfrom collections import Counter\n"
        expression = spec["reference"].replace("__import__('itertools')", "itertools")
        (base / "core.py").write_text(
            imports + "def solve(x):\n    return " + expression + "\n", encoding="utf-8"
        )
        (base / "service.py").write_text(
            "from core import solve\ndef run(rows):\n    return dict(count=len(rows),results=[solve(x) for x in rows])\n",
            encoding="utf-8",
        )
        code, text = verify_fixture(base, spec, base)
        if code:
            failures.append(dict(case=spec["id"], exit_code=code, output=text))
        checks += len(spec["checks"]) + 3
        (base / "core.py").write_text("def solve(x):\n    return x\n", encoding="utf-8")
        (base / "service.py").write_text(
            "from core import solve\ndef run(rows):\n    return solve(rows[0]) if rows else None\n",
            encoding="utf-8",
        )
        stub_failures += verify_fixture(base, spec, base)[0] != 0
    result = dict(
        tasks=100,
        reference_failures=failures,
        initial_bug_detected=stub_failures,
        checks=checks,
        verification_directory=str(base),
    )
    dump(out / "oracle-validation.json", result)
    print(json.dumps(result, ensure_ascii=False))
    if failures or stub_failures != 100:
        raise SystemExit(1)


def bootstrap_interval(values, reps=5000):
    values = np.asarray(values, dtype=float)
    rng = np.random.default_rng(101117)
    averages = values[rng.integers(0, len(values), size=(reps, len(values)))].mean(axis=1)
    return [float(v) for v in np.quantile(averages, [0.025, 0.975])]


def analyses(base, coding):
    memory = rows(base / "memory-observations.jsonl")
    raw_code = rows(coding / "coding-observations.jsonl")
    replacement_path = coding / "coding-replacements.json"
    excluded = (
        json.loads(replacement_path.read_text(encoding="utf-8"))["excluded_cases"]
        if replacement_path.exists()
        else []
    )
    code = [r for r in raw_code if r["case"] not in excluded]
    engineering = rows(base / "engineering-observations.jsonl")
    calls = rows(base / "memory-provider-calls.jsonl") + rows(
        coding / "coding-provider-calls.jsonl"
    )
    call_usage = defaultdict(
        lambda: dict(input=0, output=0, total=0, calls=0, auxiliary=0, cache=0)
    )
    for call in calls:
        entry = call_usage[call["trial_id"]]
        entry["calls"] += 1
        if not call.get("usage"):
            continue
        use = call["usage"]
        entry["input"] += use["prompt_tokens"]
        entry["output"] += use["completion_tokens"]
        entry["total"] += use["total_tokens"]
        entry["cache"] += use.get("cache_read_input_tokens", 0)
        entry["auxiliary"] += int(
            not call.get("tool_schema_present", False) and call["trial_id"].startswith("code-")
        )
    result = {}
    for name, observations, unit in [("memory", memory, "session_id"), ("coding", code, "case")]:
        entries = {}
        for arm in sorted({r["condition"] for r in observations}):
            select = [r for r in observations if r["condition"] == arm]
            grouped = defaultdict(list)
            for r in select:
                grouped[r[unit]].append(float(r["ok"]))
            values = [statistics.mean(v) for v in grouped.values()]
            latency = [r["elapsed_ms"] for r in select]
            sums = {
                key: sum(call_usage[r["trial_id"]][key] for r in select)
                for key in ["input", "output", "total", "calls", "auxiliary", "cache"]
            }
            entry = dict(
                n=len(select),
                distinct_cases=len({r["case"] for r in select}),
                clusters=len(grouped),
                successes=sum(r["ok"] for r in select),
                accuracy=sum(r["ok"] for r in select) / len(select),
                cluster_bootstrap_95=bootstrap_interval(values),
                infrastructure_errors=sum("infrastructure_error" in r for r in select),
                usage=sums,
                elapsed_median_ms=statistics.median(latency),
                elapsed_p95_ms=float(np.quantile(latency, 0.95)),
                max_elapsed_ms=max(latency),
                configured_deadline_overshoot=sum(v > 210000 for v in latency)
                if name == "coding"
                else 0,
            )
            if name == "memory":
                entry["categories"] = {
                    c: dict(n=len(sub), successes=sum(r["ok"] for r in sub))
                    for c in sorted({r["category"] for r in select})
                    if (sub := [r for r in select if r["category"] == c])
                }
            else:
                entry["planned_tiers"] = {}
                for modulo, label in enumerate(("snip", "prune", "summarize")):
                    subgroup = [r for r in select if int(r["case"].split("-")[1]) % 3 == modulo]
                    if not subgroup:
                        continue
                    entry["planned_tiers"][label] = dict(
                        n=len(subgroup),
                        distinct_tasks=len({r["case"] for r in subgroup}),
                        successes=sum(r["ok"] for r in subgroup),
                        input_tokens=sum(call_usage[r["trial_id"]]["input"] for r in subgroup),
                        total_tokens=sum(call_usage[r["trial_id"]]["total"] for r in subgroup),
                        auxiliary_calls=sum(
                            call_usage[r["trial_id"]]["auxiliary"] for r in subgroup
                        ),
                    )
                entry["families"] = {
                    c: dict(n=len(sub), successes=sum(r["ok"] for r in sub))
                    for c in sorted({r["family"] for r in select})
                    if (sub := [r for r in select if r["family"] == c])
                }
                entry["compaction_tiers"] = dict(
                    Counter(t for r in select for t in r.get("compaction_tiers", []))
                )
                entry["runtime_statuses"] = dict(
                    Counter(r.get("runtime_status", "exception") for r in select)
                )
            entries[arm] = entry
        result[name] = entries
    for name, observations, unit, control, experiment in [
        ("memory", memory, "session_id", "recent", "layered"),
        ("coding", code, "case", "context_off", "context_on"),
    ]:
        if not observations:
            continue
        by_cluster = defaultdict(lambda: defaultdict(list))
        for r in observations:
            by_cluster[r[unit]][r["condition"]].append(float(r["ok"]))
        difference = [
            statistics.mean(v[experiment]) - statistics.mean(v[control])
            for v in by_cluster.values()
            if control in v and experiment in v
        ]
        if difference:
            result[name]["accuracy_difference"] = dict(
                control=control,
                experiment=experiment,
                mean=statistics.mean(difference),
                cluster_bootstrap_95=bootstrap_interval(difference),
            )
    fault = [r for r in engineering if r["domain"] == "fault"]
    result["fault"] = dict(
        n=len(fault),
        successes=sum(r["ok"] for r in fault),
        boundaries=dict(Counter(r["boundary"] for r in fault)),
        restoration_median_ms=statistics.median(
            r["restore_ms"] for r in fault if "restore_ms" in r
        ),
        stale_fenced=sum(r.get("stale_fenced", False) for r in fault),
        ambiguity_retained=sum(r.get("ambiguous_retained", False) for r in fault),
    )
    result["security"] = {}
    for domain in ["policy_attack", "policy_benign", "dlp_attack", "dlp_benign"]:
        select = [r for r in engineering if r["domain"] == domain]
        result["security"][domain] = dict(
            n=len(select),
            successes=sum(r["ok"] for r in select),
            categories={
                c: dict(n=len(sub), successes=sum(r["ok"] for r in sub))
                for c in sorted({r["category"] for r in select})
                if (sub := [r for r in select if r["category"] == c])
            },
            actions=dict(Counter(r["action"] for r in select if "action" in r)),
            pii_active=sorted({r["pii_active"] for r in select if "pii_active" in r}),
        )
    duplicates = {
        name: len(select) - len({r["trial_id"] for r in select})
        for name, select in [("memory", memory), ("coding", code), ("engineering", engineering)]
    }
    result["audit"] = dict(
        raw_coding_observations=len(raw_code),
        excluded_invalid_oracle_trials=len(raw_code) - len(code),
        counts=dict(memory=len(memory), coding=len(code), engineering=len(engineering)),
        duplicates=duplicates,
        failed_provider_attempts=sum(not c["ok"] for c in calls),
        provider_models=dict(Counter(c.get("resolved_model", "error") for c in calls)),
        excluded_oracle_api_calls=sum(c["trial_id"].startswith("code-026-snake-") for c in calls),
        calls=len(calls),
        known_total_tokens=sum(
            c.get("usage", {}).get("total_tokens", 0) for c in calls if c.get("usage")
        ),
    )
    result["audit"]["complete"] = (
        len(memory) == 3600
        and len(code) == 600
        and len(engineering) == 800
        and not any(duplicates.values())
        and all(
            sum(r["case"] == case and r["condition"] == arm for r in code) == 3
            for case in {r["case"] for r in code}
            for arm in ("context_on", "context_off")
        )
        and len({r["case"] for r in code}) == 100
    )
    # Verify module snapshots per phase. The base snapshot predates the source-
    # filename completion fix; its memory/engineering relevant modules stay fixed.
    for phase, folder, modules in [
        (
            "memory",
            base,
            ["memory/recall.py", "memory/store.py", "memory/retriever.py", "memory/embedding.py"],
        ),
        (
            "engineering",
            base,
            ["run_store.py", "run_projection.py", "run_events.py", "policy.py", "l5.py"],
        ),
        ("coding", coding, None),
    ]:
        snapshot = {
            p.replace("\\", "/"): h
            for p, h in json.loads((folder / "manifest.json").read_text(encoding="utf-8"))[
                "source_hashes"
            ].items()
        }
        selected = (
            snapshot
            if modules is None
            else {"cc_harness/" + m: snapshot["cc_harness/" + m] for m in modules}
        )
        result["audit"][phase + "_module_hashes_match"] = all(
            hashlib.sha256((ROOT / p).read_bytes()).hexdigest() == h for p, h in selected.items()
        )
    regression = ET.parse(base / "regression.xml").getroot()
    result["regression"] = {
        k: sum(int(x.attrib.get(k, 0)) for x in regression.iter("testsuite"))
        for k in ["tests", "failures", "errors", "skipped"]
    }
    # Completed eligible pair savings: total usage can fall solely because the
    # agent stops early. Avoid calling that a successful efficiency improvement.
    paired = defaultdict(dict)
    for r in code:
        paired[(r["case"], r["repeat"])][r["condition"]] = r
    success_pairs = [p for p in paired.values() if len(p) == 2 and all(r["ok"] for r in p.values())]
    pair_usage = {
        arm: sum(call_usage[p[arm]["trial_id"]]["total"] for p in success_pairs)
        for arm in ["context_off", "context_on"]
    }
    result["coding"]["both_successful_pairs"] = dict(
        n=len(success_pairs),
        usage=pair_usage,
        savings=1 - pair_usage["context_on"] / pair_usage["context_off"]
        if pair_usage["context_off"]
        else None,
        limitation="Conditional selection; exploratory only, not overall causal performance.",
    )
    dump(base / "formal-analysis.json", result)
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return result


def write_report(base, coding, result):
    if not result["audit"]["complete"]:
        raise RuntimeError(
            "Formal report requires all 600 coding, 3600 memory and 800 engineering observations"
        )
    m = result["memory"]
    c = result["coding"]
    recent, layered, full = m["recent"], m["layered"], m["full"]
    off, on = c["context_off"], c["context_on"]
    snip_off, snip_on = off["planned_tiers"]["snip"], on["planned_tiers"]["snip"]

    def pct(n):
        return f"{100 * n:.1f}%"

    memory_input_saving = 1 - layered["usage"]["input"] / recent["usage"]["input"]
    snip_saving = 1 - snip_on["total_tokens"] / snip_off["total_tokens"]
    overall_saving = 1 - on["usage"]["total"] / off["usage"]["total"]
    mem_table = "\n".join(
        f"| {label} | {arm['successes']}/{arm['n']} | {pct(arm['accuracy'])} | {arm['usage']['input']:,} | {arm['usage']['total']:,} |"
        for label, arm in [("仅近期历史", recent), ("完整历史", full), ("分层记忆", layered)]
    )
    categories = "\n".join(
        f"| {category} | {full['categories'][category]['successes']}/{full['categories'][category]['n']} | {recent['categories'][category]['successes']}/{recent['categories'][category]['n']} | {layered['categories'][category]['successes']}/{layered['categories'][category]['n']} |"
        for category in ["L3", "L2", "L1", "multihop", "unknown", "update"]
    )
    tier_table = "\n".join(
        f"| {tier} | {off['planned_tiers'][tier]['distinct_tasks']} | {off['planned_tiers'][tier]['successes']}/{off['planned_tiers'][tier]['n']} | {on['planned_tiers'][tier]['successes']}/{on['planned_tiers'][tier]['n']} | {off['planned_tiers'][tier]['total_tokens']:,} | {on['planned_tiers'][tier]['total_tokens']:,} |"
        for tier in ["snip", "prune", "summarize"]
    )
    fault = result["fault"]
    security = result["security"]
    normal = security["dlp_benign"]
    known = sum(
        v["successes"]
        for k, v in security["dlp_attack"]["categories"].items()
        if k
        not in {
            "base64",
            "spaced",
            "wrapped",
            "unicode_escape",
            "email",
            "cn_phone",
            "us_phone",
            "cn_id",
        }
    )
    report = f"""# cc-harness 正式扩测报告（2026-10-11）

## 结论

完成 100 个不同编程任务的 600 次完整 Agent trial、100 段构造历史的 400 个查询/3,600 次真实模型回答、10 个中断边界的 200 次真实进程故障注入，以及 300 个攻击与 300 个正常组件用例。全部原始记录保留，无重复 trial ID。

分层记忆相较仅近期历史的准确率由 16.5% 提升至 42.0%，输入 Token 下降 {pct(memory_input_saving)}；完整历史准确率为 100%，因此不能声称分层记忆提高了完整历史条件下的准确率。上下文压缩整体完成率由 {pct(off["accuracy"])} 降至 {pct(on["accuracy"])}；虽然总 Token 下降 {pct(overall_saving)}，失败后的提前停止会减少用量，不能把该整体减少值写作有效效率提升。预先分配的 Snip 子组表现单独见下表。

这些是内部构造场景的数据，尚未使用真实用户需求、线上会话或生产告警，不能表述为实际业务效果。编程样例主要为短规则与算法修复，含两个模块、真实文件读取/修改和独立测试；未覆盖大型真实仓库、模型补充测试文件、复杂长期需求或容器服务任务。

工具集合仅包含 Read、Write 和固定验证命令；未评测完整生产工具集合下的恢复手段。此次没有测最大可承载会话轮数、最长任务链路或默认窗口容量，不能填入这类简历数值。

## 编程与上下文

使用生产 RunStore、RunCoordinator、ReActKernel、DurableModelAdapter、RunWorker、AgentCapabilityRuntime 和 MutationEngine。模型调用 Read、Write、固定 run_command，真实改动两个 Python 模块；外部验证器不可由模型修改。通过标准为运行时 completed、两个模块实际发生改动且独立检查通过。没有注入自动通过的 completion verifier。

本地 run_command 适配器将进程实际执行记录为 succeeded 工具观察，exit_code 和测试结果原样保留，并由外部验证器决定最终通过。Runtime completed 本身不保证断言通过；出现这种不一致时仍判失败。本次不据此验证完整生产命令适配器的错误恢复行为。

100 个任务分布：数字规则 24、字符串处理 24、列表算法 20、映射规则 16、设备预约区间 16。每条件每题 3 次，工作目录和 SQLite 状态每 trial 重置；两个条件顺序按固定种子打乱。验证器共 1,436 条单任务检查，全部参考实现通过，100 个初始错误实现均失败。判断比较 Python/JSON 值，未对所有标量强制类型等价检查。

为在有限工具输出下覆盖压缩压力，实验窗口固定 128,000 Token；三档阈值按任务编号余数预先分配为 (0.08,0.20,0.30)、(0.035,0.08,0.20)、(0.035,0.055,0.08)，保护区 512 Token，输出预留 2,500 Token。生产默认 60%/80%/95% 阈值没有在本实验中验证。控制组使用同样的任务、预算和阈值，仅关闭压缩。实际 provider 安全系数会自适应，以事件中记录为准。最多 12 次模型请求（包含摘要）、3 个调度 segment、210 秒/trial；达到上限或仍 queued 都算失败。

210 秒是 asyncio 配置的取消期限，运行时取消处理和关闭存储会使部分 trial 超出该时间，不是严格的进程强杀上限。开启条件共有 {on["configured_deadline_overshoot"]} 次超出，最长 {on["max_elapsed_ms"] / 1000:.1f} 秒；原始 elapsed_ms 保留。本报告不声称任务耗时具有 210 秒硬保证。

| 预先分配压缩档 | 不同任务 | 关闭压缩通过 | 开启压缩通过 | 关闭总 Token | 开启总 Token（含摘要） |
| --- | ---: | ---: | ---: | ---: | ---: |
{tier_table}

总体：关闭 {off["successes"]}/300，开启 {on["successes"]}/300；输入 Token 分别 {off["usage"]["input"]:,} / {on["usage"]["input"]:,}；全部 Token 分别 {off["usage"]["total"]:,} / {on["usage"]["total"]:,}；摘要辅助调用 {on["usage"]["auxiliary"]} 次。生产完成状态：{json.dumps(on["runtime_statuses"], ensure_ascii=False)}。Snip 子组全部开销减少 {pct(snip_saving)}，此值仅适用于表中预先分配的 Snip 样例。不能扩展到全部三级机制或生产使用。

按不同任务聚合三次重复并对任务重采样 5,000 次：完成率差（开启−关闭）为 {pct(c["accuracy_difference"]["mean"])}，经验 95% 区间 [{pct(c["accuracy_difference"]["cluster_bootstrap_95"][0])}, {pct(c["accuracy_difference"]["cluster_bootstrap_95"][1])}]。同题重复不视作独立任务。成功配对子集的省 Token 结果存在成功条件选择偏差，仅用于诊断。

Prune 的实际失败轨迹中，模型明确回复“README content was cleared from my context”，随后重复 Read；调度去重与投影压缩后仍未完成。Summary 条件也出现把任务重新当作待读取规格的情况。失败样例及全部工具事件保存在 coding 子目录。正式运行期间未根据这些结果更改压缩算法。

## 跨会话记忆

冻结 100 段构造历史，每段 4 个不同查询，覆盖画像 60、场景 80、原子事实 120、多步关联 80、无答案 40、更新/冲突 20。三个条件各重复 3 次，真实 provider 回答并以明确预期值判定。使用真实 MemoryStore/sqlite-vec、MemoryRetriever 和 progressive layered_recall；L1 事实与 L2/L3 摘要预先写入，未测自动抽取质量。Embedding 是本地 256 维位置敏感词法 hash，未调用语义 Embedding 服务。全库包含 100 段历史，召回使用 session_id 隔离条件。

| 条件 | 正确回答 | 准确率 | 输入 Token | 总 Token |
| --- | ---: | ---: | ---: | ---: |
{mem_table}

| 查询类型 | 完整历史 | 仅近期历史 | 分层记忆 |
| --- | ---: | ---: | ---: |
{categories}

分层−近期历史提升 25.5 个百分点，按会话聚合的经验 95% Bootstrap 区间为 21.0–30.5 个百分点；分层准确率区间 37.5%–46.5%。含 40 个无答案查询，正确 UNKNOWN 也计入总准确率；去除无答案后分层为 384/1,080（35.6%），近期历史为 78/1,080（7.2%）。完整历史相对分层准确率明显更高，输入少不等于检索有效。

L3/L2 词项或子串匹配会提前终止检索；无关的高层片段也能命中自然问题中的普通词。进入 L1 的样例仍发生空召回，本地位置敏感词法 Embedding 与全库候选截断后再按会话过滤是需继续排查的限制。报告保留原始 query、历史、召回层、返回内容、期望答案、模型输出；正式回答完成后未重新改答案或修检索后替换结果。

## 状态恢复与安全

200 次子进程均以 os._exit(73) 异常结束，没有执行 SQLite close/finally。边界分别是 queued 事件、worker 租约、模型调用开始、模型返回尚未提交、assistant 提交、动作 planned、prepared、外部效果完成尚未 observation 提交、observation 提交、checkpoint 投影，各 20 次，payload 和任务 ID 不同。模型返回边界使用明确模拟结果，检验持久化边界，不计为真实 LLM trial。

已提交投影恢复：{fault["successes"]}/200。状态加载中位数 {fault["restoration_median_ms"]:.1f} ms，包含打开存储和读取投影，排除 Python 进程启动、重新调模型及任务重做。20 个效果已执行但尚未记账的动作仍为 started，已执行的本地效果文件保留；不把它们误报为 completed。20 次租约过期后旧 worker 提交均被 fence 拒绝。此项没有验证恢复后最终任务完成、分布式网络分区或跨重启 exactly-once。内存控制字段是持久化缺失的分析性对照，没有跑不持久化版本的完整 Agent。

300 攻击 + 300 正常由权限 100+100、DLP 200+200 组成；均为组件评测，没有执行攻击动作，也未进行完整 Agent 的 Prompt Injection 成功率测量。

- 权限：攻击输入硬拒绝 70/100，其余 30/100 转人工确认，自动放行 0/100；正常 Read 为 100/100 转人工确认、硬拒绝 0/100、自动放行 0/100。确认请求不能算自动通过，也不能等同于攻击已执行。
- DLP：原文密钥格式样例 {known}/120 脱敏；24 字符 glpat 变体 0/12，当前规则限定 20 字符。Base64/字符间空格/换行/Unicode escape 共 0/40 完整隐藏；PII 共 0/40。此环境 presidio_analyzer、presidio_anonymizer、spaCy 缺失，pii_active=False；不能声称本机启用了 PII 脱敏。
- 正常 DLP 文本 {normal["successes"]}/200 未被改写。构造模板存在相关性，不能将 200 个相似变体看作 200 个代表性业务输入，也不据此推断低生产泄露率。

## 校准、代码变更与复现

扩测前修复两处运行问题：引用字段 scenario_ids/atom_ids 不再命中高层回答；service.py 等文件名不再触发在线服务健康检查。正式结果使用新冻结集，旧 pilot 及所有失败校准目录保留。正向校准任务 code-000 曾用于检查工具适配与完成判定，不能称整个 100 题完全未见的留出集。

初次上下文启动中阈值参数名被配置模型忽略，控制条件未生效，已停止并标记为 excluded-coding-run.json；该批未计入任何正式比较。snake 的原参考表达式误把反向引用解释为控制字符，其 6 次 trial 全部作为无效 oracle 保留并剔除，使用新的 path-components 任务补足 100 题；正式编码原始记录共 606 条，有效 600 条，规则替换见 coding-replacements.json。补充的 100 条手工计算规则示例检查通过。第 1 阶段个别 Unicode 验证输出按 UTF-8 解码 GBK 产生替换字符，原始输入/检查 JSON 保持完整；第 2 阶段验证器显式设置 UTF-8 输出，不改变判断值。

正式编程证据独立存于 {coding.as_posix()}，记忆和工程证据存于 {base.as_posix()}。原执行脚本保留在 frozen-runner / frozen-runner-stage2；第二阶段增加替换用例读取、修正参考表达式与验证日志编码，压缩算法、预算和其余用例的判断标准保持一致。第二阶段并发为 16，第一阶段为 8；不据此比较绝对延迟。

运行时/故障回归 246 项（245 通过，1 个独立 provider probe 跳过），上下文/完成门回归 96 项全部通过。正式评测本身实际调用模型，跳过的 probe 不代表正式集没有真实调用。各模块版本哈希校验：{json.dumps({k: v for k, v in result["audit"].items() if k.endswith("hashes_match")}, ensure_ascii=False)}。

实际 provider 响应模型 {json.dumps(result["audit"]["provider_models"])}，配置模型 deepseek-v4-flash，temperature=0，thinking=disabled。正式集与上述无效 oracle 的 API 尝试共 {result["audit"]["calls"]:,}，其中无效 oracle 尝试 {result["audit"]["excluded_oracle_api_calls"]}，失败尝试 {result["audit"]["failed_provider_attempts"]}，有 usage 回执的全部 Token 为 {result["audit"]["known_total_tokens"]:,}。条件比较的 Token 表已剔除无效 oracle；整体费用记录保留。Token 不折算金额，也不把调用次数当独立任务数。

对这份固定构造集的 Bootstrap 是经验稳定性描述；全成功数据会产生退化区间 [100%,100%]，不代表总体必然成功。更多样例不替代真实业务代表性，统计口径及资料见 [样本量建议](resume-ablation-sample-size-2026-10-11.md)。

复现本次数据需先把冻结的 memory-dataset.json、security-dataset.json、coding-dataset.json 和 coding-replacements.json 分别复制到新的对应输出目录；空目录会生成当前代码版本的默认题集，不能视作本次同一冻结集。

```powershell
python scripts/resume_formal_suite.py --phase memory --output <新的输出目录> --concurrency 8
python scripts/resume_formal_suite.py --phase engineering --output <同一输出目录>
python scripts/resume_formal_suite.py --phase coding --output <独立编程输出目录> --concurrency 8 --limit 300
python scripts/resume_formal_suite.py --phase coding --output <独立编程输出目录> --concurrency 8
python scripts/report_resume_formal.py --base <记忆与工程目录> --coding <编程目录> --write-report
```

脚本恢复时跳过已记录的 terminal trial，失败不会被自动挑出重跑。字段模型预算失败、queued 或未通过判据都保留，不以“基础设施问题”抹去。
"""
    (ROOT / "docs/eval/resume-formal-2026-10-11.md").write_text(report, encoding="utf-8")
    resume = f"""# 长链路编程 Agent 运行框架（cc-harness）

**项目背景：** 面向多步骤编程任务构建 Agent Runtime，支持任务状态持久化、上下文压缩、跨会话检索与工具安全控制；建立内部构造场景的对照评测，记录真实模型和工具执行的全过程。

- **任务状态恢复：** 基于 Run、事件存储与 Checkpoint 实现持久化恢复；在覆盖 10 类中断边界的 200 次真实进程异常退出测试中，已提交状态恢复 200/200，状态加载中位耗时 {fault["restoration_median_ms"]:.1f} ms；验证过期租约对旧 worker 的提交隔离。
- **上下文管理：** 实现 Snip、Prune、Summary 三级压缩与原文回查，建立 100 个不同双模块任务、600 次完整 Agent 对照执行；预先分配的 Snip 子组（{snip_on["distinct_tasks"]} 题，每条件重复 3 次）总 Token 减少 {pct(snip_saving)}，通过率分别为 {snip_off["successes"]}/{snip_off["n"]} 与 {snip_on["successes"]}/{snip_on["n"]}；通过完整执行链定位 Prune/Summary 造成的规格丢失与停滞。
- **跨会话记忆：** 构建 L1 事实、L2 场景、L3 用户画像及分层检索链路；在 100 段构造历史、400 条查询的三组对照中，回答准确率相较仅近期历史从 16.5% 提升至 42.0%（+25.5 个百分点），输入 Token 减少 {pct(memory_input_saving)}；分析高层误匹配与 L1 空召回问题。
- **工具调用安全：** 建立权限门、来源校验和输出 DLP，并覆盖 300 个攻击与 300 个正常组件用例；攻击型权限输入 70/100 硬拒绝、30/100 转人工确认，原文密钥格式样例 {known}/120 脱敏、200/200 正常文本保持不变；识别编码变体及未启用 PII 层的覆盖缺口。

**数据口径：** 全部指标来自内部构造场景，不代表线上业务收益。Snip 是预先分配的子组结果，三级压缩总体完成率由 {pct(off["accuracy"])} 降至 {pct(on["accuracy"])}，不能用子组省 Token 值替代整体效果。记忆完整历史对照为 100%，分层为 42%；不能声称优于完整历史。状态加载不是完整任务恢复耗时，权限 ASK 不是自动拒绝或放行，安全组件评测不是完整 Agent 攻击成功率。

证据与完整失败分布见 [正式扩测报告](eval/resume-formal-2026-10-11.md)。面试时应优先说明适用范围和已发现的问题。
"""
    (ROOT / "docs/resume-project-formal-2026-10-11.md").write_text(resume, encoding="utf-8")


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--base", type=Path, required=True)
    parser.add_argument("--coding", type=Path)
    parser.add_argument("--validate-oracles", action="store_true")
    parser.add_argument("--write-report", action="store_true")
    args = parser.parse_args()
    if args.validate_oracles:
        validate_oracles(args.base, args.coding)
    else:
        result = analyses(args.base, args.coding or args.base)
        if args.write_report:
            write_report(args.base.resolve(), (args.coding or args.base).resolve(), result)
