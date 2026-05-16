# CLAUDE.md — Working Contract for Claude Code on ExpenseFlow

> Read this at session start. It's short on purpose.

## Context

ExpenseFlow is a vertical-AI expense management portfolio project. The reviewer (project owner) is a senior PM, not a senior engineer — they direct the agent and review at the architecture level. They are actively building their **code-reading muscle**, which means: **don't bury the lede in your PR descriptions**.

Current code health: ~14% duplication documented in [`docs/code-health-audit.md`](docs/code-health-audit.md). Architecture is currently CRUD-shaped; target is domain-shaped (see [`docs/industrial-readiness-roadmap.md`](docs/industrial-readiness-roadmap.md) Gap 9). Don't quietly recreate the patterns the audit named.

---

## Protocol 1 · PR descriptions must have a "Reviewer Focus" section

Every PR you create includes this block, **placed before the "Test plan"**:

```markdown
## Reviewer Focus

The 2-3 things worth your time on this diff:

1. **`path/to/file.py:42-58`** — <one sentence: what changed + why it might be wrong>
2. **`path/to/other.py:120`** — <the trade-off I made; what alternative I rejected>
3. (optional) **boring stuff worth skimming**: <files where the change is mechanical>
```

Why: the reviewer is building the muscle to read diffs critically. Generic "it works, tests pass" descriptions deny them the practice. **Point at the choices, not the line count.**

If a PR is genuinely mechanical (rename, formatting, doc-only), say so explicitly: "Reviewer Focus: nothing — this is a mechanical refactor; CI is the proof."

---

## Protocol 2 · Dedup pre-flight before writing any new "shape"

Before adding any of the following, **grep first, then write**:

| Shape | Pre-flight grep |
|---|---|
| New approval / status-change endpoint | `grep -rn "@router.post.*\(approve\|reject\|return\)" backend/api/routes/` |
| New frontend list+detail page | `ls frontend/employee/ frontend/manager/ frontend/finance/` and read the closest existing page |
| New chat tool | `grep -n "^async def tool_" backend/api/routes/chat.py` |
| New serializer / dict-builder for a model | `grep -rn "merchant.*amount.*currency" backend/api/routes/` |
| New audit-log call site | `grep -rn "append_audit_step\|create_audit_log" backend/` |
| Any `Submission.status` or `Report.status` mutation | `grep -rn "\.status = " backend/` |

Then **count instances** and decide:

- **0–1 existing**: write it
- **2 existing**: write it + add `# TODO: extract if a 3rd instance appears` comment
- **3+ existing**: **stop**. Either extract a helper in this PR, or write `# DEDUP-EXEMPT: <specific reason>` if you genuinely have a reason to diverge.

**Report the result in the Reviewer Focus block** — e.g., "Dedup audit: status-change endpoints now at 9 (already over threshold; not refactored here because Gap 9 is scheduled separately)."

---

## Protocol 3 · UI changes require manual-test confirmation

Tests verify correctness, not feature behavior. If a PR touches:
- HTML / CSS / JS in `frontend/`
- Anything visible in the chat drawer / report list / detail panel
- i18n strings
- Auth / role-routing

Then the PR description **must include either** (a) "I ran this in a real browser, here's what I saw: ..." with at least one observed behavior, **or** (b) "I cannot test this in browser; reviewer must verify before merge" — explicit, not implied.

The cached welcome bug (PR #49 → #50) was a "all tests green, UI still broken" case. Don't repeat that.

---

## Protocol 4 · Architecture direction (so future agents know which way to push)

**Current shape** (CRUD-shaped, 9 endpoints repeating the same 5-step pattern):
```
api/routes/        # owns load + check + mutate + audit + notify per endpoint
db/store.py        # SQLAlchemy + raw status string mutations
```

**Target shape** (domain-shaped, gradual migration):
```
api/routes/        # thin: parse args → call use case → serialize
use_cases/         # business orchestration (e.g., approve_submission.py)
domain/            # invariants (Submission.transition(), StatusEnum, AuditEvent)
infra/             # SQLAlchemy isolated here (repositories.py)
```

When adding a **new** feature, prefer creating it in the target shape (introduce `domain/`, `use_cases/` as needed). Don't refactor the existing 14% — wait for payback triggers per [`docs/code-health-audit.md`](docs/code-health-audit.md).

---

## Protocol 5 · Honest scope limits

This project is a portfolio + design-contract codebase, not a production SaaS. Don't:
- Add SOC 2 / SOX compliance scaffolding (see roadmap Gap 7 — deferred)
- Build payment rails (Gap 5 — deferred)
- Add multi-tenancy beyond `tenant_id` if/when it's needed (Gap 6 — deferred)
- Pursue deep ERP API integration (`integration-design.md` Path B — deferred)

These are **named-and-budgeted in the roadmap**, not "we forgot." If a feature request implies one of these, push back: "this lands us in [Gap N], which is deferred until [trigger]."

---

## Protocol 6 · Read-this-first reading order

When unsure of project conventions, in this order:
1. This file (`CLAUDE.md`)
2. [`README.md`](README.md) — including the "Reading order for portfolio reviewers" section
3. [`docs/code-health-audit.md`](docs/code-health-audit.md) — current debt
4. [`docs/hybrid-fraud-architecture.md`](docs/hybrid-fraud-architecture.md) — what's built
5. [`docs/industrial-readiness-roadmap.md`](docs/industrial-readiness-roadmap.md) — what's deferred

Don't introduce a pattern without checking these first.

---

## Operational reminders

- **Branch naming**: `claude/<feature>-EyRKj` (the suffix is the session marker)
- **Commit messages**: heredoc format with the `https://claude.ai/code/...` footer
- **Tests**: run them before saying "done" — never claim a test passes without seeing the actual `===== passed =====` line
- **Pre-existing failures**: `test_qa_spend_summary_flow` is broken on main (key `total` vs `total_home`). Skip with `--deselect`; do **not** "fix" without scope confirmation

---

*This file is the contract between the reviewer and Claude Code. When the reviewer's habits change (e.g., they become a fluent code reader), this file gets shorter. Until then, the protocols above stay.*
