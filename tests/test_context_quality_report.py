from scripts.report_context_quality import compare


def test_cost_includes_summary_and_paired_quality_counts_every_failure():
    observations = []
    calls = []
    for repeat in range(3):
        for arm in ("context_off", "context_on"):
            trial = f"case-{arm}-{repeat}"
            observations.append(
                {
                    "trial_id": trial,
                    "case": "case",
                    "condition": arm,
                    "ok": arm == "context_off" or repeat != 0,
                }
            )
            calls.append(
                {
                    "trial_id": trial,
                    "ok": True,
                    "tool_schema_present": True,
                    "usage": {
                        "prompt_tokens": 90 if arm == "context_off" else 40,
                        "total_tokens": 100 if arm == "context_off" else 50,
                    },
                }
            )
            if arm == "context_on":
                calls.append(
                    {
                        "trial_id": trial,
                        "ok": True,
                        "tool_schema_present": False,
                        "usage": {"prompt_tokens": 15, "total_tokens": 20},
                    }
                )
    result = compare(observations, calls)
    assert result["arms"]["context_on"]["total_tokens"] == 210
    assert result["arms"]["context_on"]["summary_calls"] == 3
    assert result["arms"]["context_on"]["passed"] == 2
    assert result["task_count"] == 1
    assert not result["observed_target_met"]


def test_three_repeats_per_task_are_one_bootstrap_cluster():
    observations = [
        {
            "trial_id": f"task-{arm}-{i}",
            "case": "task",
            "condition": arm,
            "ok": arm == "context_on" or i != 0,
        }
        for arm in ("context_off", "context_on")
        for i in range(3)
    ]
    result = compare(observations, [])
    assert result["task_count"] == 1
    lower, upper = result["task_cluster_bootstrap_95_interval"]
    assert lower == upper
    assert round(lower, 3) == 0.333
