# Eval Harness Plan — Capability vs Regression, Bug-Hunt Loop, 60-Score Target

> **Status:** Phase 1 implemented (this PR). Phases 2-3 are forward-compat plans.
>
> **Source principles:** the team's eval methodology brief
> ("评测集应当在难度、丰富度和精度要求上，超前/领先于训练集") plus
> Anthropic's Eval Harness vocabulary (Task / Trial / Trajectory / Grader /
> Eval Suite). See [`evals-reference.md`](evals-reference.md) for terminology
> mapping and the prior Hamel-aligned 3-level framework.

---

## TL;DR — the uncomfortable diagnosis (and the fix)

> **`fraud_investigator κ = 1.0` on 5 cases is a bug, not a feature.**
>
> κ = 1.0 means the eval set has no discriminative power. It was designed
> to *pass*, not to *expose weaknesses*. By the principle "best eval state
> is one that keeps AI scoring under 60," our current eval is failing to
> drive iteration.

This plan addresses that by:

1. **Splitting evals into two tiers** with different intents (Phase 1 ✓)
2. **Re-populating the capability suite** with cases the system FAILS on (Phase 2)
3. **Codifying a bug-hunt loop** so capability never stays "easy" (Phase 3)

---

## Two-tier model

| Tier | Intent | Target κ | When run | Failure consequence |
|---|---|---|---|---|
| **Regression** | Must-pass cases for every release. Easy + representative. | **≥ 0.85** | Every PR (CI gate) | **Block merge.** |
| **Capability** | Hard cases that expose weaknesses. Drives iteration. | **0.5 – 0.7** (deliberately) | Every PR (snapshot only) | **Snapshot only.** Failure means "agent isn't perfect yet" — that's the point. |

> **Why capability is targeted at ~60% and not higher:** a capability suite
> at 0.9+ has lost its job. The cases got too easy; iteration has no pull.
> When κ creeps above 0.7, we **refresh the suite** with harder cases
> (per Phase 3 below).

The regression suite stays high-pass on purpose: it's the "we cannot ship
if these break" gate. The two suites measure different things and should
move in opposite directions over time (regression κ stays high; capability
κ stays in the discomfort zone).

---

## Phase 1 — cleanup + restructure (✓ THIS PR)

### Files

| Old | New | Action |
|---|---|---|
| `fraud_investigation_human_labeled.yaml` (5 real + 15 PH) | `eval_regression_fraud_investigator.yaml` | Keep 5 real; **delete 15 placeholders** |
| `agent_compliance_human_labeled.yaml` (12 real + 18 PH) | `eval_regression_agent_compliance.yaml` | Keep 12 real; **delete 18 placeholders** |
| `ambiguity_human_labeled.yaml` (0 real + 3 PH) | (file deleted) | All-placeholder file removed |
| `fraud_human_labeled.yaml` (0 real + 3 PH) | (file deleted) | All-placeholder file removed |
| (none) | `eval_capability_fraud_investigator.yaml` | New empty stub for Phase 2 |
| (none) | `eval_capability_agent_compliance.yaml` | New empty stub for Phase 2 |
| (none) | `eval_capability_ambiguity.yaml` | New empty stub for Phase 2 |
| (none) | `eval_capability_fraud_llm.yaml` | New empty stub for Phase 2 |

**Net change**: 39 placeholder cases deleted; 17 real cases preserved as
regression. Capability suites are explicitly empty (with TODO headers
listing seed candidates).

### Tests

- `test_fraud_investigator_eval.py` → reads regression file
- `test_agent_compliance_eval.py` → reads regression file
- `test_human_eval.py` → reads capability file (skips cleanly while empty)
- `test_judge_agreement.py` → reads capability file (skips cleanly while empty)

### Why we deleted instead of migrating

The 39 placeholder cases had `description: "PLACEHOLDER — ..."` and
`labeler_note: "PLACEHOLDER"`. They were skipped by every test path (per
the Stage 1 cleanup convention). Their continued presence was theatre —
implying capacity that didn't exist. **The cleanup is the work.**

If you want a record of what placeholders existed: `git log --oneline`
points at the deleted blobs, and the new capability stub headers preserve
the "seed candidates worth chasing first" hints.

---

## Phase 2 — populate capability suites (FORWARD-COMPAT)

**Goal**: each capability suite has 20-30 cases the current system FAILS on
(or partially fails). Target κ in 0.5-0.7 range — **not** 0.9+.

### Recommended sequencing

1. **Start with `fraud_investigator`** (highest leverage; OODA path is the
   most complex; mock + real-LLM divergence is the easiest signal)
2. Then `agent_compliance` (cross-record reasoning is the next-most complex)
3. Then `ambiguity_detector` (rule-based, less LLM-driven)
4. Last `fraud_llm_analyzer` (subfield-bucket scoring is fiddly)

### How to source 30 cases per component

| Source | Volume | Cost | Notes |
|---|---|---|---|
| Real anonymized expenses | 5-10 seeds | (your time / friend favors) | Start here — these have ground-truth realism |
| Vision-LLM-generated receipt images | 50-100 variants | ~$5 / 100 (GPT-4o or Claude Vision) | Different fonts / amounts / layouts; pick 5-15 best |
| Logical YAML variants from one seed | 10-50 variants | LLM cost only | "Same scenario, change one variable" — LLM-batched |
| Real receipts photographed | 30 raw | 1 weekend | OCR-fail rate becomes the seed list |

