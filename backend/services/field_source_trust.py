"""Helpers for turning field provenance strings into trust scores."""

from __future__ import annotations

from typing import Mapping


DEFAULT_FIELD_SOURCE_TRUST = {
    "api": 1.0,
    "user_typed": 0.9,
    "ocr": 0.6,
    "unknown": 0.5,
}


def normalize_field_source(source: str | None) -> str:
    text = (source or "unknown").strip().lower()
    if text in {"api", "user_typed", "ocr", "unknown"}:
        return text
    if text.startswith("user") or text in {"manual", "user_confirmed"}:
        return "user_typed"
    if text.startswith("ocr") or "vision" in text:
        return "ocr"
    if any(token in text for token in ("api", "mcp", "sandbox", "mock", "didi", "ctrip", "card")):
        return "api"
    return "unknown"


def field_source_trust_score(
    source: str | None,
    trust_config: Mapping[str, float] | None = None,
) -> tuple[str, float]:
    normalized = normalize_field_source(source)
    config = dict(DEFAULT_FIELD_SOURCE_TRUST)
    if trust_config:
        config.update({str(k): float(v) for k, v in trust_config.items()})
    return normalized, float(config.get(normalized, config["unknown"]))
