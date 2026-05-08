# Code Health Audit — Duplication & Coupling

> **Status:** Diagnostic — measured but not fixed. This doc names what's wrong so the team has a contract for when to pay it back.
>
> **Auditor:** Internal review, May 2026
>
> **Scope:** `backend/api/routes/`, `frontend/{employee,manager,finance}/`, `backend/db/store.py`, `backend/api/routes/chat.py`

---

## TL;DR

ExpenseFlow has **~14% duplication** in its application code (≈500 of ~3,500 lines repeated 2-9 times across files). This is a normal cost of vertical-AI MVP velocity, **but it is being named and budgeted, not hidden**.

| Hotspot | Instances | Duplication ratio | Shared layer? | Severity at current scale |
|---|---|---|---|---|
| Status-change endpoints (approve/reject/return/finance-approve) | 9 endpoints | 35-40% logic repeated | ❌ none | Medium |
| Frontend list+detail pages (my-reports / queue / review) | 3 pages | 70-85% function similarity | ⚠️ CSS only | High (visible to anyone reading the repo) |
| Chat tool pairs (`get_report_detail` vs `get_submission_for_review`) | 2 tools | ~60% serialization overlap | ❌ none | Low |
| Status-string hardcoding ("pending"/"manager_approved"/...) | 54 occurrences | ad-hoc per file | ❌ no enum | Medium |
| Audit timeline writing | 15+ call sites | ~45% boilerplate | ❌ no builder | Low |

**This audit is the contract**: when one of these hits a payback trigger (defined per row), the team executes the named refactor. Until then, duplication stays — *named, not hidden*.

---

## Why this exists (root cause analysis)

Three structural causes, ranked by leverage:

### Cause 1 — Role modeled as layout boundary, not policy boundary

The folder split `frontend/employee/` / `frontend/manager/` / `frontend/finance/` is the physical artifact of this mistake. Three separate HTML pages (`my-reports.html`, `queue.html`, `review.html`) render the same domain object (a list of reports + a detail panel) with different action buttons.

**Industry comparison**: Concur, Airwallex, Ramp all use **one list view with role-conditional action buttons**. They learned this the same way every B2B SaaS does — by writing it wrong twice and then refactoring.

What this codebase has instead:
- 472 + 514 + 464 = **1,450 lines** for 3 nearly-parallel pages
- `fmtMoney()` defined 3 times, identical implementation
- `renderList()` defined 3 times, ~75% shared logic
- Risk/tier color mapping inlined in 3 places

### Cause 2 — Status machine doesn't exist

9 approval-style endpoints across `submissions.py` / `reports.py` / `approvals.py` / `finance.py` each implement the same 5-step pattern:

```
load → check status ∈ allowed_set → mutate status → write audit → notify
```

This pattern is **copied 9 times** with minimal variation. Status strings (`pending`, `manager_approved`, `finance_approved`, `exported`, `reviewed`, `rejected`, `processing`, `in_report`, `needs_revision`, `open`, `withdrawn` — 11 distinct values) are hardcoded in 54 locations. There is no `transition(submission, from_status, to_status, actor)` function.

**Industry comparison**: SAP famously rewrote Concur's approval engine as a generic state machine after the acquisition — same problem, larger scale.

What this codebase has instead:
- `if sub.status not in _MANAGER_ACTIONABLE:` patterns repeated per endpoint with slight variations
- Transition rules ("only `pending → manager_approved` allowed for managers") enforced inline per endpoint, never declared
- No invariant that a state machine would catch (e.g., illegal transitions silently succeed if a route forgets the check)

### Cause 3 — No "duplication budget" with a payback trigger

This is the deepest cause; the first two are symptoms.

The industry heuristic is **rule of three**:
- 1 instance: write it
- 2 instances: copy with TODO
- 3 instances: refactor (mandatory)

Several hotspots in this codebase have 3-9 instances *without the refactor trigger ever firing*. The duplication wasn't ignorance of DRY — it was the absence of a checklist gate. Every PR was "ship the feature, refactor next sprint." Compounded over 47 PRs, that became 14% waste.

---

## Detail per hotspot

### Hotspot 1 — Status-change endpoints (9 endpoints, 35-40% repeated)

**Files**: `backend/api/routes/approvals.py`, `reports.py`, `finance.py`

