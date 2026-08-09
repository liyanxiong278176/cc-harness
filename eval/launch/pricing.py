"""Frozen normalized pricing for the Claude Code parity route."""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from decimal import ROUND_HALF_UP, Decimal


@dataclass(frozen=True)
class PricingContract:
    contract_id: str
    version: str
    model: str
    uncached_input_usd_per_million: Decimal
    cache_creation_usd_per_million: Decimal
    cache_read_usd_per_million: Decimal
    output_usd_per_million: Decimal

    def projection(self) -> dict[str, str]:
        return {
            "schema_version": "eval.pricing-contract.v1",
            "contract_id": self.contract_id,
            "version": self.version,
            "model": self.model,
            "uncached_input_usd_per_million": str(
                self.uncached_input_usd_per_million
            ),
            "cache_creation_usd_per_million": str(
                self.cache_creation_usd_per_million
            ),
            "cache_read_usd_per_million": str(self.cache_read_usd_per_million),
            "output_usd_per_million": str(self.output_usd_per_million),
        }

    @property
    def digest(self) -> str:
        raw = json.dumps(
            self.projection(), sort_keys=True, separators=(",", ":")
        ).encode("utf-8")
        return f"sha256:{hashlib.sha256(raw).hexdigest()}"

    def cost_microusd(
        self,
        *,
        uncached_input_tokens: int,
        cache_creation_input_tokens: int,
        cache_read_input_tokens: int,
        output_tokens: int,
    ) -> int:
        counts = (
            uncached_input_tokens,
            cache_creation_input_tokens,
            cache_read_input_tokens,
            output_tokens,
        )
        if any(value < 0 for value in counts):
            raise ValueError("token counts cannot be negative")
        cost = (
            Decimal(uncached_input_tokens) * self.uncached_input_usd_per_million
            + Decimal(cache_creation_input_tokens)
            * self.cache_creation_usd_per_million
            + Decimal(cache_read_input_tokens) * self.cache_read_usd_per_million
            + Decimal(output_tokens) * self.output_usd_per_million
        )
        return int(cost.quantize(Decimal(1), rounding=ROUND_HALF_UP))


PARITY_PRICING = PricingContract(
    contract_id="deepseek-v4-flash.claude-code-route",
    version="1.0.0",
    model="deepseek-v4-flash",
    uncached_input_usd_per_million=Decimal(5),
    cache_creation_usd_per_million=Decimal("6.25"),
    cache_read_usd_per_million=Decimal("0.5"),
    output_usd_per_million=Decimal(25),
)
