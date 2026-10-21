from cc_harness.loop_control import completion_contract_from_instruction


def test_source_filenames_do_not_request_live_service():
    for path in ("service.py", "server.ts", "endpoint.js", "src/service/core.py"):
        contract = completion_contract_from_instruction(f"Repair {path} and run unit tests.")
        assert not contract.require_service_health_check


def test_real_service_still_requires_health_check():
    for text in (
        "Start the server and verify readiness",
        "Deploy the service",
        "Check http://localhost:8080/health",
    ):
        assert completion_contract_from_instruction(text).require_service_health_check