**Endpoints**:
- `POST /api/submissions/{id}/approve` (manager) — approvals.py:45-76
- `POST /api/submissions/{id}/reject` (manager) — approvals.py:81-106
- `POST /api/submissions/bulk-approve` (manager) — approvals.py:111-142
- `POST /api/reports/{id}/approve` (manager) — reports.py:579-620
- `POST /api/reports/{id}/reject` (manager) — reports.py:623-658
- `POST /api/reports/{id}/return` (manager) — reports.py:661-708
- `POST /api/reports/{id}/finance-approve` (finance) — reports.py:340-390
- `POST /api/reports/{id}/finance-reject` (finance) — reports.py:393-429
- `POST /api/reports/bulk-approve` (finance) — reports.py:432-468

**What's repeated**: load object → check `status in allowed_set` → set new status → call `append_audit_step()` → call `create_audit_log()` → maybe enqueue notification.

**What's not repeated**: the specific status transition (which differs by 1-2 strings) and the allowed-set check (which differs by which statuses count).

**Refactor path**: extract `transition(resource, from_status, to_status, actor, audit_message)` helper that owns the 5 steps. Each endpoint becomes ~5 lines instead of ~30-40.

**Payback trigger**: when adding the 10th endpoint of this shape, OR when adding a new status that requires editing 9 places.

### Hotspot 2 — Frontend list+detail pages (3 pages, 70-85% similar)

**Files**: `frontend/employee/my-reports.html`, `frontend/manager/queue.html`, `frontend/finance/review.html`

**Duplication breakdown**:

| Component | my-reports | queue | review | Status |
|---|---|---|---|---|
| `fmtMoney()` | line 167 | line 123 | line 113 | 3 identical copies |
| `renderList()` | line 317 (65 lines) | line 141 (50 lines) | line 171 (45 lines) | ~75% shared |
| `renderDetail()` / `renderStats()` | n/a | line 183 (98 lines) | line 208 (100 lines) | ~80% overlap |
| Risk/tier color mapping | inline 149-152 | inline 149-152 | inline 114-115 | 3 identical inline blocks |
| Modal confirm pattern | 100-115 | 99-112 | 91-104 | ~85% identical |

**What's not repeated**: the action-button set (employee can recall, manager can approve/reject/return, finance can approve/reject/export) and the column set (employee sees no reviewer column; finance sees export status).

**Refactor path**:
1. Extract `frontend/shared/reports-table.js` — a module that exports `renderReportsList(opts)` and `renderReportDetail(opts)`, where `opts` includes `role`, `actions`, `columns`.
2. Or: write a `<reports-table>` Web Component that accepts `role` as an attribute. Higher upfront cost, but the cleanest end state.

**Payback trigger**: when adding a 4th list view (e.g., "rejected reports archive" or "auditor read-only view"), OR when changing the date format / risk badge style requires editing 3+ files.

### Hotspot 3 — Chat tool pairs (2 tools, ~60% overlap)

**Files**: `backend/api/routes/chat.py`

- `tool_get_report_detail` (chat.py:527-564, 38 lines) — employee, owner-scoped
- `tool_get_submission_for_review` (chat.py:567-607, 41 lines) — manager/finance, owner-bypassed

**What's repeated**: 14 of the 18 fields serialized are identical (merchant, amount, currency, category, date, tax_amount, description, invoice_number, status, etc.) with the same `Decimal → float` and `datetime.isoformat()` pattern.

**What's not repeated**: ownership scoping (4 lines) and the manager-only fields (audit_report, tier, risk_score — added in the manager version).

**Refactor path**: single `serialize_submission(sub, *, scope: Literal["self", "approver"])` function. Both tools become 5-line wrappers around it.

**Payback trigger**: when adding a 3rd serialization context (e.g., admin export, partner API), OR when changing how `audit_report` is exposed forces editing both tools.

### Hotspot 4 — Status string hardcoding (54 occurrences)

**Files** mutating or checking `Submission.status` / `Report.status`:
1. `backend/db/store.py` — generic `update_submission_status` / `set_report_status`
2. `backend/api/routes/reports.py` — direct `s.status = ...` at 521, 600, 644, 684, 746, 789
3. `backend/api/routes/chat.py` — inline assignments during draft → submission
4. `backend/api/routes/admin.py` — bulk admin operations
5. `backend/api/routes/auto_rules.py` — auto-rule state changes

