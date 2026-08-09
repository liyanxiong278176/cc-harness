from __future__ import annotations

from datetime import UTC, datetime

from eval.canary import (
    PairedCanaryRunner,
    PairedRetryPolicy,
    install_canary_contracts,
)
from eval.core import (
    AdapterIdentity,
    EvalRunManifest,
    EvalStore,
    GraderResult,
    LocalEvalRunner,
    ResourceUsage,
    ResultStatus,
    RunState,
    TrialExecutionContext,
    TrialResult,
    content_fingerprint,
)
from eval.core.tests.test_models import manifest
from eval.launch import HarnessKind


class SequencedAdapter:
    def __init__(self, identity: AdapterIdentity, *, transient_first: bool) -> None:
        self.identity = identity
        self.transient_first = transient_first
        self.calls = 0

    async def run_trial(self, context: TrialExecutionContext) -> TrialResult:
        self.calls += 1
        common = {
            "trial_id": context.request.trial_id,
            "run_id": context.request.run_id,
            "run_manifest_digest": context.request.run_manifest_digest,
            "task_id": context.request.task.task_id,
            "task_contract_digest": content_fingerprint(context.request.task),
            "attempt": context.attempt,
            "adapter": self.identity,
            "started_at": datetime.now(UTC),
            "finished_at": datetime.now(UTC),
            "usage": ResourceUsage(
                wall_time_ms=1,
                steps=1,
                model_calls=1,
                tool_calls=0,
                input_tokens=1,
                output_tokens=1,
                cost_microusd=None,
            ),
        }
        if self.transient_first and self.calls == 1:
            return TrialResult(
                **common,
                status=ResultStatus.INVALID,
                invalid_reason="transient provider failure: exit code 1, 503",
            )
        outcome = await context.artifacts.put_artifact(b'{"passed":true}', "application/json")
        return TrialResult(
            **common,
            status=ResultStatus.PASS,
            grader_results=(
                GraderResult(
                    grader_id=context.request.task.graders[0].grader_id,
                    status=ResultStatus.PASS,
                    score=1.0,
                ),
            ),
            outcome_ref=outcome,
        )


async def test_transient_failure_retries_both_harnesses_and_selects_joint_round(tmp_path) -> None:
    store = EvalStore(tmp_path / "evidence")
    await store.open()
    try:
        contract = (await install_canary_contracts(store))[0]
        base = manifest()
        model = base.subject.model.model_copy(update={"resolved_model": "deepseek-v4-flash"})
        common = {
            **base.model_dump(mode="python"),
            "created_at": datetime.now(UTC),
            "task_contract_digests": (content_fingerprint(contract),),
            "default_budget": contract.budget,
            "repetitions": 1,
        }
        cc_manifest = EvalRunManifest(
            **{
                **common,
                "run_id": "paired-cc",
                "subject": base.subject.model_copy(update={"model": model}),
            }
        )
        claude_manifest = EvalRunManifest(
            **{
                **common,
                "run_id": "paired-claude",
                "subject": base.subject.model_copy(
                    update={"subject_id": "claude-code", "model": model}
                ),
            }
        )
        await store.create_run(cc_manifest)
        await store.create_run(claude_manifest)
        cc_identity = AdapterIdentity(adapter_id="canary-cc-harness", adapter_version="1.0.0")
        claude_identity = AdapterIdentity(adapter_id="canary-claude-code", adapter_version="1.0.0")
        cc_adapter = SequencedAdapter(cc_identity, transient_first=True)
        claude_adapter = SequencedAdapter(claude_identity, transient_first=False)
        local = LocalEvalRunner(
            store,
            (cc_adapter, claude_adapter),
            worker_id="paired-worker",
        )
        progress: list[str] = []
        paired = PairedCanaryRunner(
            store,
            local,
            {
                HarnessKind.CC_HARNESS: cc_manifest,
                HarnessKind.CLAUDE_CODE: claude_manifest,
            },
            {
                HarnessKind.CC_HARNESS: cc_identity,
                HarnessKind.CLAUDE_CODE: claude_identity,
            },
            policy=PairedRetryPolicy(maximum_attempts=2, cooldown_seconds=0),
            progress=progress.append,
        )

        selections = await paired.run((contract,))

        assert len(selections) == 1
        selection = selections[0]
        assert selection.valid_pair_selected is True
        assert len(selection.attempts) == 2
        assert selection.attempts[0].synchronized_retry is True
        assert selection.selected_cc_harness_trial_id.endswith("paired-2")
        assert selection.selected_claude_code_trial_id.endswith("paired-2")
        assert cc_adapter.calls == claude_adapter.calls == 2
        assert len(await store.list_results(cc_manifest.run_id)) == 2
        assert len(await store.list_results(claude_manifest.run_id)) == 2
        assert await store.get_run_state(cc_manifest.run_id) is RunState.COMPLETED
        assert await store.get_run_state(claude_manifest.run_id) is RunState.COMPLETED
        assert progress[0].startswith("paired attempt 1/2")
        assert any("cc-harness=invalid, claude-code=pass" in item for item in progress)
        assert any(item.startswith("paired attempt 2/2") for item in progress)
    finally:
        await store.close()
