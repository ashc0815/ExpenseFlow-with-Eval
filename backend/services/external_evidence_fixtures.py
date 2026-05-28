"""Deterministic external evidence fixtures for expense assistant evals.

The lookup tools use this module for local/mock provider mode. It supports
three selection modes:

1. Explicit fixture id in tool args, e.g. {"fixture_id": "..."}.
2. Eval case id in tool args or EXPENSE_EVAL_CASE_ID / EVAL_CASE_ID.
3. Default local fixtures for normal dev and legacy tests.
"""
from __future__ import annotations

import copy
import hashlib
import json
import os
from pathlib import Path
from typing import Any

import yaml


DOMAIN_CONFIG = {
    "ctrip": {
        "source": "ctrip_mock",
        "provider_domain": "ctrip_booking",
        "file": "mock_ctrip_bookings.yaml",
        "direct_arg": "ctrip_fixture_id",
        "default_fixture_ids": [
            "ctrip_gold_flight_shanghai_shenzhen_1280",
            "ctrip_gold_hotel_shenzhen_680",
            "ctrip_rebooked_old_to_current",
            "ctrip_cancelled_full_refund_900",
        ],
    },
    "didi": {
        "source": "didi_mock",
        "provider_domain": "didi_trip",
        "file": "mock_didi_trips.yaml",
        "direct_arg": "didi_fixture_id",
        "default_fixture_ids": [
            "didi_gold_shanghai_airport_86",
            "didi_gold_shenzhen_hotel_client_54",
        ],
    },
    "card": {
        "source": "card_mock",
        "provider_domain": "card_transaction",
        "file": "mock_card_transactions.yaml",
        "direct_arg": "card_fixture_id",
        "default_fixture_ids": [
            "card_gold_didi_shanghai_86",
            "card_gold_flight_ctrip_1280",
            "card_gold_hotel_shenzhen_680",
        ],
    },
}

CONTROL_ARG_KEYS = {
    "fixture_id",
    "case_id",
    "ctrip_fixture_id",
    "didi_fixture_id",
    "card_fixture_id",
}

ERROR_STATUSES = {
    "not_configured",
    "provider_error",
    "timeout",
    "auth_error",
    "rate_limited",
    "malformed_response",
}


def lookup_fixture_evidence(domain: str, args: dict | None) -> dict:
    """Lookup deterministic mock evidence for one provider domain."""
    if domain not in DOMAIN_CONFIG:
        raise ValueError(f"Unsupported fixture domain: {domain}")

    query = dict(args or {})
    cfg = DOMAIN_CONFIG[domain]
    fixture_id = _requested_fixture_id(domain, query)
    case_id = _requested_case_id(query)

    if fixture_id:
        fixture = _fixture_by_id(domain, fixture_id)
        if fixture is None:
            return _empty_result(
                domain,
                query,
                status="not_found",
                fixture_id=fixture_id,
                case_id=case_id,
                error=f"Unknown {domain} fixture_id={fixture_id}",
            )
        return _result_from_fixture(domain, query, fixture, case_id=case_id)

    if case_id:
        case_fixture_id = _fixture_id_for_case(case_id, domain)
        if not case_fixture_id:
            return _empty_result(
                domain,
                query,
                status="not_found",
                case_id=case_id,
                error=f"Case {case_id} does not define a {domain} fixture",
            )
        fixture = _fixture_by_id(domain, case_fixture_id)
        if fixture is None:
            return _empty_result(
                domain,
                query,
                status="not_found",
                fixture_id=case_fixture_id,
                case_id=case_id,
                error=f"Case {case_id} references unknown {domain} fixture_id={case_fixture_id}",
            )
        return _result_from_fixture(domain, query, fixture, case_id=case_id)

    candidates = _default_candidates(domain)
    matches = [c for c in candidates if _matches_lookup(c, query)]
    if not matches and domain == "ctrip" and query.get("amount") is not None:
        loose_query = dict(query)
        loose_query.pop("amount", None)
        matches = [c for c in candidates if _matches_lookup(c, loose_query)]

    status = "ok" if matches else "not_found"
    return _build_result(
        domain=domain,
        query=query,
        status=status,
        candidates=matches,
        fixture_id=None,
        case_id=None,
        error=None,
        raw_response=None,
    )


def _fixture_root() -> Path:
    configured = os.getenv("EXPENSE_EVIDENCE_FIXTURE_DIR", "").strip()
    if configured:
        return Path(configured).expanduser()
    return Path(__file__).resolve().parents[2] / "demo_receipt"