**What's wrong**: 11 distinct status strings appear in 54 locations as raw strings. A typo (`"manager_approved"` vs `"manager-approved"`) would silently fail.

**Refactor path**: single `enum.StrEnum` (or `Literal`) module — `backend/domain/status.py` — used everywhere. Bonus: makes the state machine in Hotspot 1 almost free.

**Payback trigger**: first production bug caused by status string typo, OR when adding the 12th status. Whichever comes first.

### Hotspot 5 — Audit timeline writing (15+ call sites, ~45% boilerplate)

**Files**: `submissions.py` (3×), `reports.py` (6×), `approvals.py` (3×), `finance.py` (1×), plus more in admin / chat.

**Pattern repeated**:
```python
await append_audit_step(
    db, submission_id,
    message=f"[ROLE] {actor} [ACTION]，[DETAIL]",
    phase="[STATUS_NAME]",
)
await create_audit_log(
    db, actor_id=ctx.user_id, action="[action_name]",
    resource_type="submission/report", resource_id=...,
    detail={**dict},
)
```

**What's wrong**: every endpoint manually constructs the message f-string and the detail dict. No event class, no builder, no templating.

**Refactor path**: `audit_event(actor, action, resource, *, detail=None, phase=None)` — single helper that handles both calls + standardizes the f-string format. Becomes 1 line per call site instead of 8-10.

**Payback trigger**: when adding a localized audit message (Chinese/English/...), OR when audit log format needs to change for SOC 2 evidence collection.

---

## Severity assessment

**At current scale (3,500 lines, demo)**:
- Pain: low. A single engineer can navigate the duplication. Mental model fits in head.
- Risk: low. No customer cares yet.

**At 10K-15K lines (Path B — first paying customers)**:
- Pain: high. Adding a new status / role / approval rule touches 9-15 places. Onboarding a 2nd engineer means showing them which of the 9 endpoints to copy from.
- Risk: medium. Status machine bugs become production incidents. Audit log inconsistency becomes SOC 2 finding.

**At 50K+ lines (Path B — enterprise tier)**:
- Pain: very high. Every state addition is a multi-week refactor.
- Risk: high. AI agent behavior diverges across views (manager chat sees one shape of audit_report, finance chat sees another) → customer trust erodes.

---

## Refactor budget

The full payback (all 5 hotspots) is **~1.5-2 weeks of one engineer's time**:

| Hotspot | Effort | When to do it |
|---|---|---|
| 1. Status state machine | 0.5 day | Before adding the 10th approval endpoint, or before any new status |
| 2. Frontend list module | 1 day | Before adding the 4th list view |
| 3. Submission serializer | 2 hours | Before adding the 3rd serialization context |
| 4. Status enum | 0.5 day (often) | Concurrent with #1 — they're the same refactor in two layers |
| 5. Audit event helper | 1 hour | Before localizing audit messages, or before SOC 2 prep |

**Recommended sequencing**: do #4 + #1 together (state machine + enum is one PR). #5 falls out almost free. Then #3. Then #2 (highest visible payoff but largest effort).

---

## What this doc deliberately doesn't say

- **It's not a refactor PR.** Naming the debt is the work this document does. The refactors happen on payback triggers, not on aesthetic discomfort.
- **It's not a complaint about velocity.** The MVP shipped because copy-paste was faster than the right abstraction at the time. That's correct. The mistake would be pretending the cost doesn't exist.
- **It's not a list of every smell.** I focused on the 5 places where copy-paste compounds across role boundaries. Other smells (function-too-long, missing type hints, etc.) are real but lower-leverage.

---

## References

- [`industrial-readiness-roadmap.md`](industrial-readiness-roadmap.md) — Gap 9 references this doc
- [`hybrid-fraud-architecture.md`](hybrid-fraud-architecture.md) — counter-example: that subsystem was designed before being written, has near-zero duplication
- Martin Fowler — *Refactoring*, ch. 3 "Bad Smells in Code" (specifically "Shotgun Surgery" pattern, which is what Hotspot 1 + 4 are)
- "Rule of three" heuristic — credited to Martin Fowler / Don Roberts

---

*This doc is the honest audit. When asked "is the codebase clean?" the answer is "no, here's the 14%, here's why, here's when we pay it back." That's the discipline — same one used for `industrial-readiness-roadmap.md`.*
