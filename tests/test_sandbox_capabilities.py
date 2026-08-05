from cc_harness.sandbox_capabilities import CapabilityStatus, sandbox_capability_profile


def test_profile_withholds_isolated_claim_until_all_controls_are_proven():
    profile = sandbox_capability_profile()

    assert profile["schema_version"] == 2
    assert profile["security_label"] == "restricted-preview"
    assert profile["isolated_claim_allowed"] is False
    statuses = {item["status"] for item in profile["capabilities"].values()}
    assert CapabilityStatus.ENFORCED.value not in statuses
    assert statuses == {CapabilityStatus.PARTIAL.value}


def test_newly_wired_controls_remain_partial_until_conformance():
    capabilities = sandbox_capability_profile()["capabilities"]

    for name in (
        "process_isolation",
        "resource_limits",
        "network_egress",
        "credential_isolation",
    ):
        assert capabilities[name]["status"] == CapabilityStatus.PARTIAL.value
        assert capabilities[name]["evidence"]
        assert capabilities[name]["blockers"]


def test_every_incomplete_capability_names_its_blockers():
    profile = sandbox_capability_profile()

    for capability in profile["capabilities"].values():
        if capability["status"] != CapabilityStatus.ENFORCED.value:
            assert capability["blockers"]


def test_profile_exposes_release_evidence_policy():
    gate = sandbox_capability_profile()["release_gate"]

    assert gate["required_platforms"] == ["Linux", "Windows"]
    assert gate["minimum_consecutive_runs_per_platform"] == 2
    assert gate["requires_clean_source"] is True