def _load_yaml(filename: str) -> dict:
    path = _fixture_root() / filename
    if not path.exists():
        return {}
    with open(path, encoding="utf-8") as fh:
        return yaml.safe_load(fh) or {}


def _fixtures_for_domain(domain: str) -> list[dict]:
    data = _load_yaml(DOMAIN_CONFIG[domain]["file"])
    return list(data.get("fixtures") or [])


def _fixture_by_id(domain: str, fixture_id: str) -> dict | None:
    wanted = _norm(fixture_id)
    for fixture in _fixtures_for_domain(domain):
        if _norm(fixture.get("fixture_id")) == wanted:
            return fixture
    return None


def _fixture_id_for_case(case_id: str, domain: str) -> str | None:
    data = _load_yaml("expense_reconciliation_eval_cases.yaml")
    wanted = _norm(case_id)
    for case in data.get("cases") or []:
        if _norm(case.get("id")) != wanted:
            continue
        fixtures = case.get("fixtures") or {}
        fixture_id = fixtures.get(domain)
        return str(fixture_id) if fixture_id else None
    fixture_id = _fixture_id_for_chatbot_case(case_id, domain)
    if fixture_id:
        return fixture_id
    return None


def _fixture_id_for_chatbot_case(case_id: str, domain: str) -> str | None:
    """Find provider fixtures embedded in chatbot eval cases.

    The all_scenarios1 eval cases are authored in the chatbot dataset, not the
    provider dataset above. Real models usually query by date/amount/city rather
    than by fixture_id, so the eval harness sets EXPENSE_EVAL_CASE_ID and this
    resolver makes the mock provider return the intended deterministic fixture.
    """
    dataset_path = Path(__file__).resolve().parents[2] / "backend" / "tests" / "eval_datasets" / "chatbot_expense_assistant.yaml"
    if not dataset_path.exists():
        return None
    try:
        cases = yaml.safe_load(dataset_path.read_text(encoding="utf-8")) or []
    except (OSError, yaml.YAMLError):
        return None

    tool_names = {
        "ctrip": {"lookup_ctrip_booking"},
        "didi": {"lookup_didi_trip"},
        "card": {"lookup_card_transaction"},
    }.get(domain, set())
    wanted = _norm(case_id)
    for case in cases:
        if _norm(case.get("id")) != wanted:
            continue
        for turn in case.get("scripted_turns") or []:
            for call in turn.get("tool_calls") or []:
                if call.get("name") not in tool_names:
                    continue
                inp = call.get("input") or {}
                fixture_id = inp.get("fixture_id") or inp.get(DOMAIN_CONFIG[domain]["direct_arg"])
                if fixture_id:
                    return str(fixture_id)
        expected_args = ((case.get("expect") or {}).get("tool_args") or {})
        for tool_name in tool_names:
            inp = expected_args.get(tool_name) or {}
            fixture_id = inp.get("fixture_id") or inp.get(DOMAIN_CONFIG[domain]["direct_arg"])
            if fixture_id:
                return str(fixture_id)
        return None
    return None


def _default_candidates(domain: str) -> list[dict]:
    candidates: list[dict] = []
    for fixture_id in DOMAIN_CONFIG[domain]["default_fixture_ids"]:
        fixture = _fixture_by_id(domain, fixture_id)
        if fixture:
            candidates.extend(copy.deepcopy(fixture.get("candidates") or []))
    return candidates


def _requested_fixture_id(domain: str, query: dict) -> str | None:
    direct_arg = DOMAIN_CONFIG[domain]["direct_arg"]
    for key in ("fixture_id", direct_arg):
        value = query.get(key)
        if value:
            return str(value)
    return None


def _requested_case_id(query: dict) -> str | None:
    value = (
        query.get("case_id")
        or os.getenv("EXPENSE_EVAL_CASE_ID")
        or os.getenv("EVAL_CASE_ID")
    )
    return str(value) if value else None


def _result_from_fixture(domain: str, query: dict, fixture: dict, *, case_id: str | None) -> dict:
    behavior = str(fixture.get("behavior") or "ok")
    candidates = copy.deepcopy(fixture.get("candidates") or [])
    status = _status_for_fixture(behavior, candidates)
    return _build_result(
        domain=domain,
        query=query,
        status=status,
        candidates=candidates,
        fixture_id=str(fixture.get("fixture_id") or ""),
        case_id=case_id,
        error=fixture.get("error"),
        raw_response=fixture.get("raw_response"),
    )


