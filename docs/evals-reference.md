# Evals Reference — ExpenseFlow

> **Status:** Initial skeleton v0.1 · adapted from Hamel Husain's "Your AI Product Needs Evals" framework
> **Audience:** ExpenseFlow contributors (PMs, engineers, eval analysts)
> **Source frame:** Hamel Husain — *Your AI Product Needs Evals* + *LLM Evals: Everything You Need to Know* + *A Field Guide to Rapidly Improving AI Products*. See [References](#references).

---

## 0. Why this doc exists

Most AI products that fail to ship don't fail because the model is bad — they fail because the team can't tell whether changes improve or regress quality. **Evals are the iteration speed of an AI product.**

ExpenseFlow already has a richer eval setup than most projects: 6-factor reproducibility tracking, separated eval DB (`concurshield_eval.db`), labeled human ground-truth datasets, custom graders, and a dashboard for prompt/config editing. This doc:

1. Names what we already have, in Hamel's vocabulary.
2. Identifies gaps against the framework.
3. Gives a single page anyone can read to understand "how ExpenseFlow does evals" without reading the whole codebase.

This is **a reference**, not a runbook. Day-to-day eval workflow lives in `backend/tests/eval_*` files; this doc explains *why* those files exist and how to add the next eval.

---

## 1. Core thesis (Hamel)

> *"Like software engineering, success with AI hinges on how fast you can iterate. The bottleneck is rarely the model — it's the absence of an evaluation system that tells you whether a change is an improvement."*

The flywheel:

```
   ┌──────────────────────┐
   │  Look at data        │  ← always be looking at data
   └──────────┬───────────┘
              ↓
   ┌──────────────────────┐
   │  Error analysis      │  ← what failure modes show up?
   │  (categorize)        │
   └──────────┬───────────┘
              ↓
   ┌──────────────────────┐
   │  Build evaluators    │  ← the right level (1/2/3) for each mode
   └──────────┬───────────┘
              ↓
   ┌──────────────────────┐
   │  Iterate prompts /   │
   │  configs / tools     │
   └──────────┬───────────┘
              ↓
   ┌──────────────────────┐
   │  Re-measure          │  ← did anything regress?
   └──────────┬───────────┘
              └→ back to top
```

**Anti-patterns (Hamel) that this project explicitly fights against:**

- "Vibes-based" evaluation (someone tries 5 inputs, says it's fine)
- Generic eval frameworks not tailored to the product
- Skipping logging
- Using LLM-as-judge without ever measuring its agreement with humans
- Stopping at static accuracy numbers (without trace inspection)

---

## 1.5. Anthropic vocabulary mapping

The Hamel framework gives the **what** (3 levels of evaluators). Anthropic's Eval Harness vocabulary gives the **how** — the runtime infrastructure that takes a dataset and produces scored results. Both apply to ExpenseFlow.

| Anthropic term | Definition | ExpenseFlow concrete artifact |
|---|---|---|
| **Task / Case / Query** | One independent test input | A single YAML entry in `eval_*.yaml` |
| **Trial** | One run of one task | One `pytest` invocation against one case (mock + real-LLM are separate trials) |
| **Trajectory / Trace** | The full record of a trial | `LLMTrace` table row + `audit_report.investigation.evidence_chain` |
| **Grader** | The scorer that turns a trial into a number | `cohens_kappa()` + `must_call_tools` checker in `test_judge_agreement.py` |
| **Eval Suite** | A coherent collection of related tasks | A YAML file (e.g. `eval_regression_fraud_investigator.yaml`) |
| **Eval Harness** | The whole running infrastructure | `backend/tests/test_*_eval.py` + `/eval` dashboard + JSON snapshots |

The 5 elements every Eval Harness needs (per Anthropic), and where they live in ExpenseFlow:

| Harness element | ExpenseFlow location |
|---|---|
| Data Loader (read cases) | YAML loader at the top of each `test_*_eval.py` |
| Runner (execute each case) | pytest with parametrize; orchestrates seed → call → collect |
| Environment isolation (clean state per run) | Fresh tmp SQLite (`_TMP_DB`) per pytest module |
| Logging / Trace | `LLMTrace` table (model + prompt + response + latency + git commit) |
| Result aggregator | JSON snapshot files (`eval_judge_*_latest.json`) → dashboard |

**Why both vocabularies matter.** Hamel tells you what kind of eval to write; Anthropic tells you how to run it reproducibly at scale. ExpenseFlow uses both — see [`eval-harness-plan.md`](eval-harness-plan.md) for the full architecture and the regression-vs-capability split that operationalises the Anthropic infra against the Hamel principles.

---

## 2. The Three Levels — and where ExpenseFlow stands

Hamel groups evals by what they can verify:

### Level 1 — Unit Tests (deterministic assertions)

> Things you can check with `assert` — no model, no human judgment needed.

| Hamel concept | ExpenseFlow implementation |
|---|---|
| Output shape / schema | `test_agent_eval.py` checks `tool_calls_include` / `response_contains` |
| Required fields present | `test_eval_harness.py` per-component pass-rate |
| Forbidden actions blocked | `test_employee_agent_acl.py` (8 tests) — verifies agent cannot dispatch `submit/approve/reject/pay` |
| Tool whitelist injection | `eval_cases.yaml::whitelist_inject` cases |
| Routing correctness | `eval_cases.yaml::qa_*` — keyword "我这个月花了多少" must call `get_spend_summary` |

**Status: Solid.** This level is well-developed — covers safety-critical invariants (ACL), routing, schema.

**Gaps:**
- [ ] No deterministic check that `audit_report.violations` always contains a `rule_id` field (we ship `agent.*` violations now, never asserted)
- [ ] No deterministic check that auto-rules `evidence_chain` round-trips through JSON → frontend
- [ ] No assertion that `compose_explanation` never returns more than 5 green/yellow/red flags

### Level 2 — Human & Model Eval (subjective quality)

> Things that need a judgment call — quality of explanation, severity of a flag, whether a category suggestion is "right".

The standard process (Hamel):

1. Collect traces (logs of inputs + outputs + tool calls).
2. Pick a sample, **label by hand** (or get a domain expert to).
3. Try LLM-as-judge against the same sample.
4. **Measure the LLM judge's agreement with human labels** (TPR / TNR / κ).
5. If agreement is poor, iterate the *judge prompt* (not the under-test system).
6. Once judge agrees with humans ≥ ~90%, use it to score larger sets.
7. Re-measure agreement periodically — judges drift.

| Hamel concept | ExpenseFlow implementation |
|---|---|
| Trace logging | `LLMTrace` table (`backend/db/store.py:278`), captures component / model / prompt / response / latency / tokens |
| Per-component dataset | `backend/tests/eval_datasets/*.yaml` — 7 datasets: `ambiguity_detector`, `category_classifier`, `fraud_llm_rules`, `fraud_rules_deterministic`, `layer_decision`, `ambiguity_human_labeled`, `fraud_human_labeled` |
| Human-labeled ground truth | `*_human_labeled.yaml` files with `human_label` block (target: 30 cases each, currently placeholders) |
| Component metrics | `EvalRun.component_metrics` JSON: `{component: {正确标记, 误报, 漏报, 正确放行, precision, recall, f1}}` |
| Dashboard for review | `/eval/dashboard.html` |

**Status: Infrastructure ready, datasets sparse.** The plumbing exists; what's missing is the labeled cases.

**Critical gap (Hamel's L2 emphasis):**
- [ ] **No measured judge agreement.** Today the LLM judge in `llm_fraud_analyzer.py` is trusted by default. We don't compute "when human says fraud, does the judge say fraud?" → no TPR/TNR. Without this, judge scores are uncalibrated.
- [ ] Need a `judge_agreement.py` script: load `*_human_labeled.yaml`, run judge, compute confusion matrix, save to `EvalRun.metadata`.

### Level 3 — A/B / Outcome Testing

> Production telemetry that proves the AI is actually moving user behavior — not just looking right in a notebook.

| Hamel concept | ExpenseFlow implementation |
|---|---|
| Outcome metric | **Auto-Approval Funnel KPI** (`/api/eval/auto-approval-rate`) — % of submissions auto-approved (T1+T2) / human review (T3) / rejection (T4) |
| Telemetry events | `TelemetryEvent` table (`backend/db/store.py:229`) — entry / final_layer / fields_edited_count / time_to_attest_ms / attest_or_abandoned |
| Cohort comparison | **Not yet** — there's no "deploy v2 prompt to 10% of traffic" infra |

**Status: Single-arm only.** We track the metric but don't run controlled experiments.

**Gap:**
- [ ] No prompt-version routing (e.g. `prompts.json::active_version` toggles globally — can't split traffic)
- [ ] No client-side experiment assignment (cookie / employee_id mod N)
- [ ] No statistical significance testing on funnel deltas

This is a Phase-2-or-later concern — not a roadmap blocker. Documenting it here so we know we know.

---

## 3. ExpenseFlow's eval surfaces (mapped to Levels)

Where in the product do we run evals against?

### 3.1 OCR — `tool_extract_receipt_fields`

- **L1**: schema (every output has amount/merchant/date/invoice_number)
- **L2**: extraction accuracy (does merchant string match the receipt? human label vs OCR)
- **L3**: `fields_edited_count` from `TelemetryEvent` — proxy for OCR quality (employees correcting OCR)

### 3.2 Category classifier — `tool_suggest_category`

- **L1**: returned category is in `expense_types.yaml::expense_types` allowed list
- **L2**: `category_classifier.yaml` — input merchant → expected category (currently mostly placeholders)
- **L3**: how often does employee accept the AI suggestion (need new telemetry field)

### 3.3 AmbiguityDetector — 5-factor risk scoring

- **L1**: score is in [0, 100]; `triggered_factors` is a subset of the 5 named factors
- **L2**: `ambiguity_human_labeled.yaml` — human says "this should be flagged" vs AmbiguityDetector says
- **L3**: Auto-Approval Funnel KPI — does shifting weights change the auto/review/reject distribution as predicted?

### 3.4 Fraud rules + LLM analyzer

- **L1**: `fraud_rules_deterministic.yaml` — known-bad inputs trigger expected rule_ids
- **L2**: `fraud_human_labeled.yaml` — overall_risk (clean/suspicious/fraud) human vs LLM
- **L3**: ⚠ no production telemetry yet — fraud findings on submitted reports never get a "yes that was actually fraud" or "false alarm" feedback loop

### 3.5 Agent compliance reasoner — `agent.*` violations (recently shipped)

- **L1**: every reasoner finding `kind` has a template in `AGENT_VIOLATIONS` (covered by `test_compliance_reasoner.py::test_every_finding_kind_has_a_template`)
- **L2**: ⚠ **completely missing** — no human-labeled dataset for "should this submission have triggered cross-person meal double-dip flag?" Add `agent_compliance_human_labeled.yaml`.
- **L3**: not yet

### 3.6 AI Explanation Card — `compose_explanation` (manager-facing summary)

- **L1**: returned dict has tier / risk_score / recommendation / advisory
- **L2**: ⚠ **missing** — does the headline actually capture the right risk concern? Subjective; needs human labeling.
- **L3**: manager approve/reject decision time (`telemetry_events`?), whether they overrode the AI's recommendation

### 3.7 Conversational Agent — intent routing + tool dispatch

- **L1**: `eval_cases.yaml` — keyword → expected tool calls (38 cases currently). Plus ACL injection cases.
- **L2**: ⚠ no human label for "did the agent's reply actually answer the question?" 
- **L3**: drawer abandonment rate? not tracked

---

## 4. The Three Gulfs (Hamel × Shankar) — applied to ExpenseFlow

> A diagnostic frame: when an eval fails, *which gulf is the problem in?*

### Gulf of Comprehension

> Does the system understand what the user is asking?

ExpenseFlow examples:
- Employee asks "我这个月花了多少" → does agent understand "this month" = current calendar month vs rolling 30 days? (Currently rolling 30; ambiguous to user.)
- Manager opens an explanation card — what question are they asking the AI? "Should I approve?" or "What's wrong?" — different framings produce different ideal cards.

**Eval response:** intent classification cases + diverse phrasings of the same intent.

### Gulf of Specification

> Can you specify what the right answer looks like?

ExpenseFlow examples:
- "Is this expense ambiguous?" — defined as ambiguity_score > 30 in code, but the Specification gulf is whether the **5 factors with their current weights** capture what humans mean by "ambiguous".
- "Is this fraud?" — `human_label.overall_risk` distinguishes clean/suspicious/fraud, but the boundary between suspicious and fraud is not specified in the policy doc.

**Eval response:** rubrics in `*_human_labeled.yaml::labeler_note` — every example documents WHY a human chose this label. The labeler_note IS the specification.

### Gulf of Generalization

> Does your eval set match the distribution of real production data?

ExpenseFlow examples:
- Eval datasets are mostly Chinese-context (海底捞, 滴滴, 上海). If a customer onboards in Singapore, datasets don't reflect that distribution.
- Fraud datasets focus on patterns observed in synthetic data — production may have entirely different fraud archetypes.

**Eval response:** sample real production traces (`LLMTrace`) into the labeling queue. Don't only label synthetic cases.

---

## 5. Process — how to add a new eval (canonical workflow)

Adapted from Hamel's "How to bootstrap evals":

```
┌─ Step 1 ─ Look at 30+ traces in LLMTrace where the component fired ─┐
│                                                                     │
│   • Pick a component (e.g. agent.travel_during_leave)               │
│   • Filter LLMTrace.component or by submission_id                   │
│   • Read the prompt + response + actual data — full trace           │
│   • Take notes on failure modes you see                             │
└─────────────────────────────────────────────────────────────────────┘
                              ↓
┌─ Step 2 ─ Categorize failure modes ─────────────────────────────────┐
│                                                                     │
│   • False positive: triggered when shouldn't have                   │
│   • False negative: missed something it should catch                │
│   • Wrong attribution: right outcome, wrong reasoning chain         │
│   • Style: correct but unhelpful (jargon / too long / no specifics) │
└─────────────────────────────────────────────────────────────────────┘
                              ↓
┌─ Step 3 ─ Decide which Level each failure belongs to ───────────────┐
│                                                                     │
│   L1 if assertable: shape, schema, named fields, blacklists         │
│   L2 if subjective: "did the AI explain the concern well?"          │
│   L3 if behavioral: "do managers approve this faster?"              │
└─────────────────────────────────────────────────────────────────────┘
                              ↓
┌─ Step 4 ─ Add cases to the appropriate dataset ─────────────────────┐
│                                                                     │
│   L1 → backend/tests/test_*.py (pytest)                             │
│   L2 → backend/tests/eval_datasets/<component>_human_labeled.yaml   │
│        with human_label + labeler_note                              │
│   L3 → log new TelemetryEvent fields, build dashboard slice         │
└─────────────────────────────────────────────────────────────────────┘
                              ↓
┌─ Step 5 ─ Run eval, log to EvalRun ─────────────────────────────────┐
│                                                                     │
│   • python -m pytest backend/tests/test_eval_harness.py             │
│   • Output → EvalRun row with metadata (6-factor) + results JSON    │
│   • Dashboard /eval/dashboard.html shows pass-rate trend            │
└─────────────────────────────────────────────────────────────────────┘
                              ↓
┌─ Step 6 ─ Iterate (prompt / config / code) → re-run ────────────────┐
│                                                                     │
│   • If L2: tweak the COMPONENT's prompt (eval_prompts.json)         │
│   • If L1: fix the code                                             │
│   • If L3: change weights or thresholds in eval_config.json         │
│   • Re-run, EvalRun row #2, diff metadata vs previous run           │
└─────────────────────────────────────────────────────────────────────┘
```

---

## 6. Tools & infrastructure

### What we have

| Tool | Where | Purpose |
|---|---|---|
| `LLMTrace` table | `backend/db/store.py:278` | Single source of truth for every LLM call |
| `EvalRun` table | `backend/db/store.py:298` | Per-run summary with 6-factor metadata |
| Eval Dashboard | `/eval/dashboard.html` | Browse runs, edit prompts, see funnel KPI |
| `code_graders.py` | `backend/tests/graders/` | Custom Python graders for component-specific checks |
| Dashboard prompt editor | `eval_prompts.json` + `/api/eval/prompt-versions` | Versioned prompt changes |
| Auto-Approval Funnel | `/api/eval/auto-approval-rate` | L3 outcome metric |

### What we don't have (Hamel-style gaps)

| Missing | Hamel reasoning | Effort |
|---|---|---|
| Annotation UI for human labelers | "Build tools that make it smooth to examine samples and hand-label them" — Gradio/Streamlit page | 1-2 days |
| LLM-judge agreement script | Without measuring judge↔human agreement, judge scores are uncalibrated noise | 1 day |
| Theoretical-saturation tracker | Hamel: review traces until new ones don't surface new modes (~100). We have no UI marking which traces are "reviewed". | 0.5 day |
| Synthetic data generator (cold start) | "Use a powerful LM to generate test cases" — we don't auto-generate, all hand-written | 2-3 days |
| Diff view across EvalRuns | Already exists at `/api/eval/runs/{id1}/diff/{id2}` ✅ | done |
| Sample-from-prod into labeling queue | Pulling production `LLMTrace` rows into eval datasets | 0.5 day |

---

## 7. Synthetic data — when and how

Hamel: "You don't need extensive product usage to bootstrap evals — go far with an idea of usage patterns and a powerful LM to generate test cases."

ExpenseFlow's stance:

- ✅ Use synthetic for **L1 unit tests** — invariants, ACL, routing keywords. Synthetic gives full coverage of edge cases.
- ✅ Use synthetic for **bootstrap of L2 datasets** — generate 30 candidates, then *humans label them*, not the LM.
- ❌ Don't use synthetic-only for L2 evaluation — the labeler must be human, otherwise the judge is grading itself.
- ❌ Don't use synthetic for L3 — outcome metrics must come from real users.

**Generator pattern (for future):**

```python
# scripts/generate_eval_cases.py (proposed)
prompt = """You are generating evaluation cases for an enterprise expense
detector. Generate 10 expense submissions, each with:
  - A description that should trigger {factor_under_test}
  - A description that looks similar but should NOT trigger it
  Output JSON. Note: do NOT include the expected label —
  a human labeler will fill that in."""
```

The output goes into `*_human_labeled.yaml` with empty `human_label` blocks. Humans fill in `human_label` + `labeler_note` before the case becomes part of the eval set.

---

## 8. LLM-as-Judge — the agreement step we're missing

Hamel's process (verbatim importance):

1. Run LLM judge over a human-labeled sample.
2. Compute confusion matrix: TP / FP / TN / FN against human label.
3. If TPR < ~90% or FPR > ~10%, **iterate the judge prompt**, not the under-test system.
4. Use Cohen's kappa as the headline number — `0.6+` is "substantial", `0.8+` is "almost perfect".
5. Once calibrated, the judge can grade large unlabeled sets.
6. **Re-measure agreement every N runs** — judges drift when the under-test prompts change.

### What this would look like in ExpenseFlow

```python
# backend/tests/test_judge_agreement.py (proposed)

@pytest.mark.asyncio
async def test_fraud_judge_agreement_above_threshold():
    """Locks in the contract: our LLM fraud judge agrees with human
    labels at least 90% of the time. If this drops, we have a judge
    drift problem; do not rely on the judge's scores until re-tuned."""
    cases = load_yaml("eval_datasets/fraud_human_labeled.yaml")
    judge_outputs = [await llm_fraud_analyzer.analyze(c.input) for c in cases]
    human_labels = [c.human_label.overall_risk for c in cases]
    
    agreement = compute_agreement(human_labels, judge_outputs)
    assert agreement.cohens_kappa >= 0.8, (
        f"Judge agreement κ={agreement.cohens_kappa} below threshold; "
        f"{agreement.fp_examples} false positive cases need investigation."
    )
```

**Status:** [ ] not built. **Priority: high** — this is the single most important gap in our L2 setup.

---

## 9. When is enough enough? (theoretical saturation)

Hamel: *"Review traces until new ones don't reveal new failure modes — typically around 100 traces."*

ExpenseFlow operationalizes this:

- For each new component, **first 30 traces** = exploratory labeling. New failure modes get added as new categories.
- Next 30-70 traces = consolidation. If no new failure modes appear in 20 consecutive traces, we've reached saturation for the current prompt version.
- Re-saturate when: (a) prompt version changes, (b) production data drift detected, (c) new customer onboards (different distribution).

**Tracking gap:**
- [ ] No "trace reviewed" flag on `LLMTrace` rows. Add a `reviewed_at` + `reviewed_by` column so the dashboard can show "saturation curve" — failure modes per N traces reviewed.

---

## 10. Anti-patterns (Hamel) — and how we avoid them

| Anti-pattern | Symptom | ExpenseFlow defense |
|---|---|---|
| Vibes-based eval | "I tried 3 inputs, looks fine, ship it" | EvalRun is pre-merge gate; pass-rate must not regress |
| Generic eval framework | Bringing in a third-party metric that doesn't match our problem | We run `code_graders.py` per component, no off-the-shelf rubrics |
| Skipping logging | Can't diagnose because traces aren't kept | `LLMTrace` is required by every LLM-calling site |
| Unmeasured LLM-as-judge | Judge says "70% pass" — but vs what? | Judge agreement (§8) is a Phase-1 priority |
| Static accuracy without inspection | Numbers go up but quality went down | Dashboard always shows trace samples next to numbers |
| Dataset rot | Eval set stops matching prod | Sample-from-prod pipeline (§6 gap) |
| One labeler | Person bias as ground truth | [TBD] policy: each high-stakes case needs ≥2 independent labels |

---

## 11. Open questions / TODO

These are real questions for the team, not framework gaps:

1. **Whose labels are ground truth?** For ambiguity, fraud, and agent compliance — should it be product (PM judgment), finance ops (domain expert), or a panel? Different labelers will disagree on edge cases.
2. **Cost of labeling.** A 30-case dataset takes ~3 hours per component if cases are pre-generated. We have 7 components. Can we ship a labeling sprint?
3. **Labeling tooling.** Inline YAML editing or a real annotation UI? Hamel suggests Gradio in <1 day. Worth it before the 7-component sprint?
4. **L3 instrumentation.** Telemetry for "manager overrode AI recommendation" — single new column on `Submission` (`manager_overrode_ai`) or new event in `TelemetryEvent`?
5. **Per-customer eval profiles.** When multi-entity arrives ([`docs/multi-entity-design.md`](multi-entity-design.md)), do eval datasets need per-entity splits? Probably yes.
6. **Eval for the agent reasoner.** No `agent_compliance_human_labeled.yaml` yet — needs creating + 30 cases for the 3 reasoner kinds (`travel_during_leave`, `claim_vs_allowance`, `cross_person_meal_double_dip`).

---

## 12. Reading order for new contributors

1. **Skim Hamel's original post** — ~30 min (link below). Establishes vocabulary.
2. **This doc** — establishes our adaptation.
3. **`eval_cases.yaml`** — concrete L1 examples.
4. **`eval_datasets/fraud_human_labeled.yaml`** — concrete L2 example with `human_label` + `labeler_note`.
5. **`backend/api/routes/eval.py`** — how runs get logged + diffed.
6. **`/eval/dashboard.html`** — what the eval admin sees.

---

## References

- Hamel Husain, *Your AI Product Needs Evals* (the foundation): <https://hamel.dev/blog/posts/evals/>
- Hamel Husain, *LLM Evals: Everything You Need to Know* (FAQ): <https://hamel.dev/blog/posts/evals-faq/>
- Hamel Husain, *Using LLM-as-a-Judge For Evaluation* (the agreement chapter): <https://hamel.dev/blog/posts/llm-judge/>
- Hamel Husain, *A Field Guide to Rapidly Improving AI Products*: <https://hamel.dev/blog/posts/field-guide/>
- *Three Gulfs* — Husain & Shankar (Maven course frame): referenced in Field Guide

ExpenseFlow internal:
- README "Eval Platform" section
- [`docs/multi-entity-design.md`](multi-entity-design.md)
- [`docs/spec-conflicts.md`](#) — *(planned, see fix #1 in conflict report)*

---

*This doc is a skeleton — sections marked `[TBD]` or `[ ]` need real ExpenseFlow content. The reading order in §12 is the recommended way to grow it: start with placeholders, fill in as you label real cases.*
