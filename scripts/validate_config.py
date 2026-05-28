#!/usr/bin/env python3
"""Validate YAML and steering config files.

Run manually:
    python3 scripts/validate_config.py
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any

import yaml


REPO_ROOT = Path(__file__).resolve().parent.parent
CONFIG_DIR = REPO_ROOT / "config"
STEERING_DIR = CONFIG_DIR / "steering"
REQUIRED_FILES = [
    "policy.yaml",
    "approval_flow.yaml",
    "workflow.yaml",
    "expense_types.yaml",
    "city_mapping.yaml",
    "fx_rates.yaml",
]
REQUIRED_STEERING_FIELDS = {"id", "scenario", "expected_behavior", "wrong_behavior"}


def _load_yaml(path: Path, errors: list[str]) -> dict[str, Any]:
    try:
        data = yaml.safe_load(path.read_text(encoding="utf-8"))
    except yaml.YAMLError as exc:
        errors.append(f"YAML PARSE ERROR in {path.name}: {exc}")
        return {}
    if not isinstance(data, dict):
        errors.append(f"{path.name}: top-level YAML must be a mapping")
        return {}
    return data


def _validate_steering(errors: list[str]) -> None:
    if not STEERING_DIR.exists():
        errors.append(f"MISSING: {STEERING_DIR}")
        return

    for path in sorted(STEERING_DIR.glob("*.json")):
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except json.JSONDecodeError as exc:
            errors.append(f"JSON PARSE ERROR in {path.name}: {exc}")
            continue
        if not isinstance(data, list):
            errors.append(f"{path.name}: top-level JSON must be a list")
            continue
        for idx, item in enumerate(data):
            if not isinstance(item, dict):
                errors.append(f"{path.name}[{idx}]: example must be an object")
                continue
            missing = REQUIRED_STEERING_FIELDS - set(item)
            if missing:
                errors.append(f"{path.name}[{idx}]: missing fields {sorted(missing)}")


def main() -> int:
    errors: list[str] = []
    configs: dict[str, dict[str, Any]] = {}

    for fname in REQUIRED_FILES:
        fpath = CONFIG_DIR / fname
        if not fpath.exists():
            errors.append(f"MISSING: {fpath}")
            continue
        loaded = _load_yaml(fpath, errors)
        if loaded:
            configs[fname] = loaded

    policy = configs.get("policy.yaml", {})
    expected_levels = {"L1", "L2", "L3", "L4"}
    expected_tiers = {"tier_1", "tier_2", "tier_3"}

    if policy:
        levels = {lv.get("id") for lv in policy.get("employee_levels", [])}
        if levels != expected_levels:
            errors.append(f"policy.yaml employee_levels: expected {expected_levels}, got {levels}")

        tiers = set(policy.get("city_tiers", {}).keys())
        if not expected_tiers.issubset(tiers):
            errors.append(f"policy.yaml city_tiers: missing {expected_tiers - tiers}")

        for limit_key, tier_map in policy.get("limits", {}).items():
            if not isinstance(tier_map, dict):
                errors.append(f"policy.yaml limits.{limit_key}: must be a mapping")
                continue
            for tier in expected_tiers:
                if tier not in tier_map:
                    errors.append(f"policy.yaml limits.{limit_key}: missing {tier}")
                    continue
                level_map = tier_map.get(tier) or {}
                missing_levels = expected_levels - set(level_map)
                if missing_levels:
                    errors.append(
                        f"policy.yaml limits.{limit_key}.{tier}: missing levels {missing_levels}"
                    )

        trust = policy.get("field_source_trust")
        if trust is not None:
            for key in ("api", "user_typed", "ocr", "unknown"):
                if key not in trust:
                    errors.append(f"policy.yaml field_source_trust: missing {key}")
                elif not isinstance(trust[key], (int, float)):
                    errors.append(f"policy.yaml field_source_trust.{key}: must be numeric")

    workflow = configs.get("workflow.yaml", {})
    known_skills = {"receipt_validation", "approval", "compliance", "voucher", "payment"}
    valid_actions = {"reject", "warn", "skip", "alert", "retry"}
    for step in workflow.get("pipeline", []):
        skill = step.get("skill")
        if skill not in known_skills:
            errors.append(f"workflow.yaml: unknown skill '{skill}'")
        fail_action = step.get("fail_action")
        if fail_action and fail_action not in valid_actions:
            errors.append(f"workflow.yaml skill={skill}: invalid fail_action '{fail_action}'")

    expense_types = configs.get("expense_types.yaml", {})
    policy_limit_keys = set(policy.get("limits", {}).keys()) if policy else set()
    for cat_key, cat_val in expense_types.get("expense_types", {}).items():
        for sub in cat_val.get("subtypes", []):
            limit_key = sub.get("limit_key")
            if limit_key and limit_key not in policy_limit_keys:
                errors.append(
                    f"expense_types.yaml {cat_key}.{sub.get('id')}: "
                    f"limit_key '{limit_key}' not found in policy.yaml limits"
                )

    approval = configs.get("approval_flow.yaml", {})
    if approval and not isinstance(approval.get("approval_matrix", {}), dict):
        errors.append("approval_flow.yaml: approval_matrix must be a dict")

    city_mapping = configs.get("city_mapping.yaml", {})
    if city_mapping and policy:
        all_tier_cities: set[str] = set()
        for tier_data in policy.get("city_tiers", {}).values():
            all_tier_cities.update(str(city) for city in tier_data.get("cities", []))
        if "*" not in all_tier_cities:
            for alias, canonical in city_mapping.get("aliases", {}).items():
                if canonical not in all_tier_cities:
                    errors.append(
                        f"city_mapping.yaml: alias '{alias}' -> '{canonical}', "
                        f"but '{canonical}' is not in any city_tier"
                    )

    _validate_steering(errors)

    if errors:
        print(f"CONFIG VALIDATION FAILED - {len(errors)} error(s):")
        for error in errors:
            print(f"  ERROR: {error}")
        return 1

    print(f"CONFIG VALIDATION PASSED - {len(REQUIRED_FILES)} YAML files checked.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
