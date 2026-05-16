**[EN](README.md)** | **[中文](README_CN.md)**

# ExpenseFlow

AI-powered enterprise expense management platform — end-to-end automation from receipt submission to payment, with a built-in 5-Skill compliance pipeline, conversational Agent assistant, and Eval framework.

> Core design principles: distinguish Workflow from Agent, audience-layered presentation, tool whitelist as prompt-injection defense.
> Reference: [Anthropic *Building Effective Agents*](https://www.anthropic.com/research/building-effective-agents) (2024).

---

## Table of Contents

- [What This Project Does](#what-this-project-does)
- [System Architecture](#system-architecture)
- [Architecture Highlights — What Makes This Different](#architecture-highlights--what-makes-this-different)
- [5-Skill Compliance Pipeline](#5-skill-compliance-pipeline)
- [Conversational Agent](#conversational-agent)
- [Design Decisions](#design-decisions)
- [Approval & Budget Workflow](#approval--budget-workflow)
- [Role-Based Access Control (RBAC)](#role-based-access-control-rbac)
- [Eval Platform](#eval-platform)
- [API Overview](#api-overview)
- [5 Things We Deliberately Don't Do](#5-things-we-deliberately-dont-do)
- [Project Structure](#project-structure)
- [Quick Start](#quick-start)
- [Running Tests](#running-tests)
- [Tech Stack](#tech-stack)

---

## What This Project Does

ExpenseFlow simulates a complete enterprise reimbursement system: employee uploads receipt → AI auto-review (OCR, rules engine, ambiguity detection) → manager approval (with AI decision explanation) → finance review → voucher generation → payment execution.

**Core Differentiators:**

1. **5-Skill Compliance Pipeline** — Receipt validation, approval chain, compliance check (with AmbiguityDetector 5-factor scoring), voucher generation, payment execution — all configuration-driven
2. **Conversational Agent** — Employees complete reimbursements via natural language (OCR → category suggestion → dedup check → budget check); managers get AI explanation cards for approval decisions
3. **Eval Framework** — YAML-defined test cases covering Agent routing, risk tiering, RBAC permissions, and tool whitelist security

---

## System Architecture

```
Employee submits receipt
  |
  v
+--------------------------- FastAPI Backend ----------------------------+
|                                                                        |
|  +--------------+    +--------------------------------------+          |
|  |  Chat Agent  |    |     5-Skill Compliance Pipeline      |          |
|  |              |    |                                      |          |
|  | - Submit     |    |  Receipt -> Approval -> Compliance   |          |
|  | - Q&A        |    |            | AmbiguityDetector       |          |
|  | - Explain    |    |  Voucher -> Payment                  |          |
|  +------+-------+    +--------------+-----------------------+          |
|         |                           |                                  |
|  +------v---------------------------v--------------------------+       |
|  |                     Data Layer                              |       |
|  |  SQLAlchemy Async | Submissions | Drafts | Budgets          |       |
|  |  Employees | AuditLogs | CostCenterBudgets                  |       |
|  +-------------------------------------------------------------+       |
|                          |                                             |
|  +-----------------------v---------------------------------+           |
|  |                  YAML Config Layer                      |           |
|  |  policy | approval_flow | expense_types | workflow      |           |
|  |  city_mapping | fx_rates                                |           |
|  +---------------------------------------------------------+           |
+------------------------------------------------------------------------+
       |                                      |
  +----v------+                      +--------v--------+
  | Frontend  |                      | Eval Framework  |
  | Employee  |                      | YAML test cases |
  | Manager   |                      | Agent behavior  |
  | Finance   |                      | verification    |
  +-----------+                      +-----------------+
```

---

## Architecture Highlights — What Makes This Different

Most "AI for expense management" portfolio projects ship a wrapper around GPT-4o. This one was designed around the trade-offs that production fraud-detection systems actually face. Six choices distinguish it.

### 1. Hybrid fraud architecture: deterministic rules + OODA agent

- **Layer 1** — 20 deterministic fraud rules + 5-factor `AmbiguityDetector`. Runs on every submission in milliseconds. Each flag cites a specific `rule_id`, fully auditable.
- **Layer 2** — an OODA agent that triggers only on the ~10% of submissions where Layer 1 says something is up. Multi-round (max 4 rounds), LLM-driven tool selection, builds an evidence chain, emits a verdict (`clean` / `suspicious` / `fraud`) with confidence.

Pure rules can't catch unknown patterns. Pure agent costs ~$0.02 per submission and breaks determinism. Hybrid keeps both strengths — same shape Airwallex Spend AI / Stripe Radar / Concur Detect use.

[Full design: `docs/hybrid-fraud-architecture.md`](docs/hybrid-fraud-architecture.md)

### 2. Cohen's κ eval framework — not raw accuracy

Most AI-product portfolios claim "95% accurate" with no methodology. This one:

- Has **human-labeled ground-truth datasets** (`backend/tests/eval_datasets/*_human_labeled.yaml`) — every case ships with an explicit `human_label` + `labeler_note` explaining *why* it's labeled that way (Hamel's "specification gulf" made concrete)
- Measures **Cohen's κ** (inter-rater agreement) — accounts for chance agreement; raw accuracy is misleading when classes are imbalanced
- Asserts **κ ≥ 0.40** in CI; the test fails if drift is detected
- Surfaces **κ + confusion matrix on the dashboard** for human review (Review Quality tab)
- Placeholder cases SKIP, not fake-PASS — no false-confidence numbers

[Full framework: `docs/evals-reference.md`](docs/evals-reference.md)

### 3. Tool whitelist as security boundary (Concur Joule pattern)

The agent's write-tool set is the empty set — by construction. The dispatcher is one line:

```python
fn = INVESTIGATION_TOOLS.get(tool_name)
if fn is None:
    raise ValueError(f"unknown tool: {tool_name}")
```

Prompt injection can produce any string the LLM wants. None of those strings will ever map to a function that mutates submission state — because no such function exists in the registry to begin with. The tool registry holds 8 read-only functions. Security at the boundary, not at the prompt.

This is the same pattern Concur Joule and Expensify Concierge use: the LLM is allowed to be unreliable; the **edges** are not.

### 4. Honest workflow-vs-agent labeling

"Agentic AI" is overused. The [Design Decisions §1](#1-workflow-vs-agent-honest-labeling) table lists every "agent-like" component in the codebase and maps it to one of two real categories:

- **Workflow** (deterministic, predetermined steps) — most of the system, by design
- **Agent** (LLM makes a decision) — only where it's genuinely needed

Six rows total. Only one is a true multi-round agent (the fraud investigator). The rest are honestly labeled as workflows or single-round dispatchers. This avoids the common portfolio failure mode of calling everything "agentic" because it sounds impressive.

### 5. Cite-the-rule explainability

Every flag the system raises points to a specific `rule_id` with structured violation metadata: rule text, severity (`info` / `warn` / `error`), suggested fix, and evidence. When the AI says "this looks wrong", the manager sees **which rule** and **why** — not just a risk score.

The structured `violations[]` array lives in `audit_report` and renders on the AI explanation card as the "📋 触发规则" block. Auditors can trace every red flag back to a specific, citable rule.

[Implementation: `agent/violation_registry.py`](agent/violation_registry.py)

### 6. Honest scope — multi-entity is documented, not built

Most portfolio projects oversell. This one ships:

- **Single-entity production code** that works end-to-end
- **An architecture spec** for multi-entity ([`docs/multi-entity-design.md`](docs/multi-entity-design.md)) — 4-layer decoupling (Entity / Category / Mapping / Policy) — that's deliberately **not implemented** because no real customer needs it yet
- **Clear scope statement** in this README: "Configuration-driven within fixed schema" (not "zero code changes for any client")

The discipline of marking what's **NOT** done is a portfolio signal too. Most candidates over-claim; deferring with a written contract is more credible than half-built abstractions.

### 7. Industrial-readiness gaps documented before being closed

The 8 things that separate a portfolio demo from a production-grade SaaS — multi-tenancy, real ERP integration, real payment execution, SOC 2, operational maturity — are all **documented as a roadmap** rather than half-built. Each gap has effort estimate, decision points, who-to-talk-to, and dependency graph.

Plus a **customer segmentation** doc that picks which segment to target first (50-person SaaS / 5,000-person manufacturer / 500-person cross-border e-commerce) and an **integration design** doc with concrete API contracts for NetSuite + Stripe Issuing + the "Excel-as-bridge" alternative for ERPs without good APIs.

[Roadmap: `docs/industrial-readiness-roadmap.md`](docs/industrial-readiness-roadmap.md) · [Segmentation: `docs/customer-segmentation.md`](docs/customer-segmentation.md) · [Integration: `docs/integration-design.md`](docs/integration-design.md)

---

**Reading order for portfolio reviewers:**

1. **[`docs/case-study-hybrid-fraud.md`](docs/case-study-hybrid-fraud.md) — if you read one thing, read this (2-min, 1-page)**
2. This README's [Design Decisions](#design-decisions) section — taxonomy and principles
3. [`docs/hybrid-fraud-architecture.md`](docs/hybrid-fraud-architecture.md) — the Layer 1 + Layer 2 design story (10-min read)
4. [`docs/evals-reference.md`](docs/evals-reference.md) — eval discipline (Hamel framework adaptation)
5. [`docs/agent-eval-dimensions.md`](docs/agent-eval-dimensions.md) — 8 eval dimensions a vertical AI agent should cover (privacy / FP-FN split / cross-model / multilingual / ...)
6. [`docs/multi-entity-design.md`](docs/multi-entity-design.md) — what's deliberately deferred and why
7. [`docs/industrial-readiness-roadmap.md`](docs/industrial-readiness-roadmap.md) — what production-grade requires (8 gaps)
8. [`docs/customer-segmentation.md`](docs/customer-segmentation.md) — which segment to target first
9. [`docs/integration-design.md`](docs/integration-design.md) — concrete NetSuite + Stripe + Excel-bridge designs
10. [`docs/fapiaoforce-prd.md`](docs/fapiaoforce-prd.md) — Concur-style multi-group product spec for the China-enterprise vertical (the productization of Segment B)

---

## 5-Skill Compliance Pipeline

After each expense submission, the backend asynchronously executes 5 Skills, all orchestrated by `workflow.yaml`:

| Skill | Function | Key Capabilities |
|-------|----------|-----------------|
| **01 Receipt Validation** | Invoice format, header, dedup, date checks | Global unique invoice number constraint; city name normalization ("SH"/"Shanghai" → unified) |
| **02 Approval Chain** | Build approval chain by expense type x amount x employee level | Timeout escalation (24h remind → 48h escalate → 72h auto-escalate); level exemptions |
| **03 Compliance Check** | Per-line-item A/B/C compliance + AmbiguityDetector | 5-factor ambiguity scoring (description vagueness / amount boundary / pattern anomaly / time anomaly / city mismatch); score >50 triggers Claude deep analysis |
| **04 Voucher Generation** | Accounting entries, VAT splitting | Auto-split input tax for special VAT invoices; debit-credit balance validation |
| **05 Payment Execution** | 5-point pre-check + payment simulation | >=5000 bank transfer, <5000 petty cash |

**Shield Mechanism:** When Skill-03's ambiguity score triggers human review (30-70) or suggests rejection (>70), the pipeline stops and marks `PENDING_REVIEW`, awaiting manual intervention.

**Configuration-Driven (within fixed schema):** Skip approval by setting `workflow.yaml: approval.enabled: false`; change expense limits by editing numbers in `policy.yaml`; tune ambiguity-detector sensitivity via `eval_config.json` — all without code changes.

**Current scope: single-entity.** Per-entity GL chart, VAT, and approval chains are an architecture-level extension documented in [`docs/multi-entity-design.md`](docs/multi-entity-design.md). Schema-level customization (custom fields per expense type, per-entity overrides) is on the roadmap but not yet implemented. Honest limits, real extension points — see the design doc for the 4-layer decoupling that closes the gap.

### AmbiguityDetector — 5-Factor Scoring Model

| Factor | Weight | Trigger Condition |
|--------|--------|-------------------|
| Description vagueness | 25% | Description <10 chars or contains generic words ("other", "misc", "expense") |
| Amount boundary | 20% | Amount within 90%-110% of policy limit |
| Pattern anomaly | 25% | >=3 same-category submissions within 7 days, amounts within +/-15% |
| Time anomaly | 15% | Meal/transport expenses on weekends |
| City mismatch | 15% | City name unrecognized or inconsistent before/after normalization |

Score → Decision: `<30` auto-pass / `30-70` human review / `>70` suggest reject

When score >50, calls Claude API for deep semantic analysis (falls back to rule-based scoring if no API key configured).

---

## Conversational Agent

One unified drawer for every employee page, plus a dedicated submit-chat on the quick-add flow. Security is **not** enforced by role-based tool whitelists — it lives inside each write tool (data-level ACL) and in the simple rule that **AI has zero state-changing business actions**: no `submit_expense`, no `approve`, no `reject`, no `pay`.

| Role | Where | Available Tools |
|------|-------|----------------|
| **employee_submit** | `quick.html` inline chat (draft creation) | OCR extraction, category suggestion, invoice dedup, draft editing, budget query |
| **employee** | Shared drawer on every employee page (`/api/chat/message`) | Recent expenses, report detail, spend summary, budget summary, policy rules, **line-item edits** (owner + `status ∈ {open, needs_revision}` checked inside the tool) |
| **manager_explain** | Structured AI risk card on approval pages (`/api/chat/explain/{id}`) | View pending expenses, employee history → output risk assessment + approval recommendation |

**LLM Abstraction:** Defaults to MockLLM (deterministic state machine, no API Key needed, ideal for demo and testing). Set `OPENAI_API_KEY` + `AGENT_USE_REAL_LLM=1` to switch to GPT-4o.

---

## Design Decisions

### 0. Design Principles (aligned with Airwallex Spend AI / Concur Joule / Ramp Copilot)

ExpenseFlow follows three principles that mirror modern AI-native expense
platforms ([Airwallex Spend AI 2026](https://www.airwallex.com/blog/meet-your-finance-ai-agents-a-new-way-to-manage-bills-and-expenses), Concur Joule, Ramp Copilot):

1. **Automation-first** — repetitive, rules-based work (receipt OCR, policy
   checks, voucher generation, payment routing) should never consume human
   time. Implemented via the **5-Skill compliance pipeline**.

2. **Explainability** — every AI decision is **transparent, auditable, and
   traceable to a specific rule**. Implemented via:
   - **Cite the rule** — `audit_report.violations[]` lists each triggered
     rule_id (e.g. `policy.limit_exceeded`, `ambiguity.description_vague`)
     with plain-language text + suggested fix, rendered as a dedicated
     "📋 触发规则" block on the AI explanation card
   - **Phased timeline** — `audit_report.timeline` only ever reflects what
     has actually happened, not future-tense predictions
   - **Per-tool audit logs** — every write tool emits an audit_log entry
     citing which user / role / rule fired

3. **Human-in-the-loop** — AI **does not execute** state-changing actions
   that carry legal liability (submit / approve / reject / pay). Those are
   UI-only buttons. Implemented via the empty action whitelist on every
   chat agent (`TOOL_REGISTRY["employee"]` excludes them by construction).

The **AI Auto-Approval Funnel** KPI on the Eval Observatory dashboard
tracks the production result of these principles (e.g. *"71.8% of recent
expenses auto-approved by AI, 21.1% routed to human review, 7.0% suggested
reject"*) so we can tell whether tiering is actually doing useful work —
not just whether the eval suite passes.

### 1. Workflow vs Agent: Honest Labeling

> "Agentic AI" is overused. Here we explicitly label what's an agent and what's a workflow.

| Location | Implementation | Reality | Rationale |
|----------|---------------|---------|-----------|
| Employee submit: happy path (upload receipt → fill fields) | OCR → dup_check → suggest → write, linear pipeline | **Workflow** | Steps are predetermined, LLM doesn't need to decide |
| Employee submit: user edits fields ("change amount to 380") | Requires Real LLM to parse intent | **True Agent** | LLM must parse intent, dynamically select tools |
| Employee my-reports QA drawer | Keyword match → single tool → format | **Workflow** | Single tool call, no decision-making |
| 5-Skill compliance pipeline | 5 sequential steps, policy_engine hard rules | **Workflow (intentional)** | Compliance requires determinism; LLM must not alter the flow |
| Manager/Finance AI explanation card | Calls read-only tools, assembles risk assessment + recommendation | **Agent (lightweight)** | Must independently decide which tools to call for evidence gathering |
| Fraud investigator (Layer 2, on high-risk submissions) | OODA loop, 4 rounds max, LLM picks tools from a 8-tool registry, emits verdict | **True Agent (multi-round)** | First multi-round LLM-driven decision making in the project. See [`docs/hybrid-fraud-architecture.md`](docs/hybrid-fraud-architecture.md). |

### 2. Unified Agent + Data-Level ACL (Concur Joule / Expensify Concierge Pattern)

The shared drawer is a **single agent** — it does not switch behavior by page or role. Auth is enforced inside the tools, not at the routing layer:

1. **Hard ceiling: AI has no state-changing business actions.** Submit / approve / reject / pay are UI-only because they carry legal and compliance weight; they must be human-confirmed. The `employee` whitelist literally doesn't list them.
2. **Write tools self-validate.** `tool_update_report_line_field` re-checks ownership (`report.employee_id == ctx.user_id`), state (`status ∈ {open, needs_revision}`), and field (`∈ EDITABLE_FIELDS`) inside the handler. A prompt-injected LLM that passes a stranger's `line_id` or targets a `pending` report still gets rejected by the tool, not by the router.
3. **Dispatcher double-checks.** Even before the tool's own check, if the LLM hallucinates a tool outside the role's whitelist (e.g. tries to call `update_draft_field` from the unified `employee` role), dispatch is rejected up front.

```python
TOOL_REGISTRY = {
    # Specialized flow — quick.html's draft-filling pipeline.
    "employee_submit": ["extract_receipt_fields", "suggest_category",
                        "check_duplicate_invoice", "get_my_recent_submissions",
                        "update_draft_field", "check_budget_status"],
    # Unified drawer — shared across every employee page. Auth inside tools.
    "employee":        ["get_my_recent_submissions", "get_report_detail",
                        "get_spend_summary", "get_budget_summary",
                        "get_policy_rules",
                        "update_report_line_field"],   # owner + state check in the tool
    # Risk card on approval pages — single request, structured JSON.
    "manager_explain": ["get_submission_for_review",
                        "get_employee_submission_history"],
}
```

**Why unified, not one role per page?** Real users have dual identities — every mid-level manager is both an employee filing their own expenses and an approver for their team. Role-based segmentation would force them to "switch hats" or navigate to a specific page to get the right tool set. Data-level ACL handles the same-user-two-hats case naturally: the LLM picks the right data by intent, and the tool checks ownership on the specific object the user references. Industry precedent: SAP Joule, Expensify Concierge, Ramp Copilot, Navan Ava all use this pattern.

> The Agent never has `submit_expense` / `approve` tools. This is not a limitation — it's the boundary between helper and actor.

### 3. Phased Audit Timeline

**Problem**: The original implementation wrote all 5-skill results into `audit_report.timeline` at submission time, causing "voucher generated / payment executed" to appear in the AI explanation card before manager approval.

**Fix**:

```
After submission     timeline = [step0, step1, step2]          phase="submit"
After manager approval   timeline.append(step3: "voucher generated")   phase="manager_approved"
After finance approval   timeline.append(step4: "payment executed")    phase="finance_approved"
```

`audit_report.timeline` only ever reflects "what has already happened."

### 4. Audience Layering

The AI explanation card presents information in two layers:

| Layer | Who Sees It | Display Condition |
|-------|-------------|-------------------|
| Recommendation, flags, advisory | All users (manager/finance) | Always shown |
| Tool call details, agent_role | Developers / interviewers | `auth.isDev()` = true |

Activate dev mode: add `?dev=1` to the URL, or click the toolbar button in the navbar.

---

## Approval & Budget Workflow

### State Machine

```
processing → reviewed → manager_approved → finance_approved → exported
                              |                    |
                          rejected              rejected
```

### Budget Control

Each cost center has quarterly budgets, checked in real-time on submission:
- **info** (75%-95%): approaching budget warning
- **blocked** (>=95%): auto-blocked, requires finance unlock
- **over_budget** (>100%): over-budget alert

### Risk Tiers

| Tier | AI Recommendation | Risk Score | Meaning |
|------|-------------------|------------|---------|
| T1 | approve | <=25 | Invoice compliant, amount normal, description specific |
| T2 | approve | 25-50 | Low risk, minor attention items |
| T3 | review  | 50-75 | Needs manual check — high amount or vague description |
| T4 | reject  | >75 | High risk — abnormal amount / missing documentation |

---

## Role-Based Access Control (RBAC)

| Role | Capabilities |
|------|-------------|
| **employee** | Submit expenses, view own expenses, chat assistant |
| **manager** | Approve team expenses, view AI explanation cards |
| **finance_admin** | Finance approval, unlock budget blocks, export vouchers, bulk operations |
| **admin** | Employee management, policy configuration, audit logs, budget settings |

---

## Eval Platform

The eval system has three layers: **YAML test datasets** define what to test, the **Unified Eval Harness** runs them with code-based graders, and the **Eval Observatory** provides a web dashboard + REST API to browse results, compare runs, and manage prompts.

### Architecture

```
+-------------------+     pytest      +---------------------+     POST     +-------------------+
|  YAML Datasets    | ------------->  |  Unified Eval       | ----------> |  Observatory API  |
|                   |                 |  Harness             |             |  /api/eval/runs   |
|  fraud_llm_rules  |   per-case     |  test_eval_harness   |  run results|                   |
|  fraud_rules_det  |   graders      |  .py                 |  + metadata |  Stores to DB:    |
|  ambiguity_detect |  <-----------> |                      |             |  EvalRun, LLMTrace|
|  layer_decision   |   code_graders |  Pass rate summary   |             |                   |
|  category_classif |   .py          |  P/R/F1 per component|             |  Eval Dashboard   |
+-------------------+                +---------------------+             +-------------------+
```

### What Gets Evaluated (5 Components)

| Component | Dataset File | Cases | What It Tests |
|-----------|-------------|-------|---------------|
| **Deterministic Fraud Rules** | `fraud_rules_deterministic.yaml` | ~60 | 14 rule functions (duplicate_attendee, geo_conflict, threshold_proximity, weekend_frequency, round_amount, consecutive_invoices, merchant_category_mismatch, pre_resignation_rush, fx_arbitrage, collusion_pattern, vendor_frequency, seasonal_anomaly, ghost_employee, timestamp_conflict). Each case provides input submissions + expected signal (true/false) |
| **LLM Fraud Analyzer** | `fraud_llm_rules.yaml` | ~15 | GPT-4o-based semantic analysis (template detection, receipt contradiction, per-person amount reasonableness, vagueness scoring). Supports pass^k trials for non-deterministic outputs. Requires `OPENAI_API_KEY` |
| **Ambiguity Detector** | `ambiguity_detector.yaml` | ~12 | 5-factor scoring model: score range validation, triggered factor verification, recommendation check (auto_pass / human_review / suggest_reject) |
| **Layer Decision** | `layer_decision.yaml` | ~20 | Quick-submit layered routing: given OCR/classify/dedupe/budget signals, verify correct layer assignment (green/yellow/red) |
| **Category Classifier** | `category_classifier.yaml` | ~20 | Merchant name -> expense category mapping (meal/transport/accommodation/entertainment/other) |

### YAML Test Case Format

Every case follows the same structure, regardless of component:

```yaml
- id: rule1_positive_overlap
  component: fraud_rules_deterministic
  rule: duplicate_attendee                    # which rule function to call
  description: "A and B same day same merchant, B's attendees include A"
  input:
    submissions:
      - id: "s1"
        employee_id: "emp-A"
        amount: 200
        category: "meal"
        date: "2026-04-10"
        merchant: "Haidilao"
        attendees: ["emp-B"]
      - id: "s2"
        employee_id: "emp-B"
        amount: 180
        category: "meal"
        date: "2026-04-10"
        merchant: "Haidilao"
        attendees: ["emp-A"]
  expect:
    has_signal: true                           # should this rule fire?
    rule_name: "duplicate_attendee"            # expected signal name
```

### Code-Based Graders (No LLM in the Loop)

All grading is deterministic — `backend/tests/graders/code_graders.py`:

| Grader | What It Checks |
|--------|---------------|
| `grade_score_range` | Actual score within [lo, hi] range |
| `grade_field_match` | Exact field value match |
| `grade_enum_in` | Value in allowed set |
| `grade_list_contains` | All required items present in list |
| `grade_bool` | Boolean match (for `has_signal`) |
| `classify_detection` | TP/FP/FN/TN classification for P/R/F1 metrics |

The universal `grade_case(actual_output, expect)` function auto-dispatches: `*_range` keys use range grader, `has_signal` uses bool grader, `layer` uses field match, etc.

### 6-Factor Reproducibility Tracking

Every eval run captures 6 factors in `eval_config.json` to ensure reproducibility:

| Factor | What It Tracks | Example |
|--------|---------------|---------|
| **Prompt version** | Which prompt template is active | `v1` |
| **Model** | LLM model + snapshot | `gpt-4o-2025-03-01` |
| **Sampling params** | temperature, top_p, max_tokens | `0.0, 1.0, 1024` |
| **Config thresholds** | Rule-specific tuning knobs | `threshold_proximity_pct: 0.03` |
| **Parsing version** | How LLM output is parsed into structured data | `v1 (JSON + regex fallback)` |
| **Dataset hash** | MD5 of all YAML dataset files combined | `auto-computed` |

When pass rate drops, diff two runs via `GET /api/eval/runs/{a}/diff/{b}` to see exactly which factor changed and which cases regressed.

### Observatory Dashboard & API

**Web UI** at `/eval/dashboard.html`:
- KPI cards: total cases, pass rate, component breakdown
- Pass rate trend chart (last 10 runs)
- Per-case drill-down: input, expected, actual output, classification
- Run comparison (diff view): metadata changes + case regressions/improvements
- Prompt management: view/edit/version prompt templates, set active version
- One-click eval trigger (runs pytest in background)

**REST API** at `/api/eval/`:

| Method | Path | Function |
|--------|------|----------|
| `GET` | `/runs` | List eval runs (paginated) |
| `GET` | `/runs/{id}` | Single run detail with all case results |
| `POST` | `/runs` | Record a new eval run (called by harness) |
| `GET` | `/runs/{a}/diff/{b}` | Compare two runs: metadata diff + case regressions |
| `GET` | `/traces` | List LLM traces (filterable by component, error status) |
| `GET` | `/traces/{id}` | Single trace with full prompt + response |
| `GET` | `/stats` | Aggregate: pass rate trend + component error rates |
| `GET/PUT` | `/config` | Read/update the 6-factor eval config |
| `POST` | `/trigger` | Trigger eval run via background pytest subprocess |
| `GET` | `/trigger/status` | Check if eval is currently running |
| `GET` | `/prompts` | List all prompt templates with version counts |
| `GET` | `/prompts/{key}` | Full prompt with all versions |
| `PUT` | `/prompts/{key}/versions/{v}` | Create/update a prompt version |
| `PUT` | `/prompts/{key}/active` | Set active prompt version |

### Detection Quality Metrics

For deterministic fraud rules, the harness computes **Precision / Recall / F1** per rule:

```
  -- Detection Quality (P/R) --
  fraud_rule_duplicate_attendee:    P=100% R=100% F1=100%
  fraud_rule_threshold_proximity:   P=100% R=100% F1=100%
  fraud_rule_weekend_frequency:     P=100% R=67%  F1=80%
```

Classification matrix uses business-friendly labels:
- **TP** = correct detection (rule fires when it should)
- **FP** = false alarm (rule fires when it shouldn't)
- **FN** = missed fraud (rule doesn't fire when it should)
- **TN** = correct pass (rule correctly doesn't fire)

### Running Eval

```bash
# Run all eval cases (deterministic — no API key needed)
pytest backend/tests/test_eval_harness.py -v

# Only fraud rules
pytest backend/tests/test_eval_harness.py -v -k deterministic

# Only ambiguity detector
pytest backend/tests/test_eval_harness.py -v -k ambiguity

# With LLM fraud analysis (requires OPENAI_API_KEY)
OPENAI_API_KEY=sk-... pytest backend/tests/test_eval_harness.py -v -k llm

# Agent behavior eval (separate harness)
pytest backend/tests/test_agent_eval.py -v -s

# Via Observatory API (trigger from dashboard)
curl -X POST http://localhost:8000/api/eval/trigger -H 'Content-Type: application/json' -d '{"component": "all"}'
```

Results are automatically posted to the Observatory API if the server is running; otherwise saved to `backend/tests/eval_last_run.json` for later import.

---

## API Overview

**Reports** (expense report — employees bundle line items into a report and submit as a unit)

| Method | Path | Role | Description |
|--------|------|------|-------------|
| `POST` | `/api/reports` | employee | Create a new report |
| `GET` | `/api/reports` | employee | List my reports |
| `GET` | `/api/reports/{id}` | all | Report detail (employees see own only) |
| `POST` | `/api/reports/{id}/submit` | employee | Submit report for approval |
| `POST` | `/api/reports/{id}/withdraw` | employee | Withdraw a submitted/approved report |
| `POST` | `/api/reports/{id}/resubmit` | employee | Resubmit a `needs_revision` report |
| `POST` | `/api/reports/{id}/approve` | manager | Manager approves the report |
| `POST` | `/api/reports/{id}/reject` | manager | Manager rejects |
| `POST` | `/api/reports/{id}/return` | manager | Return for revision (`needs_revision`) |
| `POST` | `/api/reports/{id}/finance-approve` | finance_admin | Finance approval |
| `POST` | `/api/reports/{id}/finance-reject` | finance_admin | Finance rejection |
| `PATCH` | `/api/reports/{id}/title` | employee | Rename report |
| `PATCH` | `/api/reports/{id}/lines/{sid}` | employee | Edit a line item |
| `DELETE` | `/api/reports/{id}/lines/{sid}` | employee | Delete a line item |
| `DELETE` | `/api/reports/{id}` | employee | Delete an empty open report |

**Submissions**

| Method | Path | Role | Description |
|--------|------|------|-------------|
| `POST` | `/api/submissions` | employee | Submit expense, returns 202 + background AI review |
| `GET` | `/api/submissions/{id}` | all | View single submission (employees see own only) |
| `GET` | `/api/submissions` | all | List submissions (employees see own only) |
| `POST` | `/api/submissions/{id}/approve` | manager | Manager approval |
| `POST` | `/api/submissions/{id}/reject` | manager | Manager rejection |
| `POST` | `/api/finance/submissions/{id}/approve` | finance_admin | Finance approval + voucher number |
| `POST` | `/api/finance/submissions/{id}/reject` | finance_admin | Finance rejection |
| `GET` | `/api/finance/export/preview` | finance_admin | Export preview list |
| `POST` | `/api/finance/export` | finance_admin | Bulk export CSV |

**Agent / Chat**

| Method | Path | Role | Description |
|--------|------|------|-------------|
| `POST` | `/api/chat/drafts` | employee | Create new draft |
| `POST` | `/api/chat/drafts/{id}/receipt` | employee | Upload receipt to draft |
| `POST` | `/api/chat/drafts/{id}/message` | employee | Agent 1: submit-flow chat on `quick.html` (SSE) |
| `POST` | `/api/chat/drafts/{id}/submit` | employee | Convert draft to formal submission |
| `POST` | `/api/chat/message` | employee | Agent 2: unified drawer on every employee page (SSE). Optional `context: {report_id}` tells the LLM which report the user is looking at |
| `POST` | `/api/chat/explain/{id}` | manager / finance_admin | Agent 3: structured AI risk card (JSON, not a chat) |

**Budget**

| Method | Path | Role | Description |
|--------|------|------|-------------|
| `GET` | `/api/budget/status/{cost_center}` | all | Cost center budget status |
| `GET` | `/api/budget/snapshot/me` | employee | My budget overview |
| `GET/PUT` | `/api/budget/policies/{cc}` | finance_admin | Budget policy config |
| `GET/POST` | `/api/budget/amounts` | finance_admin | Budget amount management |

**Admin**

| Method | Path | Role | Description |
|--------|------|------|-------------|
| `GET/PUT` | `/api/admin/policy` | admin | Expense policy |
| `GET` | `/api/admin/audit-log` | admin | Audit log |
| `GET` | `/api/admin/stats` | admin | Summary statistics |
| `GET` | `/api/users/me` | all | Current user info |

---

## 5 Things We Deliberately Don't Do

| # | What We Don't Do | Why |
|---|-----------------|-----|
| 1 | **Turn the 5-Skill pipeline into an Agent** | Compliance requires deterministic auditability; shifting legal liability from rules to LLM is unacceptable |
| 2 | **Give the Agent submit/approve tools** | The whitelist is the last line of injection defense — breaching it dismantles the entire security model |
| 3 | **Add a chat drawer to the approval page** | Managers review ~30 items/day; +30 sec/item = +15 min/day — they'll just turn off AI |
| 4 | **Let AI auto-submit expenses** | Legal liability is on the employee; Submit must be human-confirmed (SAP Concur Joule 2026 made the same decision) |
| 5 | **Adopt an Agent SDK from day one** | MVP native tool calling is sufficient; SDK value is in subagents/memory, which this project doesn't need yet |

---

## Project Structure

```
backend/
  main.py                          # FastAPI entry point
  config.py                        # DATABASE_URL and env config
  storage.py                       # File storage abstraction
  api/
    middleware/auth.py              # RBAC auth middleware
    routes/
      submissions.py               # Expense submission + 5-Skill pipeline trigger
      chat.py                      # Chat Agent (3 roles)
      reports.py                   # Expense report management
      approvals.py                 # Manager approval
      finance.py                   # Finance approval + voucher export
      budget.py                    # Budget management
      fx.py                        # Foreign currency conversion
      admin.py                     # Admin policy configuration
      employees.py                 # Employee CRUD
      ocr.py                       # OCR recognition API
      eval.py                      # Eval dashboard API
  db/store.py                      # SQLAlchemy async ORM + CRUD
  quick/
    pipeline.py                    # Quick pipeline orchestration
    layer_decision.py              # Layered decision engine
    finalize.py                    # Draft -> formal submission conversion
  services/
    fraud_rules.py                 # Deterministic fraud detection rules
    llm_fraud_analyzer.py          # LLM fraud analysis
    fx_service.py                  # Exchange rate service
    config_loader.py               # YAML config loader
    trace.py                       # Call chain tracing
  tests/
    eval_datasets/                 # Eval YAML test datasets
    graders/                       # Custom graders
    test_*.py                      # Unit + integration tests
agent/
  controller.py                    # ExpenseController - workflow orchestration
  ambiguity_detector.py            # 5-factor scoring + Claude API deep analysis
skills/
  skill_01_receipt.py              # 4-layer receipt validation
  skill_02_approval.py             # Approval chain + timeout escalation
  skill_03_compliance.py           # A/B/C compliance judgment + Shield
  skill_04_voucher.py              # Accounting voucher + VAT splitting
  skill_05_payment.py              # 5-point pre-check + payment simulation
config/
  policy.yaml                      # Expense limits, city tiers, employee level caps
  approval_flow.yaml               # Approval matrix, timeout escalation rules
  expense_types.yaml               # Expense types, accounting codes, VAT config
  workflow.yaml                    # Pipeline orchestration (enable/disable/fail)
  city_mapping.yaml                # City name normalization mapping
  fx_rates.yaml                    # Foreign exchange rates
frontend/
  employee/                        # Employee: submit, drafts, reports, history
  manager/                         # Manager: approval queue
  finance/                         # Finance: review, export
  admin/                           # Admin: policy, employees, audit logs
  eval/                            # Eval dashboard
  shared/                          # Common JS/CSS, API wrapper, auth
models/                            # Data models (Pydantic)
rules/                             # Policy engine + city normalization
mock_data/                         # 7 test scenario factory functions
scripts/seed_demo_data.py          # Demo data seed script
Dockerfile                         # Container deployment
docker-compose.yml                 # Docker Compose orchestration
requirements.txt                   # Python dependencies
```

---

## Quick Start

```bash
# 1. Install dependencies
pip install -r requirements.txt

# 2. (Optional) seed demo data
python scripts/seed_demo_data.py

# 3. Start backend in dev mode
uvicorn backend.main:app --reload --port 8000
```

**Access points** (role is switched via the navbar dropdown or `?as=<role>` URL param in mock auth mode):

| Who | URL |
|-----|-----|
| Employee | `http://localhost:8000/employee/quick.html` |
| Manager | `http://localhost:8000/manager/queue.html` |
| Finance | `http://localhost:8000/finance/review.html` |
| Admin | `http://localhost:8000/admin/dashboard.html` |
| Eval Observatory | `http://localhost:8000/eval/dashboard.html` |
| OpenAPI Docs | `http://localhost:8000/docs` |

**5-minute end-to-end demo:**

| Step | Role | Action |
|------|------|--------|
| 1 | employee | `quick.html` → upload any receipt → AI recognizes fields → confirm submit |
| 2 | — | AI review runs in background (1–3 s); `my-reports.html` polls until `status=reviewed` |
| 3 | manager | `queue.html` → open submission → view AI explanation card → approve |
| 4 | finance_admin | `review.html` → approve → voucher number generated |
| 5 | finance_admin | `export.html` → bulk export CSV |

### Environment Variables

| Variable | Default | Description |
|----------|---------|-------------|
| `AUTH_MODE` | `mock` | `mock` (dev) / `clerk` (production) |
| `DATABASE_URL` | SQLite local file | Production: `postgresql+asyncpg://...` |
| `EVAL_DATABASE_URL` | SQLite `concurshield_eval.db` | Physically isolated from business DB — stores LLM traces + eval runs so trace volume doesn't affect main DB performance |
| `STORAGE_BACKEND` | `local` | `local` / `r2` (Cloudflare R2) |
| `ANTHROPIC_API_KEY` | -- | Optional: AmbiguityDetector deep analysis (triggered when score >50) |
| `OPENAI_API_KEY` | -- | Optional: Chat Agent uses GPT-4o |
| `OPENAI_MODEL` | `gpt-4o` | Override the model when `OPENAI_API_KEY` is set |
| `AGENT_USE_REAL_LLM` | -- | Set to `1` + provide API Key → switch to RealLLM |

### MockLLM vs RealLLM

| Mode | Condition | Behavior |
|------|-----------|----------|
| **MockLLM** (default) | No API Key needed | Deterministic state machine: happy path runs linearly, keyword routing, deterministic eval |
| **RealLLM (GPT-4o)** | `OPENAI_API_KEY` + `AGENT_USE_REAL_LLM=1` | GPT-4o real reasoning, unlocks "user edits fields" Agent behavior |

---

## Running Tests

```bash
# Unit tests
python -m pytest backend/tests/ -v

# Eval assessment
python -m pytest backend/tests/test_agent_eval.py -v

# Full flow tests (7 scenarios)
python -m pytest tests/test_full_flow.py -v
```

---

## Tech Stack

- **Backend**: FastAPI, SQLAlchemy (async), aiosqlite/asyncpg
- **AI**: Claude API (AmbiguityDetector), OpenAI GPT-4o (Chat Agent), MockLLM (default)
- **Frontend**: Vanilla HTML/JS (no framework dependencies)
- **Database**: SQLite (dev) / PostgreSQL (production)
- **Config**: YAML configuration-driven (policy, workflow, approval, expense types, city mapping)
