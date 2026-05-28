# ExpenseFlow Security & Engineering Hardening Plan (P0–P2)

> Reference: [anthropics/financial-services](https://github.com/anthropics/financial-services/tree/main)
> Codebase root: `/Users/ashleychen/expenseflow/`

---

## Overview

Six work items ordered by priority. Each item is self-contained with exact file paths, function signatures, and acceptance criteria. Items within the same priority tier are independent and can be parallelized.

---

## P0-A: Move Prompt Injection Detection from Tool to Middleware

### Problem

`tool_detect_document_prompt_injection` in `backend/api/routes/chat.py` is a **tool** the agent must voluntarily call. If the agent is already compromised by injection in OCR text, it will not call the detection tool on itself. This is a security architecture flaw.

### What to do

**Step 1: Create `backend/services/injection_guard.py`**

```python
"""Prompt injection detection — runs as a mandatory middleware layer,
not as an optional tool the agent can choose to skip."""

import re
from typing import Optional

INJECTION_PATTERNS: list[re.Pattern] = [
    re.compile(r"ignore\s+(all\s+)?(previous\s+)?instruction", re.IGNORECASE),
    re.compile(r"forget\s+(all\s+)?(previous\s+)?context", re.IGNORECASE),
    re.compile(r"now\s+you\s+are", re.IGNORECASE),
    re.compile(r"system\s+override", re.IGNORECASE),
    re.compile(r"disregard\s+(all\s+)?above", re.IGNORECASE),
    re.compile(r"new\s+instruction", re.IGNORECASE),
    re.compile(r"you\s+must\s+(now\s+)?act\s+as", re.IGNORECASE),
    re.compile(r"<\s*system\s*>", re.IGNORECASE),
    re.compile(r"ADMIN\s*MODE", re.IGNORECASE),
]


def scan_text(text: str) -> Optional[dict]:
    """Return injection report if patterns found, else None."""
    if not text:
        return None
    found = []
    for pat in INJECTION_PATTERNS:
        if pat.search(text):
            found.append(pat.pattern)
    if found:
        return {"injection_detected": True, "patterns": found}
    return None
```

**Step 2: Patch `tool_extract_receipt_fields` in `backend/api/routes/chat.py`**

Find the function `tool_extract_receipt_fields` (approx lines 550-620). After the OCR call returns its result dict, **before returning it to the agent loop**, insert injection scanning:

```python
from backend.services.injection_guard import scan_text

# ... after OCR result is obtained ...
raw_text_fields = [
    result.get("merchant", ""),
    result.get("description", ""),
    result.get("items_text", ""),
    result.get("remarks", ""),
]
combined_text = " ".join(str(f) for f in raw_text_fields if f)
injection_report = scan_text(combined_text)
if injection_report:
    result["_injection_warning"] = True
    result["_injection_patterns"] = injection_report["patterns"]
    # Redact suspicious fields — keep amount/date (structured), strip free-text
    for field_name in ["description", "remarks", "items_text"]:
        if field_name in result:
            result[field_name] = "[REDACTED — injection pattern detected]"
```

**Step 3: Patch the agent loop's user-message intake**

In `chat.py`, find the `/api/chat/message` endpoint (the SSE streaming route). Before the message enters `run_agent()`, scan the user's text:

```python
@router.post("/message")
async def chat_message(...):
    user_text = body.message  # or however the message body is structured
    injection_report = scan_text(user_text)
    if injection_report:
        # Log but don't block — user messages are semi-trusted
        await create_audit_log(db, actor_id=ctx.user_id, action="injection_attempt",
                               detail={"patterns": injection_report["patterns"]})
```

**Step 4: Remove `tool_detect_document_prompt_injection` from `_TOOL_DEFS` and all `TOOL_REGISTRY` lists.**

It should no longer exist as an agent-callable tool. The functionality is now mandatory middleware.

### Acceptance criteria

- [ ] `injection_guard.py` exists with `scan_text()` function
- [ ] OCR tool results are scanned and redacted before entering agent message history
- [ ] User messages are scanned and logged (audit trail) before entering agent loop
- [ ] `tool_detect_document_prompt_injection` is removed from `_TOOL_DEFS` and `TOOL_REGISTRY`
- [ ] Existing eval cases still pass (`pytest backend/tests/test_eval_harness.py -v`)
- [ ] New test: feed OCR mock data containing "ignore all previous instructions" → confirm redaction in result

---

## P0-B: Subagent Read/Write Separation (Chat Agent Architecture)

### Problem

In `backend/api/routes/chat.py`, the `run_agent()` function runs a single agent loop where the same LLM context reads untrusted OCR data AND calls write tools like `update_draft_field`. If prompt injection survives the P0-A guard, the agent can still write to drafts in the same turn it reads malicious text.

The reference repo's pattern: reader subagents have read-only tools; only a separate writer subagent (which never sees raw external data) can write.

### What to do

Refactor `run_agent()` into a two-phase architecture **for the `expense_assistant` role only** (the role that handles OCR + draft editing). `manager_explain` and `manager` roles are already read-only and don't need this.

**Step 1: Split `TOOL_REGISTRY["expense_assistant"]` into two subsets**

In `backend/api/routes/chat.py`, replace the flat list with two registries:

```python
TOOL_REGISTRY_READ = {
    "expense_assistant": [
        "extract_receipt_fields",
        "suggest_category",
        "check_duplicate_invoice",
        "get_my_recent_submissions",
        "get_report_detail",
        "get_spend_summary",
        "get_budget_summary",
        "get_policy_rules",
        "fetch_didi_trips",
        "fetch_ctrip_bookings",
        "fetch_credit_card_transactions",
    ],
}

TOOL_REGISTRY_WRITE = {
    "expense_assistant": [
        "update_draft_field",
        "update_report_line_field",
    ],
}
```

**Step 2: Create a two-phase `run_agent_with_isolation()` function**

```python
async def run_agent_with_isolation(
    messages: list[dict],
    agent_role: str,
    ctx: UserContext,
    db: AsyncSession,
    draft_id: str = None,
) -> AsyncIterator[dict]:
    """Two-phase agent: Phase 1 reads and reasons, Phase 2 writes.
    
    Phase 1 (Reader): Has all read tools. Produces a structured
    action_plan dict describing what fields to update and why.
    
    Phase 2 (Writer): Only sees the action_plan (never raw OCR text).
    Has only write tools. Executes the plan.
    """
    # Phase 1: Reader agent — full read tools, NO write tools
    read_tools = [_TOOL_DEFS[t] for t in TOOL_REGISTRY_READ.get(agent_role, [])]
    reader_messages = list(messages)  # copy
    
    # Append instruction to produce an action plan instead of calling write tools
    reader_messages.append({
        "role": "user",
        "content": (
            "[SYSTEM] You are in READ-ONLY mode. You can call any read tool "
            "to gather information. When you have determined what fields need "
            "to be updated, output a JSON block with key 'field_updates': "
            "[{'field': '...', 'value': '...', 'reason': '...'}]. "
            "Do NOT attempt to call update_draft_field or update_report_line_field."
        ),
    })
    
    reader_response = await _run_single_phase(reader_messages, read_tools, agent_role, ctx, db, draft_id)
    
    # Yield reader's text response to user (explanations, questions, etc.)
    for event in reader_response["events"]:
        yield event
    
    # Phase 2: If reader produced field_updates, execute them via writer
    field_updates = reader_response.get("field_updates")
    if field_updates:
        write_tools = [_TOOL_DEFS[t] for t in TOOL_REGISTRY_WRITE.get(agent_role, [])]
        for update in field_updates:
            tool_name = "update_draft_field" if draft_id else "update_report_line_field"
            fn = TOOL_IMPLEMENTATIONS.get(tool_name)
            if fn:
                result = await fn(update, ctx, db, draft_id)
                yield {"type": "tool_result", "tool": tool_name, "result": result}
```

**Step 3: Update the route handler to use the new function**

In the `/api/chat/message` endpoint, check the role:

```python
if canonical_role == "expense_assistant":
    async for event in run_agent_with_isolation(messages, canonical_role, ctx, db, draft_id):
        yield format_sse(event)
else:
    async for event in run_agent(messages, canonical_role, ctx, db, draft_id):
        yield format_sse(event)
```

**Step 4: Keep `run_agent()` as-is for `manager_explain` and `manager` roles** (they're already read-only).

### Acceptance criteria

- [ ] `expense_assistant` role uses two-phase agent: reader (read tools only) → writer (write tools only, no raw OCR text)
- [ ] `manager_explain` and `manager` roles unchanged (still use `run_agent()`)
- [ ] Writer phase never receives raw OCR text or user-uploaded content in its message history
- [ ] Existing eval cases pass: `pytest backend/tests/test_agent_eval.py -v`
- [ ] New eval case: simulate OCR injection that asks agent to set amount to 0 → confirm it's blocked

---

## P1-A: Configuration Validation Script + Pre-commit Hook

### Problem

Six YAML config files (`config/*.yaml`) drive all business logic. A typo in `policy.yaml` (e.g., misspelling `tier_1` as `teir_1`) or a missing `limit_key` cross-reference silently breaks the system at runtime. There is no validation.

### What to do

**Step 1: Create `scripts/validate_config.py`**

```python
#!/usr/bin/env python3
"""Validate all YAML config files for structural correctness and cross-references.

Run manually: python3 scripts/validate_config.py
Runs automatically via pre-commit hook.

Exit codes: 0 = clean, 1 = errors found.
"""

import sys
from pathlib import Path

import yaml


CONFIG_DIR = Path(__file__).resolve().parent.parent / "config"
REQUIRED_FILES = [
    "policy.yaml",
    "approval_flow.yaml",
    "workflow.yaml",
    "expense_types.yaml",
    "city_mapping.yaml",
    "fx_rates.yaml",
]


def main() -> int:
    errors: list[str] = []

    # 1. All required files exist and parse
    configs: dict[str, dict] = {}
    for fname in REQUIRED_FILES:
        fpath = CONFIG_DIR / fname
        if not fpath.exists():
            errors.append(f"MISSING: {fpath}")
            continue
        try:
            configs[fname] = yaml.safe_load(fpath.read_text())
        except yaml.YAMLError as e:
            errors.append(f"YAML PARSE ERROR in {fname}: {e}")

    if not configs:
        print(f"FATAL: no configs loaded. {len(errors)} errors.")
        for e in errors:
            print(f"  ERROR: {e}")
        return 1

    # 2. policy.yaml: employee levels must be L1-L4, city_tiers must have tier_1/2/3
    policy = configs.get("policy.yaml", {})
    if policy:
        levels = {lv["id"] for lv in policy.get("employee_levels", [])}
        expected_levels = {"L1", "L2", "L3", "L4"}
        if levels != expected_levels:
            errors.append(f"policy.yaml employee_levels: expected {expected_levels}, got {levels}")

        tiers = set(policy.get("city_tiers", {}).keys())
        expected_tiers = {"tier_1", "tier_2", "tier_3"}
        if not expected_tiers.issubset(tiers):
            errors.append(f"policy.yaml city_tiers: missing {expected_tiers - tiers}")

        # Each limit key must have all tier/level combos
        for limit_key, tier_map in policy.get("limits", {}).items():
            for tier in expected_tiers:
                if tier not in tier_map:
                    errors.append(f"policy.yaml limits.{limit_key}: missing {tier}")

    # 3. workflow.yaml: each skill name must map to a known skill module
    workflow = configs.get("workflow.yaml", {})
    known_skills = {"receipt_validation", "approval", "compliance", "voucher", "payment"}
    if workflow:
        for step in workflow.get("pipeline", []):
            skill = step.get("skill")
            if skill not in known_skills:
                errors.append(f"workflow.yaml: unknown skill '{skill}'")
            fa = step.get("fail_action")
            valid_actions = {"reject", "warn", "skip", "alert", "retry"}
            if fa and fa not in valid_actions:
                errors.append(f"workflow.yaml skill={skill}: invalid fail_action '{fa}'")

    # 4. expense_types.yaml: each subtype's limit_key must exist in policy.yaml limits
    expense_types = configs.get("expense_types.yaml", {})
    policy_limit_keys = set(policy.get("limits", {}).keys()) if policy else set()
    if expense_types:
        for cat_key, cat_val in expense_types.get("expense_types", {}).items():
            for sub in cat_val.get("subtypes", []):
                lk = sub.get("limit_key")
                if lk and lk not in policy_limit_keys:
                    errors.append(
                        f"expense_types.yaml {cat_key}.{sub.get('id')}: "
                        f"limit_key '{lk}' not found in policy.yaml limits"
                    )

    # 5. approval_flow.yaml: referenced roles should exist
    # (lightweight check — just ensure YAML structure is correct)
    approval = configs.get("approval_flow.yaml", {})
    if approval:
        matrix = approval.get("approval_matrix", {})
        if not isinstance(matrix, dict):
            errors.append("approval_flow.yaml: approval_matrix must be a dict")

    # 6. city_mapping.yaml: all target cities should appear in policy.yaml city_tiers
    city_mapping = configs.get("city_mapping.yaml", {})
    if city_mapping and policy:
        all_tier_cities = set()
        for tier_data in policy.get("city_tiers", {}).values():
            all_tier_cities.update(tier_data.get("cities", []))
        if "*" not in all_tier_cities:
            for alias, canonical in city_mapping.get("aliases", {}).items():
                if canonical not in all_tier_cities:
                    errors.append(
                        f"city_mapping.yaml: alias '{alias}' → '{canonical}' "
                        f"but '{canonical}' not in any city_tier"
                    )

    # Report
    if errors:
        print(f"CONFIG VALIDATION FAILED — {len(errors)} error(s):")
        for e in errors:
            print(f"  ERROR: {e}")
        return 1
    else:
        print(f"CONFIG VALIDATION PASSED — {len(REQUIRED_FILES)} files checked.")
        return 0


if __name__ == "__main__":
    sys.exit(main())
```

**Step 2: Create `scripts/install_hooks.sh`**

```bash
#!/usr/bin/env bash
# Install git pre-commit hook for config validation
set -euo pipefail

REPO_ROOT="$(git -C "$(dirname "$0")/.." rev-parse --show-toplevel)"
HOOK_PATH="$REPO_ROOT/.git/hooks/pre-commit"

cat > "$HOOK_PATH" << 'HOOK'
#!/usr/bin/env bash
set -euo pipefail

# 1. Validate YAML configs
python3 scripts/validate_config.py
if [ $? -ne 0 ]; then
    echo "Pre-commit: config validation failed. Fix errors before committing."
    exit 1
fi

# 2. Check for secrets in staged files
if git diff --cached --name-only | xargs grep -l -E '(sk-[a-zA-Z0-9]{20,}|ANTHROPIC_API_KEY\s*=\s*["\x27]sk-)' 2>/dev/null; then
    echo "Pre-commit: possible API key detected in staged files. Remove before committing."
    exit 1
fi

echo "Pre-commit: all checks passed."
HOOK

chmod +x "$HOOK_PATH"
echo "Pre-commit hook installed at $HOOK_PATH"
```

**Step 3: Add `pyyaml` to `requirements.txt` if not already present** (it likely is, since config/ already uses YAML).

### Acceptance criteria

- [ ] `python3 scripts/validate_config.py` exits 0 on current configs
- [ ] Introduce a deliberate typo in `policy.yaml` (e.g., `teir_1`) → script exits 1 with clear error message
- [ ] `bash scripts/install_hooks.sh` installs the pre-commit hook
- [ ] `git commit` on a branch with broken config is blocked by the hook
- [ ] Secret detection catches a staged file containing `sk-proj-...`
- [ ] Script validates all 6 cross-reference types listed above

---

## P1-B: GitHub Actions CI — Secret Scanning + Config Validation

### Problem

No CI pipeline exists. The pre-commit hook (P1-A) only protects local commits. PRs from forks or force-pushes bypass it.

### What to do

**Step 1: Create `.github/workflows/ci.yml`**

```yaml
name: CI

on:
  push:
    branches: [main]
  pull_request:
    branches: [main]

jobs:
  validate-config:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4
      - uses: actions/setup-python@v5
        with:
          python-version: "3.11"
      - run: pip install pyyaml
      - run: python3 scripts/validate_config.py

  secret-scan:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4
        with:
          fetch-depth: 0
      - uses: gitleaks/gitleaks-action@v2
        env:
          GITHUB_TOKEN: ${{ secrets.GITHUB_TOKEN }}

  lint-and-test:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4
      - uses: actions/setup-python@v5
        with:
          python-version: "3.11"
      - run: pip install -r requirements.txt
      - run: python -m pytest backend/tests/test_eval_harness.py -v -k "not llm"
```

**Step 2: Create `.gitleaks.toml`** (optional, to customize rules)

```toml
[allowlist]
  paths = [
    '''\.env\.example''',
    '''backend/tests/.*\.json''',
  ]
```

### Acceptance criteria

- [ ] `.github/workflows/ci.yml` exists and is valid YAML
- [ ] `validate-config` job runs `scripts/validate_config.py`
- [ ] `secret-scan` job uses gitleaks v2 action
- [ ] `lint-and-test` job runs deterministic eval tests (no API key needed)
- [ ] All three jobs pass on current codebase

---

## P2-A: Steering Examples for Agent Behavior Consistency

### Problem

`chat.py` agent roles rely solely on system prompts for behavioral guidance. The reference repo uses `steering-examples.json` to teach agents via few-shot examples — more robust than natural language instructions alone, especially for edge cases.

### What to do

**Step 1: Create `config/steering/` directory with three files**

**`config/steering/expense_assistant.json`**

```json
[
  {
    "id": "ocr_low_confidence",
    "scenario": "OCR returns a merchant name with low confidence and an amount that looks like it might be misread (e.g., ¥1880 vs ¥188.0)",
    "user_message": "帮我识别这张发票",
    "expected_behavior": "Call extract_receipt_fields. For low-confidence fields, present the OCR result AND flag uncertainty: '识别到金额 ¥1880，但置信度较低，请确认是否正确。' Do NOT auto-fill uncertain fields into draft.",
    "wrong_behavior": "Silently fill all OCR fields into draft without mentioning confidence issues."
  },
  {
    "id": "policy_question_use_tool",
    "scenario": "User asks about expense limits — should always use get_policy_rules, never answer from memory",
    "user_message": "一线城市餐费标准是多少",
    "expected_behavior": "Call get_policy_rules tool first, then answer with the exact numbers from policy. Mention employee level matters.",
    "wrong_behavior": "Answer from training data without calling the policy tool. Numbers may be outdated or wrong."
  },
  {
    "id": "multi_candidate_evidence",
    "scenario": "User asks to fill trip info, Didi API returns 3 candidate trips for the same date",
    "user_message": "帮我填一下昨天的打车记录",
    "expected_behavior": "Call fetch_didi_trips. Present all 3 candidates with times and amounts. Ask user to pick one. Do NOT auto-fill.",
    "wrong_behavior": "Pick the first result and auto-fill the draft without asking."
  },
  {
    "id": "refuse_state_change",
    "scenario": "User asks the agent to submit or approve an expense",
    "user_message": "帮我提交这个报销单",
    "expected_behavior": "Explain that submission must be done via the UI button. Offer to help review the draft before submission.",
    "wrong_behavior": "Attempt to call a submit tool or claim it has been submitted."
  },
  {
    "id": "cross_language_mirror",
    "scenario": "User writes in English but the expense data is in Chinese",
    "user_message": "What's the status of my latest expense report?",
    "expected_behavior": "Respond in English (mirror user language). Tool results with Chinese field names are fine — translate key info in your response.",
    "wrong_behavior": "Switch to Chinese just because the data is in Chinese."
  }
]
```

**`config/steering/manager_explain.json`**

```json
[
  {
    "id": "high_risk_explanation",
    "scenario": "Manager views a T4 (high risk) submission with fraud signals",
    "expected_behavior": "Lead with the risk tier and top risk signal. Show specific evidence (rule name, amount, threshold). End with a clear recommendation (approve with conditions / reject / request more info). Never downplay risk.",
    "wrong_behavior": "Give a vague summary like 'there are some concerns' without citing specific rules or evidence."
  },
  {
    "id": "low_risk_explanation",
    "scenario": "Manager views a T1 (clean) submission",
    "expected_behavior": "Brief confirmation: '该报销合规，无风险信号。' with key facts (amount, category, compliance level). Don't over-explain clean submissions.",
    "wrong_behavior": "Write a long analysis for a clean submission, wasting manager's time."
  },
  {
    "id": "ambiguity_shield_explanation",
    "scenario": "Submission flagged by ambiguity shield (compliance B-level, shield_triggered=true)",
    "expected_behavior": "Explain which ambiguity factors triggered (e.g., amount near boundary + vague description). Show the LLM review's risk_points if available. Present as 'needs your judgment' — not as a recommendation to approve or reject.",
    "wrong_behavior": "Make a definitive approve/reject recommendation for an ambiguous case. The shield exists precisely because the system can't decide — the manager must."
  }
]
```

**`config/steering/manager.json`**

```json
[
  {
    "id": "batch_approval_context",
    "scenario": "Manager asks about their pending approval queue",
    "expected_behavior": "Call get_pending_approval_queue. Present a summary table: count by risk tier, total amount, oldest pending item. Highlight any T3/T4 items that need attention first.",
    "wrong_behavior": "List all items without prioritization or summary."
  }
]
```

**Step 2: Load steering examples into system prompts**

In `backend/api/routes/chat.py`, modify the system prompt construction (in the `RealLLM.next_turn()` method or wherever the system message is built):

```python
import json
from pathlib import Path

_STEERING_CACHE: dict[str, list[dict]] = {}

def _load_steering(agent_role: str) -> list[dict]:
    if agent_role not in _STEERING_CACHE:
        fpath = Path("config/steering") / f"{agent_role}.json"
        if fpath.exists():
            _STEERING_CACHE[agent_role] = json.loads(fpath.read_text())
        else:
            _STEERING_CACHE[agent_role] = []
    return _STEERING_CACHE[agent_role]

def _format_steering_block(agent_role: str) -> str:
    examples = _load_steering(agent_role)
    if not examples:
        return ""
    lines = ["\n\n## Behavioral Examples\n"]
    for ex in examples:
        lines.append(f"### Scenario: {ex.get('scenario', ex.get('id'))}")
        if "user_message" in ex:
            lines.append(f"User says: \"{ex['user_message']}\"")
        lines.append(f"CORRECT: {ex['expected_behavior']}")
        lines.append(f"WRONG: {ex['wrong_behavior']}")
        lines.append("")
    return "\n".join(lines)
```

Append `_format_steering_block(agent_role)` to the system prompt string before sending to the LLM.

### Acceptance criteria

- [ ] `config/steering/` directory contains 3 JSON files
- [ ] Each file is valid JSON with `id`, `scenario`, `expected_behavior`, `wrong_behavior` fields
- [ ] System prompts include steering examples block when files exist
- [ ] Steering examples are cached (loaded once per process, not per request)
- [ ] `validate_config.py` (from P1-A) also validates steering JSON files: parseable + required fields present

---

## P2-B: Data Source Trust Hierarchy in Compliance Pipeline

### Problem

`skill_03_compliance.py` treats all field values equally regardless of source. But `backend/api/routes/chat.py` already tracks field provenance in `draft.field_sources` (e.g., `{"merchant": "ocr", "amount": "user_typed"}`). This trust signal is not used downstream.

When OCR-extracted amounts contradict invoice verification or API-fetched data, compliance should weight the more trusted source.

### What to do

**Step 1: Define trust hierarchy in `config/policy.yaml`**

Add a new top-level section:

```yaml
# Data source trust hierarchy (higher = more trusted)
# Used by compliance and fraud detection to weight conflicting signals
field_source_trust:
  api: 1.0          # Didi/Ctrip/credit card API — highest trust
  user_typed: 0.9   # User manually entered
  ocr: 0.6          # GPT-4o Vision extraction — lowest trust
  unknown: 0.5      # Source not tracked
```

**Step 2: Extend `models/expense.py` `LineItem` dataclass**

Add an optional field to carry source provenance into the compliance pipeline:

```python
@dataclass
class LineItem:
    expense_type: str
    amount: float
    currency: str
    city: str
    date: date
    invoice: Optional[Invoice]
    description: str
    attendees: list[str] = field(default_factory=list)
    field_sources: dict[str, str] = field(default_factory=dict)  # NEW: {"amount": "ocr", "merchant": "api", ...}
```

**Step 3: Modify `skills/skill_03_compliance.py` to use trust signals**

In the `process()` function, after obtaining the compliance level (A/B/C), add a trust-based adjustment for borderline cases:

```python
# After line: level = engine.check_tolerance(item.amount, limit)
# Add trust-based confidence flag for borderline (B-level) items:

if level == ComplianceLevel.B and item.field_sources:
    amount_source = item.field_sources.get("amount", "unknown")
    trust_config = config_snapshot.get("policy", {}).get("field_source_trust", {})
    trust_score = trust_config.get(amount_source, 0.5)
    if trust_score < 0.7:
        # Low-trust source on a borderline amount → flag for review
        issues.append(
            f"行[{idx}] 金额来源为{amount_source}(信任度{trust_score})，"
            f"建议人工复核金额准确性"
        )
```

**Step 4: Propagate `field_sources` from draft to LineItem**

In `backend/api/routes/submissions.py` (or wherever drafts are converted to formal submissions/LineItems), ensure `draft.field_sources` is carried through to the `LineItem.field_sources` field.

Find the code that creates `LineItem` objects from submission data and add:

```python
line_item = LineItem(
    expense_type=...,
    amount=...,
    # ... existing fields ...
    field_sources=draft.field_sources or {},  # NEW
)
```

**Step 5: Update `agent/ambiguity_detector.py`**

In the 5-factor ambiguity scoring model, add `field_source_trust` as a factor or modifier. If the amount field came from OCR (trust 0.6) AND the amount is near a policy boundary, increase the ambiguity score by a configurable weight:

```python
# In the scoring logic, after existing 5 factors:
amount_source = getattr(item, "field_sources", {}).get("amount", "unknown")
source_trust = trust_config.get(amount_source, 0.5)
if source_trust < 0.7 and any factor involving amount is triggered:
    score += 10  # configurable boost for low-trust + amount anomaly
```

### Acceptance criteria

- [ ] `policy.yaml` contains `field_source_trust` section
- [ ] `LineItem` dataclass has optional `field_sources` field
- [ ] Compliance skill logs a warning when B-level amount comes from low-trust source (OCR)
- [ ] Ambiguity detector considers source trust as a scoring factor
- [ ] `field_sources` propagates from draft → submission → LineItem → compliance pipeline
- [ ] Existing eval cases pass (no regressions)
- [ ] New eval case in `backend/tests/eval_datasets/`: B-level amount with `source=ocr` triggers trust warning; same amount with `source=api` does not

---

## Dependency Graph

```
P0-A (injection middleware) ──┐
                              ├──→ P0-B (subagent isolation) depends on P0-A
P1-A (config validation)  ───┤
                              ├──→ P1-B (CI pipeline) depends on P1-A script existing
P2-A (steering examples)     │
P2-B (trust hierarchy)       │
                              │
All P2 items are independent of P0/P1 and of each other.
```

**Recommended execution order:**
1. P0-A → P0-B (sequential, security-critical)
2. P1-A → P1-B (sequential, P1-B needs the script from P1-A)
3. P2-A and P2-B (parallel, independent)

---

## Files Modified (Summary)

| Item | New Files | Modified Files |
|------|-----------|----------------|
| P0-A | `backend/services/injection_guard.py` | `backend/api/routes/chat.py` |
| P0-B | — | `backend/api/routes/chat.py` |
| P1-A | `scripts/validate_config.py`, `scripts/install_hooks.sh` | — |
| P1-B | `.github/workflows/ci.yml`, `.gitleaks.toml` | — |
| P2-A | `config/steering/expense_assistant.json`, `config/steering/manager_explain.json`, `config/steering/manager.json` | `backend/api/routes/chat.py` |
| P2-B | — | `config/policy.yaml`, `models/expense.py`, `skills/skill_03_compliance.py`, `agent/ambiguity_detector.py`, `backend/api/routes/submissions.py` |