**Budget**: ~$30-50 + 1 weekend for the first 30 hard fraud_investigator
cases. Total Phase 2 across all 4 components: **5-7 days / 1 person**.

### The selection criterion

A case earns its place in the capability suite when:

- **Mock and real-LLM paths disagree** on the verdict (high signal)
- **Domain-expert intuition disagrees with current output** (qualitative)
- **The case probes a NEW failure mode** not covered by existing capability cases

Discard cases that just confirm what the regression suite already covers.

---

## Phase 3 — bug-hunt loop (FORWARD-COMPAT, ongoing)

This is the operational discipline that keeps capability honest forever.

```
A user / reviewer / dashboard alert reports a bad case
  ↓
Reproduce locally (one test invocation, see verdict)
  ↓
Generate ~100 variants (different employees, amounts, contexts, ...)
  ↓
Curate 20-30 most discriminative → add to capability suite
  ↓
Push remaining 70+ to the few-shot example bank in the agent's
  system prompt (NOT the eval suite)
  ↓
Re-run capability eval; expect κ to drop into 0.5-0.7
  (if it stays > 0.7, the cases were not hard enough — keep digging)
  ↓
Track in dashboard: "困难 case 库存" = number of capability cases the
  current path FAILS on. Goal: never zero (zero = stagnation).
```

### Why "100 variants → 20 keep / 80 prompt bank" matters

This is the principle's "举一反三" — one bad case isn't a fix opportunity,
it's a **discovery opportunity**. A case where the agent missed
"weekend salary-padding" probably means it'd miss many similar patterns:
different categories, different amounts, different cost centers. Mining
those variants is how the eval set gains discriminative power.

The 80 that don't make the eval still have value — they go into the
agent's prompt as few-shot examples ("here are patterns that look
suspicious"), so the *agent itself* improves before the next eval.

---

## Anthropic terminology mapping

| Anthropic term | ExpenseFlow concrete artifact |
|---|---|
| **Task / Case / Query** | A single YAML entry in `eval_*.yaml` |
| **Trial** | One pytest run of one task (mock and real-LLM are different trials) |
| **Trajectory / Trace** | The full LLMTrace + `audit_report.investigation.evidence_chain` for a trial |
| **Grader** | `cohens_kappa()` + `must_call_tools` checker in `test_judge_agreement.py` |
| **Eval Suite** | A YAML file (one file = one suite of related tasks) |
| **Eval Harness** | The whole pytest + dashboard + snapshot infra; rooted in `backend/tests/test_*_eval.py` |

The 5 elements of an Eval Harness (Anthropic):

| Element | ExpenseFlow status |
|---|---|
| Data Loader | ✓ YAML loader in each test file |
| Runner | ✓ pytest with per-case parametrization |
| Environment isolation | ✓ Fresh tmp SQLite per pytest run |
| Trace logging | ✓ `LLMTrace` table + Review Quality dashboard |
| Result aggregator | ✓ JSON snapshots → `/eval` dashboard |

**No structural gaps.** The infra is in place; the work is the data quality.

---

## Success metrics (3 months out)

| Metric | Today (after this PR) | 3-month target | Why |
|---|---|---|---|
| Regression suites | 2 (fraud_investigator, agent_compliance) | 4 (also ambiguity, fraud_llm) | Coverage parity |
| Regression κ (each) | 1.0 (fraud_investigator) | ≥ 0.85 maintained | Must-pass gate stable |
| Capability suite case count | 0 | 30+ per component | Phase 2 done |
| Capability κ (each) | n/a | **0.5 – 0.7** | Discomfort zone = iteration pull |
| "困难 case 库存" in dashboard | n/a | 20+ tagged "current FAIL" | Backlog for next iteration |

**Anti-goal**: capability κ above 0.7 across the board. If that happens,
**refresh the suite**, don't celebrate.

---

## What this plan deliberately doesn't do

- **Doesn't introduce a new eval framework.** We use pytest + YAML + snapshots
  because the existing infra works. No new dependency.
- **Doesn't add a "training set" concept.** ExpenseFlow uses prompts, not
  fine-tuned models. The closest analog to "training set" is the few-shot
  example bank in agent system prompts; per Phase 3, that's where the 80
  non-curated variants land.
- **Doesn't promise specific Phase 2/3 PRs.** Phases 2 and 3 are intent-only
  in this plan. They get scheduled when (a) Phase 1 has soaked for a week
  without surprises, and (b) a Phase 2 case-sourcing budget is allocated.
- **Doesn't gate Phase 2 on "perfect" Phase 1.** If a hard case shows up
  during normal product work before Phase 2 starts, drop it into the
  capability stub immediately. The bug-hunt loop runs anytime, not just
  when "Phase 2 begins."

---

## References

- [`evals-reference.md`](evals-reference.md) — the underlying Hamel
  3-level framework (units / human-judge / A-B). This plan operates at
  level 2 (human-judge κ).
- [`hybrid-fraud-architecture.md`](hybrid-fraud-architecture.md) §5 —
  "measuring whether the agent agrees with humans" — the κ design rationale.
- [`code-health-audit.md`](code-health-audit.md) — Gap 9, parallel-discipline
  doc that names debt rather than refactoring it. This eval-harness plan
  uses the same "name and budget, don't pretend" discipline.

---

*This doc is the contract for what eval honesty looks like on this project.
When asked "is your eval κ = 1.0 a real signal?" the answer is "no — it's
on the regression suite by design. The capability suite is where iteration
happens, and we keep it scored at 60."*
