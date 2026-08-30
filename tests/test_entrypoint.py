from types import SimpleNamespace

import pytest

from cc_harness.entrypoint import (
    _has_usable_committed_final,
    _api_cost_status,
    _run_print,
    async_main,
    build_parser,
)


def test_api_cost_status_requires_provider_fact_and_supports_legacy_direct_cost():
    assert _api_cost_status(SimpleNamespace(api_reported_cost=None)) == "unavailable"
    assert _api_cost_status(
        SimpleNamespace(api_reported_cost=0.25, api_cost_observed=True, api_cost_complete=True)
    ) == "reported"
    assert _api_cost_status(
        SimpleNamespace(api_reported_cost=None, api_cost_observed=True, api_cost_complete=False)
    ) == "incomplete"
    # Older callers that only exposed a provider cost remain compatible; no
    # token tariff is inferred when the cost field is absent.
    assert _api_cost_status(SimpleNamespace(api_reported_cost=0.25)) == "reported"


def test_installed_cli_contract_parses_claude_style_flags(tmp_path):
    args = build_parser().parse_args(
        [
            "-c",
            "--add-dir",
            str(tmp_path),
            "--effort",
            "high",
            "--permission-mode",
            "auto-edit",
        ]
    )
    assert args.continue_session is True
    assert args.add_dir == [tmp_path]
    assert args.effort == "high"
    assert args.permission_mode == "auto-edit"


def test_resume_picker_and_print_mode_parse():
    args = build_parser().parse_args(["-r"])
    assert args.resume == "picker"
    args = build_parser().parse_args(["-p", "hello"])
    assert args.print_mode is True
    assert args.prompt == "hello"
    assert args.output_format == "text"


def test_print_mode_supports_machine_readable_output():
    args = build_parser().parse_args(["-p", "--output-format", "json", "hello"])
    assert args.output_format == "json"


def test_iteration_budget_is_configurable_and_bounded():
    assert build_parser().parse_args([]).max_iterations == 20
    assert build_parser().parse_args(["--max-iterations", "50"]).max_iterations == 50
    assert build_parser().parse_args(["--unbounded-iterations"]).max_iterations is None
    with pytest.raises(SystemExit):
        build_parser().parse_args(["--max-iterations", "0"])
    with pytest.raises(SystemExit):
        build_parser().parse_args(
            ["--max-iterations", "50", "--unbounded-iterations"]
        )


def test_host_execution_requires_explicit_flag():
    assert build_parser().parse_args([]).host_execution is False
    assert build_parser().parse_args(["--host-execution"]).host_execution is True


def test_capability_profile_is_explicit_and_bare_remains_compatible():
    assert build_parser().parse_args([]).capability_profile is None
    assert (
        build_parser().parse_args(
            ["--capability-profile", "context-eval"]
        ).capability_profile
        == "context-eval"
    )
    assert build_parser().parse_args(["--bare"]).bare is True


@pytest.mark.asyncio
async def test_sandbox_capability_profile_prints_without_starting_runtime(capsys):
    assert await async_main(["--sandbox-capabilities"]) == 0
    output = capsys.readouterr().out
    assert '"security_label": "restricted-preview"' in output
    assert '"isolated_claim_allowed": false' in output


@pytest.mark.asyncio
async def test_print_json_reports_provider_model_and_usage(tmp_path, monkeypatch, capsys):
    from cc_harness.terminal.attachments import AttachmentManager

    async def prepare(self, prompt_text, *, confirm_outside):
        del self, confirm_outside
        return prompt_text, []

    monkeypatch.setattr(AttachmentManager, "prepare", prepare)

    class Runtime:
        cwd = tmp_path
        additional_dirs = ()
        session_store = SimpleNamespace(attachments_root=tmp_path / "attachments")
        state = SimpleNamespace(session_id="session-test")
        llm = SimpleNamespace(model="deepseek-v4-flash", resolved_model="deepseek-v4-flash")

        async def run_user_turn(self, prompt_text, *, event_emitter, **kwargs):
            del prompt_text, kwargs
            await event_emitter({"type": "result", "text": "done ✅"})
            return SimpleNamespace(
                error=None,
                api_prompt_tokens=12,
                api_uncached_prompt_tokens=3,
                api_cache_creation_prompt_tokens=2,
                api_cache_read_prompt_tokens=7,
                api_completion_tokens=4,
                iter_count=2,
                tool_call_log=[{"name": "read"}],
            )

        async def close(self):
            return None

    assert await _run_print(Runtime(), "hello", "bypass-prompts", "json") == 0
    output = capsys.readouterr().out
    assert '"resolved_model": "deepseek-v4-flash"' in output
    assert '"input_tokens": 12' in output
    assert '"uncached_input_tokens": 3' in output
    assert '"cache_creation_input_tokens": 2' in output
    assert '"cache_read_input_tokens": 7' in output
    assert '"trajectory":' in output
    assert "✅" in output


