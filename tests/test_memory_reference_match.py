from cc_harness.memory.recall import _matches


def test_scenario_reference_does_not_short_circuit_a_fact_query():
    persona = "summary: prefers unittest\nscenario_ids:\n- booking-v0001\n"
    assert not _matches("Booking rules cancel_cutoff_hours", persona)
    assert _matches("prefers unittest", persona)


def test_inline_reference_ids_are_not_answer_evidence():
    assert not _matches("calibration", "summary: coding preferences\natom_ids: [calibration-1]")


def test_matching_resumes_after_reference_metadata():
    value = "scenario_ids:\n- unrelated\nsummary: booking rules use half-open slots"
    assert _matches("booking rules", value)


def test_answer_bearing_lists_are_preserved():
    assert _matches("unittest", "Preferences:\n- use unittest\n- avoid pytest")
