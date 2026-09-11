"""Provider price list used to turn token counts into a cost estimate.

Prices change and vary by contract, so the built-in table is a documented
default, not a claim about anyone's invoice. Override it with a JSON file at
``CITEPRISM_PRICING_TABLE``; every reported cost carries the table version so a
figure can always be traced back to the rates that produced it.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from pathlib import Path

logger = logging.getLogger(__name__)

TABLE_VERSION = "2026-05-defaults"

# USD per 1,000,000 tokens: (input, output).
DEFAULT_RATES: dict[str, tuple[float, float]] = {
    "google:gemini-2.5-flash": (0.30, 2.50),
    "google:gemini-2.5-pro": (1.25, 10.00),
    "openai:gpt-4o": (2.50, 10.00),
    "openai:gpt-4o-mini": (0.15, 0.60),
    "huggingface:meta-llama/Llama-3.1-8B-Instruct": (0.06, 0.06),
}

# Metadata APIs are request-priced, not token-priced. USD per 1,000 requests.
DEFAULT_REQUEST_RATES: dict[str, float] = {
    "openalex": 0.0,
    "crossref": 0.0,
    "semantic_scholar": 0.0,
    "unpaywall": 0.0,
}


@dataclass(frozen=True)
class PricingTable:
    version: str = TABLE_VERSION
    token_rates: dict[str, tuple[float, float]] = None  # type: ignore[assignment]
    request_rates: dict[str, float] = None  # type: ignore[assignment]

    def __post_init__(self):
        object.__setattr__(self, "token_rates", self.token_rates or dict(DEFAULT_RATES))
        object.__setattr__(
            self, "request_rates", self.request_rates or dict(DEFAULT_REQUEST_RATES)
        )

    @classmethod
    def load(cls, path: str | Path | None) -> "PricingTable":
        if not path:
            return cls()
        try:
            document = json.loads(Path(path).read_text(encoding="utf-8"))
        except (OSError, ValueError):
            logger.warning("Could not read the pricing table; using built-in defaults.")
            return cls()
        token_rates = {
            key: (float(value[0]), float(value[1]))
            for key, value in (document.get("token_rates") or {}).items()
            if isinstance(value, (list, tuple)) and len(value) == 2
        }
        request_rates = {
            key: float(value)
            for key, value in (document.get("request_rates") or {}).items()
        }
        return cls(
            version=str(document.get("version") or "custom"),
            token_rates={**DEFAULT_RATES, **token_rates},
            request_rates={**DEFAULT_REQUEST_RATES, **request_rates},
        )

    def cost(
        self,
        *,
        provider: str | None,
        model: str | None = None,
        input_tokens: int = 0,
        output_tokens: int = 0,
        requests: int = 0,
    ) -> float:
        if not provider:
            return 0.0
        key = f"{provider}:{model}" if model else provider
        rates = self.token_rates.get(key)
        if rates is None and model:
            rates = self.token_rates.get(provider)
        if rates is not None:
            inbound, outbound = rates
            return round(
                (max(0, input_tokens) * inbound + max(0, output_tokens) * outbound) / 1_000_000,
                6,
            )
        per_thousand = self.request_rates.get(provider)
        if per_thousand is None:
            return 0.0
        return round(max(0, requests) * per_thousand / 1000, 6)

    def known_models(self) -> list[str]:
        return sorted(self.token_rates)


def cost_for(table: PricingTable | None, **kwargs) -> float:
    return (table or PricingTable()).cost(**kwargs)


__all__ = ["DEFAULT_RATES", "PricingTable", "TABLE_VERSION", "cost_for"]