@pytest.mark.asyncio
async def test_print_json_preserves_structured_error(tmp_path, monkeypatch, capsys):
    from cc_harness.terminal.attachments import AttachmentManager

    async def prepare(self, prompt_text, *, confirm_outside):
        del self, confirm_outside
        return prompt_text, []

    monkeypatch.setattr(AttachmentManager, "prepare", prepare)

    class Runtime:
        cwd = tmp_path
        additional_dirs = ()
        session_store = SimpleNamespace(attachments_root=tmp_path / "attachments")
        state = SimpleNamespace(session_id="session-error")
        llm = SimpleNamespace(model="deepseek-v4-flash", resolved_model=None)

        async def run_user_turn(self, prompt_text, *, event_emitter, **kwargs):
            del prompt_text, event_emitter, kwargs
            return SimpleNamespace(error="provider unavailable")

        async def close(self):
            return None

    assert await _run_print(Runtime(), "hello", "bypass-prompts", "json") == 1
    output = capsys.readouterr().out
    assert '"error": "provider unavailable"' in output


@pytest.mark.asyncio
async def test_usable_committed_final_requires_successful_last_observation():
    artifacts = {
        "final": '{"role":"assistant","content":"All checks pass."}',
    }

    class Store:
        artifacts = SimpleNamespace(read_text=lambda digest: artifacts[digest])

        async def read(self, run_id, *, limit):
            del run_id, limit
            return SimpleNamespace(
                events=[
                    SimpleNamespace(
                        event_type="ToolObservationCommitted",
                        payload={"status": "succeeded"},
                    ),
                    SimpleNamespace(
                        event_type="AssistantMessageCommitted",
                        payload={"message_artifact": "final"},
                    ),
                    SimpleNamespace(
                        event_type="RunFailed",
                        payload={"target_status": "failed_recoverable"},
                    ),
                ]
            )

    assert await _has_usable_committed_final(SimpleNamespace(store=Store()), "run")


@pytest.mark.asyncio
async def test_failed_last_observation_does_not_mask_recoverable_failure():
    artifacts = {"final": '{"role":"assistant","content":"Let me test one more thing."}'}

    class Store:
        artifacts = SimpleNamespace(read_text=lambda digest: artifacts[digest])

        async def read(self, run_id, *, limit):
            del run_id, limit
            return SimpleNamespace(
                events=[
                    SimpleNamespace(
                        event_type="ToolObservationCommitted",
                        payload={"status": "failed"},
                    ),
                    SimpleNamespace(
                        event_type="AssistantMessageCommitted",
                        payload={"message_artifact": "final"},
                    ),
                ]
            )

    assert not await _has_usable_committed_final(SimpleNamespace(store=Store()), "run")


@pytest.mark.asyncio
async def test_durable_print_aggregates_invocation_usage_once(tmp_path, capsys):
    from cc_harness.entrypoint import _write_durable_print_result

    class Store:
        class Artifacts:
            @staticmethod
            def read_text(_digest):
                return '{"role":"assistant","content":"done"}'

        artifacts = Artifacts()

        async def read(self, run_id, *, limit):
            del run_id, limit
            return SimpleNamespace(
                events=[
                    SimpleNamespace(
                        event_type="ModelInvocationFinished",
                        payload={
                            "invocation_id": "one",
                            "status": "succeeded",
                            "usage": {
                                "input_tokens": 100,
                                "cache_read_input_tokens": 80,
                                "output_tokens": 4,
                                "model_calls": 1,
                                "reported_cost": 0.1,
                                "reported_cost_currency": "USD",
                            },
                        },
                    ),
                    SimpleNamespace(
                        event_type="ModelInvocationFinished",
                        payload={
                            "invocation_id": "two",
                            "status": "failed",
                            "usage": {
                                "input_tokens": 50,
                                "output_tokens": 2,
                                "model_calls": 1,
                                "reported_cost": 0.2,
                                "reported_cost_currency": "USD",
                            },
                        },
                    ),
                    # This legacy payload must not be counted a second time
                    # when invocation terminal facts are present.
                    SimpleNamespace(
                        event_type="AssistantMessageCommitted",
                        payload={
                            "message_artifact": "message",
                            "usage": {"input_tokens": 999, "model_calls": 99, "reported_cost": 9},
                        },
                    ),
                ]
            )

    class Client:
        store = Store()
        _llm = SimpleNamespace(model="model-x", resolved_model="model-x")

    assert await _write_durable_print_result(Client(), "run", error=None) == 0
    payload = __import__("json").loads(capsys.readouterr().out)
    assert payload["usage"]["model_calls"] == 2
    assert payload["usage"]["input_tokens"] == 150
    assert payload["usage"]["api_reported_cost"] == pytest.approx(0.3)
    assert payload["usage"]["api_cost_status"] == "reported"
