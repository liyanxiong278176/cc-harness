from eval.launch import PARITY_PRICING


def test_parity_pricing_matches_observed_claude_code_tariff() -> None:
    cost = PARITY_PRICING.cost_microusd(
        uncached_input_tokens=7_694,
        cache_creation_input_tokens=0,
        cache_read_input_tokens=124_032,
        output_tokens=9_466,
    )

    assert cost == 337_136
    assert PARITY_PRICING.digest.startswith("sha256:")


def test_parity_pricing_counts_cache_creation_separately() -> None:
    cost = PARITY_PRICING.cost_microusd(
        uncached_input_tokens=1,
        cache_creation_input_tokens=2,
        cache_read_input_tokens=3,
        output_tokens=4,
    )

    assert cost == 119