def _status_for_fixture(behavior: str, candidates: list[dict]) -> str:
    if behavior in ERROR_STATUSES or behavior in {"not_found", "missing_fields", "conflict"}:
        return behavior
    if behavior == "multiple_candidates":
        return "multiple_candidates"
    if len(candidates) > 1:
        return "multiple_candidates"
    if candidates:
        return "ok"
    return "not_found"


def _empty_result(
    domain: str,
    query: dict,
    *,
    status: str,
    fixture_id: str | None = None,
    case_id: str | None = None,
    error: str | None = None,
) -> dict:
    return _build_result(
        domain=domain,
        query=query,
        status=status,
        candidates=[],
        fixture_id=fixture_id,
        case_id=case_id,
        error=error,
        raw_response=None,
    )


def _build_result(
    *,
    domain: str,
    query: dict,
    status: str,
    candidates: list[dict],
    fixture_id: str | None,
    case_id: str | None,
    error: Any,
    raw_response: Any,
) -> dict:
    cfg = DOMAIN_CONFIG[domain]
    normalized_request = _normalized_request(query)
    response_normalized = {
        "status": status,
        "candidates": candidates,
        "candidate_count": len(candidates),
        "error": error,
    }
    if raw_response is not None:
        response_normalized["raw_response"] = raw_response

    result = {
        "source": cfg["source"],
        "provider": "local_mock",
        "provider_name": f"{domain}_fixture_provider",
        "provider_domain": cfg["provider_domain"],
        "provider_mode": "mock",
        "status": status,
        "fixture_id": fixture_id,
        "case_id": case_id,
        "query": query,
        "request_normalized": normalized_request,
        "response_normalized": response_normalized,
        "candidates": candidates,
        "candidate_count": len(candidates),
        "confidence": _confidence(status, candidates),
        "error": error,
        "trace_replay_key": _stable_hash(
            {
                "domain": domain,
                "fixture_id": fixture_id,
                "case_id": case_id,
                "request": normalized_request,
                "response": response_normalized,
            }
        ),
    }
    if raw_response is not None:
        result["raw_response"] = raw_response
    return result


def _normalized_request(query: dict) -> dict:
    return {k: v for k, v in query.items() if k not in CONTROL_ARG_KEYS}


def _confidence(status: str, candidates: list[dict]) -> float:
    if status in ERROR_STATUSES or status == "not_found":
        return 0.0
    if len(candidates) == 1:
        return 0.95
    if candidates:
        return 0.55
    return 0.0


def _matches_lookup(candidate: dict, args: dict) -> bool:
    """Loose deterministic matcher for local fixture lookups."""
    lookup_id = args.get("booking_id") or args.get("order_id") or args.get("trip_id") or args.get("transaction_id")
    if _norm(candidate.get("status")) == "rebooked" and not lookup_id:
        return False
    if lookup_id and not _candidate_has_id(candidate, lookup_id):
        return False

    date_arg = args.get("date")
    if date_arg and str(candidate.get("date") or "") != str(date_arg):
        return False

    amount_arg = args.get("amount")
    if amount_arg is not None:
        try:
            if abs(float(candidate.get("amount", 0)) - float(amount_arg)) > 1.0:
                return False
        except (TypeError, ValueError):
            return False

    hint = str(args.get("merchant_hint") or args.get("city") or args.get("route_hint") or "").lower()
    if hint and hint not in _text_blob(candidate).lower():
        return False

    booking_type = args.get("booking_type")
    if booking_type and booking_type != "unknown" and candidate.get("booking_type") != booking_type:
        return False
    return True


def _candidate_has_id(candidate: dict, lookup_id: Any) -> bool:
    wanted = _norm(lookup_id)
    id_values: list[Any] = []
    for key in (
        "booking_id",
        "current_booking_id",
        "original_booking_id",
        "trip_id",
        "transaction_id",
        "aliases",
    ):
        value = candidate.get(key)
        if isinstance(value, list):
            id_values.extend(value)
        else:
            id_values.append(value)
    return any(_norm(value) == wanted for value in id_values if value)


def _text_blob(value: Any) -> str:
    if isinstance(value, dict):
        return " ".join(_text_blob(v) for v in value.values())
    if isinstance(value, list):
        return " ".join(_text_blob(v) for v in value)
    return str(value or "")


def _norm(value: Any) -> str:
    return str(value or "").strip().lower()


def _stable_hash(payload: dict) -> str:
    data = json.dumps(payload, ensure_ascii=False, sort_keys=True, default=str)
    return hashlib.sha256(data.encode("utf-8")).hexdigest()[:16]
