# Industrial-Readiness Roadmap — From Portfolio Demo to Production-Grade SaaS

> **Status:** Forward-compat contract. All 9 gaps documented; **none implemented in current code**.
> **Companion to:** [`hybrid-fraud-architecture.md`](hybrid-fraud-architecture.md), [`evals-reference.md`](evals-reference.md), [`multi-entity-design.md`](multi-entity-design.md), [`customer-segmentation.md`](customer-segmentation.md), [`integration-design.md`](integration-design.md).

---

## TL;DR

ExpenseFlow is currently a **single-tenant, single-entity, single-jurisdiction demo** with a strong AI/eval core. To sell it to a real customer, nine gaps need to close — three the team has been thinking about, six less obvious but more decisive:

| # | Gap | Severity | Effort | Why it's the wall |
|---|---|---|---|---|
| 1 | Org tree + matrix orgs | Medium | 1-2 weeks | Real companies aren't flat |
| 2 | N-level approval chains | Medium | 2-3 weeks | Real approval has 4+ levels w/ delegation |
| 3 | Multi-jurisdiction policy | Medium | 2-3 weeks | Tax/VAT differs per country |
| 4 | **ERP integration** | **HIGH** but **simpler than people think** | 2-4 weeks (Excel bridge) — months (deep API) | Concur's "moat" here is overrated; Excel-as-bridge gets 80% there |
| 5 | **Real payment execution** | **HIGH** | 3-6 months + KYC/AML | The actually hard one |
| 6 | **Multi-tenancy + data isolation** | **CRITICAL** | 4-6 weeks | Without it, can't sell SaaS at all |
| 7 | **Audit + compliance certifications** (SOX, SOC 2) | **CRITICAL** | 6-9 months + ~$50K USD | Enterprise procurement gate |
| 8 | **Operational maturity** (SLA, on-call, observability) | **HIGH** | Continuous | Day-2 problems start here |
| 9 | **Internal code health** (duplication + state machine) | Medium | 1.5-2 weeks (triggered) | 14% duplication compounds; state machine prevents SOC 2 findings |

Total to "credibly sellable to a 100-person company": **6-12 months / 5-7 person team**.

This doc isn't a roadmap we're going to execute — it's the **honest forward-compat contract** that articulates *what we'd do if we did*. The discipline of writing it before building it is itself a portfolio signal: most candidates over-claim; a deferred roadmap with reasoning is more credible than half-built abstractions.

---

## Why "industrial grade" is the right bar

A portfolio demo and an industrial product differ by one question:

> *Could you put this in front of a finance team at a 100-person company tomorrow and have them rely on it for next month's expense reimbursement?*

For ExpenseFlow today, the honest answer is **no** — not because the AI is bad (it's actually good), but because the surrounding **operational + integration + compliance** layer doesn't exist. The 9 gaps below are what fills that layer.

This doc is sequenced so each gap has:
1. **What it actually is** (vs. what people think it is)
2. **Current state** in the codebase
3. **What's needed** to close the gap
4. **Effort estimate** (rough, calibrated against this team)
5. **Decision points** the team has to make
6. **Who to talk to** (reference customers, vendors, advisors)
7. **Dependencies** on other gaps

---

## Gap 1 · Org tree + matrix orgs

### What it is

Real companies aren't flat. A typical 500-person company has:
- **Hierarchical tree**: CEO → VP Eng → Director Backend → Manager Auth → IC
- **Matrix overlay**: same IC also reports dotted-line to Project Lead and (sometimes) to Cost Center owner
- **Lifecycle events**: people get promoted, transferred, take parental leave, leave the company

The expense system needs to know **who currently reports to whom** at the moment a submission goes through approval routing.

### Current state

```python
# backend/db/store.py::Employee
manager_id    = Column(String(64),  nullable=True)   # 直属经理工号
cost_center   = Column(String(50),  nullable=False)
department    = Column(String(100), nullable=False)
level         = Column(String(10),  nullable=True)
```

Flat fields. No tree. No matrix. No lifecycle history.

### What's needed

1. **`org_units` table** — parent_id, name, type (department / team / cost_center), valid_from / valid_to
2. **`employee_assignments` table** — employee_id, org_unit_id, role (primary / dotted-line), valid_from / valid_to
3. **`employee_lifecycle_events` table** — employee_id, event_type (hired / promoted / transferred / left), date, details
4. **Tree traversal helpers** — `get_ancestors(employee_id, at_date)`, `get_subordinates(...)`
5. **UI** — org chart visualization on `/admin/employees.html`; transfer / OOO buttons
6. **Migration** — backfill existing flat `manager_id` into the tree

### Effort

**1-2 weeks** for one engineer. Schema is ~80 lines, the helpers are ~150 lines, UI is the bulk (~300 lines for a working org-chart view).

### Decision points

- **Tree-based vs adjacency list?** Tree (closure table or `ltree`) is faster for ancestor queries; adjacency list is simpler. Recommend closure table for this scale.
- **Matrix with M:N overlay or flat hierarchy only?** M:N covers more cases but doubles complexity. Recommend flat-only for v1, M:N as Phase 2.
- **Backfill date?** When did the existing `manager_id` relationship "start"? Pick a default (today, or company founding date).

### Who to talk to

- Anyone who's worked at a 500+ person company (HR, finance ops, anyone whose name was on an org chart)
- Workday Customer Success (their data model is the de-facto standard for org structures)

### Dependencies

- Multi-entity (Gap 3 in `multi-entity-design.md`) — org tree may span entities. Think through carefully.

---

## Gap 2 · N-level approval chains

### What it is

Today's `approval_flow.yaml` is a single-step matrix: `(amount, employee_level, expense_type) → manager_id`. Real corp policy is:

```
Submit
  ↓
Direct manager approves
  ↓ (if amount > $5K)
Director approves
  ↓ (if amount > $25K)
VP approves
  ↓ (if cost_center ∈ ['ENG-RND','MKT-EVENTS'])
CFO approves
  ↓
Finance ops review
```

Plus:
- **Delegation** — manager OOO → auto-route to delegate
- **Out-of-office** — manager away → 24h escalation to skip-level
- **Conditional routing** — `if expense.category == 'consultancy' → also CTO`
- **Skip / parallel approval** — "any one of [VP-A, VP-B] is enough"
- **Approval reasons** — comment required if rejecting OR if approving above threshold

### Current state

`config/approval_flow.yaml` defines a 2D matrix; one approver per (level, amount). No chains, no delegation, no conditional routing.

### What's needed

1. **`approval_chain_rules` config** — DSL for chain expressions (e.g. `if amount > 5000: route to direct_manager.manager`)
2. **`approvals` table** — submission_id, step_index, approver_id, decided_at, decision (approve / reject / delegate), comment
3. **`delegations` table** — delegator_id, delegate_id, valid_from / valid_to, reason
4. **Chain executor** — walks the rules, creates pending approvals, escalates on timeout
5. **UI** — manager queue shows "Approval chain: you are step 2 of 4"; delegation toggle; OOO calendar
6. **Notifications** — email / Slack / in-app when an approval lands in your queue

### Effort

**2-3 weeks** for one engineer. The hard part isn't writing the executor (~400 lines), it's the DSL design — getting it expressive enough for real policies but not so flexible that admins shoot themselves in the foot.

### Decision points

- **DSL vs UI rule builder?** A YAML DSL is dev-friendly but ops-hostile. A no-code rule builder is ops-friendly but takes 10x to build. Recommend DSL for v1, rule builder when 10+ customers.
- **Sequential vs parallel?** All-sequential is simpler. Some org policies require parallel (any-of). Recommend sequential v1, parallel v2.
- **Hard timeout vs soft warning?** "Manager hasn't acted in 48h, auto-escalate" vs "auto-remind manager 3 times". Recommend soft warning, with hard cap at 7 days.

### Who to talk to

- Finance ops at a 200-300 person company (sweet spot: complex enough to need chains, small enough to talk to one person)
- Approval-engine vendors (Camunda, Flowable) for inspiration, NOT integration — full BPMN engines are overkill

### Dependencies

- Org tree (Gap 1) — chain steps reference manager-of-manager, which requires the tree
- Notifications (part of operational maturity, Gap 8)

---

## Gap 3 · Multi-jurisdiction policy

### What it is

A US employee submitting in USD with US sales tax is one rule set. The same company's UK office submitting in GBP with VAT is a different rule set. Same employee transferring to Singapore for a project is yet another.

Real policy must vary by:
- **Country** — VAT rate, tax-deductibility rules, receipt format
- **Currency + FX** — when is FX rate locked? At submit, approve, or pay?
- **Time** — VAT rate changes mid-year (e.g. UK changed VAT rate in 2010, 2011, 2020)
- **Entity** (covered separately in `multi-entity-design.md`)

### Current state

`config/policy.yaml` has limits + city tiers + GL mapping. Single jurisdiction (China, implicit). No time versioning, no per-country tax math.

### What's needed

1. **`policy_versions` table** — policy_id, jurisdiction, version, effective_from, effective_to, content (JSONB)
2. **`tax_rules` table** — jurisdiction, category, rate, deductible, evidence_required (e.g. China VAT special invoice)
3. **Policy lookup** — `get_policy(employee.entity, expense.date)` returns the version effective at that date
4. **VAT math helpers** — apply tax rules per submission, store derived `vat_amount` + `deductible_amount`
5. **Audit trail** — every submission records WHICH policy version was applied (so re-runs match historical decisions)

### Effort

**2-3 weeks** for the schema + lookup helpers + first 2 jurisdictions (China + US). Each additional jurisdiction is 2-3 days for a developer who knows the local tax rules.

### Decision points

- **Hardcode tax rules vs externalize?** Externalize. They change.
- **Apply tax at submit vs at pay?** At submit (locks the policy version) — re-runs reproduce history.
- **Allow ad-hoc overrides?** Yes for finance, with audit trail. Audit will look for it.

### Who to talk to

- Tax accountant who's filed in 2+ jurisdictions
- A finance ops person who's lived through "VAT rate changed mid-year and our system didn't update for 3 weeks" pain

### Dependencies

- Multi-entity (Gap 3 of `multi-entity-design.md`) — entity determines jurisdiction
- ERP integration (Gap 4) — tax math output feeds GL postings

---

## Gap 4 · ERP integration

### What it is

> Reality check, prompted by recent discussion: **Concur's ERP integration isn't actually that great**. The deep two-way sync is over-engineered for 80% of customers. Most clients export from Concur, manually adjust in Excel, then import to ERP anyway. So the right design might be **Excel-as-bridge**, not deep API.

ERP integration means: when a payment is approved, the journal entry shows up in the company's ledger (SAP / NetSuite / Oracle / Workday Financials / QuickBooks / 用友 / 金蝶 / etc.) without a human re-typing it.

Two design philosophies:

### Path A · Excel-as-bridge (recommended for SMB)

```
ExpenseFlow → "ERP-ready CSV" download
              → Finance person opens in Excel
              → Sanity check / adjust
              → Imports to ERP via ERP's standard import format
```

**Pros**: works with EVERY ERP day 1; no per-ERP integration code; finance team retains control (they don't trust black-box sync).
**Cons**: manual step; doesn't scale past ~500 transactions/month.

### Path B · Direct API integration (for enterprise)

```
ExpenseFlow → ERP REST/SOAP API → journal entry posted, returns voucher_id
              ← reconciliation: bank txn matched to voucher
```

**Pros**: zero manual work; real-time; reconciliation closes the loop.
**Cons**: Each ERP needs its own integration (NetSuite, SAP, Oracle, Workday — months each); customer's ERP-admin must grant API access; fails complicated when ERP rejects (bad GL code, wrong fiscal period closed, etc.).

### Current state

`backend/api/routes/finance.py` has `/finance/export.html` that produces a CSV. That CSV is an ad-hoc format, NOT mapped to any ERP's import schema.

### What's needed (Path A · Excel-as-bridge)

1. **NetSuite-format CSV export** — match NetSuite's "Import Vendor Bills" CSV schema column-for-column
2. **SAP-format CSV export** — match SAP's BAPI-style CSV import (different schema)
3. **用友 / 金蝶 format** — match standard 凭证导入 CSV used in China
4. **Per-customer profile** — admin picks "which ERP we use" → export defaults to that schema
5. **Field mapping UI** — show preview of CSV before download, let finance map our `gl_account` to their `account_code`

### What's needed (Path B · Deep API, Phase 2)

1. **NetSuite SuiteTalk REST integration** — auth (TBA / OAuth 2.0), Vendor Bill creation, error handling, retry queue
2. **Reconciliation worker** — periodically pull bank statements, match `voucher_number` ↔ bank txn, flag mismatches
3. **Webhook receiver** — NetSuite posts events back when a voucher is approved/posted/voided

### Effort

- Path A: **2-4 weeks** for 3 ERP formats (NetSuite, SAP standard, generic CSV)
- Path B: **2-4 months PER ERP**, plus ongoing maintenance

### Decision points

- **Ship Path A first, Path B for enterprise customers only.** This is a controversial take — Concur built Path B and it's their biggest sales argument. But:
  - Their Path B is buggy in practice (talk to anyone who's done a Concur → ERP go-live)
  - Path A gets you 80% value at 5% effort
  - Customer can graduate to Path B later if volume justifies
- **Which ERPs to prioritize?** Depends on customer segment ([`customer-segmentation.md`](customer-segmentation.md)).

### Who to talk to

- A finance ops person who's done a Concur → NetSuite go-live (will tell you everything that goes wrong)
- A NetSuite implementation consultant ($$$) — get a free hour over coffee
- For China: a 财务 who's used 用友 NC / 金蝶 EAS

### Dependencies

- Multi-entity (so we know which entity's GL chart to map to)
- Audit (every export must be logged for SOX trail)

### Detailed integration spec

See [`integration-design.md`](integration-design.md) for the NetSuite + Stripe Issuing concrete API designs.

---

## Gap 5 · Real payment execution

### What it is

Today: when finance "approves" a submission, the system marks it `paid` in our DB and exports a CSV. Money doesn't actually move.

Production: money has to actually go from company bank → employee's bank account, with all the stuff that entails:
- KYC / AML verification (we'd be moving money)
- Bank rails (ACH / SEPA / SWIFT / 中国央行清算)
- Reconciliation (did the payment land?)
- Failure handling (insufficient funds, wrong account, fraud hold)
- Refund / clawback flow (employee resigns mid-pay-cycle)

### Current state

```python
# skills/skill_05_payment.py
def execute_payment(submission):
    # Mock — just marks status='paid'
    submission.status = 'paid'
```

Pure mock. Doesn't even attempt real payment.

### What's needed

Two paths, depending on geography:

**Path 1 · US/EU — Stripe Issuing + Treasury**
- Issue Stripe-backed corporate cards to employees (no reimbursement needed; they spend on company card)
- For reimbursement-on-submission: Stripe Treasury push to employee bank
- KYC handled by Stripe; we relay employee identity

**Path 2 · China — direct bank API integration**
- Each major bank (招行 / 中行 / 工行 / 平安) has different B2B API
- 网联 / 银联 unified clearing — possible but enterprise-only
- Most pragmatic: use a 第三方支付 like 合合 / 汇付 / 连连 as broker

### Effort

**3-6 months MINIMUM**, dominated by:
- KYC/AML compliance (we become a money mover, regulatory burden goes from "nothing" to "everything")
- Bank API integration (1 month per bank, plus contracts)
- Reconciliation infrastructure
- Failure handling (1-3% of payments fail in real life; each failure mode needs its own UI/comms flow)

### Decision points

- **Become a money mover ourselves vs route through Stripe/合合?** Almost always route. Becoming licensed is years.
- **Issue cards vs reimburse after-the-fact?** Cards are the future (Brex, Ramp, Airwallex — they all do this). Reimbursement is legacy.
- **Geographic scope?** Single market first. Pick one. US (Stripe) is easiest to start.

### Who to talk to

- Stripe Issuing PM / sales (free, eager)
- Brex / Ramp PM (won't talk to a competitor)
- Anyone who's run a Treasury / Card-issuing program (very rare expertise; ~$500/hr consultant)

### Dependencies

- Multi-tenancy (Gap 6) — payment per tenant, money flow per tenant
- Audit (Gap 7) — every payment is a SOX-relevant event

---

## Gap 6 · Multi-tenancy + data isolation

### What it is

Today, ExpenseFlow has ONE database. Every employee, every submission, every audit log is in the same SQLite file. There's no concept of "Company A vs Company B".

To sell SaaS, every API request must be **scoped to the requesting tenant**, with no possibility of cross-tenant leakage.

### Three patterns (industry standard)

| Pattern | Pros | Cons | Use when |
|---|---|---|---|
| **Pool model** (row-level isolation, `tenant_id` on every table) | Cheap; one DB to manage | One bug = data leak; queries must be carefully scoped | SMB / freemium tier |
| **Bridge model** (one DB, schema-per-tenant) | Better isolation; backup / restore per tenant | More schemas to manage; migrations complex | Mid-market |
| **Silo model** (one DB per tenant) | Highest isolation; meets data-residency rules | Expensive; hard to operate at scale | Enterprise / regulated |

### Current state

Zero. `Submission.employee_id` exists but is global. No `tenant_id` field anywhere. `init_db()` seeds demo data into the singleton DB.

### What's needed

1. **`tenants` table** — id, name, plan, created_at, settings_json, region
2. **`tenant_id` column on every table** — submissions, reports, employees, audit_logs, all of them
3. **Tenant-scoping middleware** — every API request must declare its tenant; every query must filter by it
4. **Tenant context in JWT** — Clerk/Auth0 puts tenant_id in claims
5. **Per-tenant config** — org tree, policy, approval flow, all loaded per tenant
6. **Tenant onboarding flow** — sign up → create tenant → invite first user
7. **Tenant termination flow** — data export, then deletion (GDPR right to be forgotten)

### Effort

**4-6 weeks** for pool model with row-level isolation. The work is touching every table + every query, plus testing every cross-tenant boundary doesn't leak. Recommend pool model for v1.

### Decision points

- **Pattern (pool / bridge / silo)?** Pool for v1 unless first customer demands silo (= enterprise).
- **Per-tenant DB encryption?** Defer until enterprise.
- **Region/data-residency?** Pick one (US) for v1; add EU when first EU customer.

### Who to talk to

- Anyone who's been responsible for "the multi-tenant migration" at a B2B SaaS startup (this is a common project)
- AWS / GCP solutions architect about per-tenant encryption + KMS

### Dependencies

- Auth (Clerk integration) needs to populate tenant_id in JWT
- Audit (Gap 7) — audit log per-tenant or global with tenant_id?

---

## Gap 7 · Audit + compliance certifications

### What it is

When you sell to a 500-person company's procurement team, they ask for:
- **SOC 2 Type II** report (US, B2B SaaS standard)
- **ISO 27001** certificate (international, slightly more enterprise)
- **GDPR / CCPA** compliance posture (regulatory)
- **SOX** controls evidence (if customer is publicly listed)
- **Penetration test** report (annual)
- **Vendor security questionnaire** (300+ questions)

Without these, you don't get past procurement. The product can be perfect; doesn't matter.

### Current state

Zero certifications. Some audit trail in `AuditLog` table, but not designed against any specific standard.

### What's needed

1. **SOC 2 Type II prep** (~6 months):
   - Written policies (data security, access control, change management, vendor mgmt, incident response, etc.) — ~30 documents
   - Evidence collection (every change goes through code review, every access is logged, etc.)
   - SOC 2 audit firm engagement (~$30-50K USD/year)
2. **Immutable audit log** — append-only, signed (so you can't backdate)
3. **Access control reviews** — quarterly attestation that everyone with access still needs it
4. **Encryption at rest + in transit** — pgcrypto + TLS everywhere
5. **Secret management** — AWS Secrets Manager / HashiCorp Vault, no secrets in code
6. **Incident response runbook** — what we do when X happens
7. **Vendor risk assessments** — every third-party (OpenAI, Stripe, OCR vendor) goes through review
8. **Backup + DR** — RPO / RTO defined and tested

### Effort

**6-9 months** end-to-end for SOC 2 Type II first audit:
- 3 months: policy writing + control implementation
- 3 months: evidence collection period (Type II requires showing controls operating)
- 3 months: audit fieldwork + report

Cost: ~$50K USD year 1, ~$30K/year ongoing.

### Decision points

- **SOC 2 first or ISO 27001 first?** SOC 2 if US-first; ISO 27001 if global.
- **Use a compliance automation platform (Vanta / Drata / Secureframe) or DIY?** Use a platform — they cut SOC 2 prep from 6 months to 3.
- **At what customer count to start?** Pre-sale customer #5 typically. Customer #1 might accept "we're working on it"; customer #5 won't.

### Who to talk to

- Vanta / Drata / Secureframe sales (will give you a "SOC 2 readiness checklist" for free)
- Any startup CTO who's been through SOC 2 (will save you months)

### Dependencies

- Multi-tenancy (Gap 6) — audit log scoping
- Operational maturity (Gap 8) — incident response, on-call

---

## Gap 8 · Operational maturity

### What it is

The software runs. Things go wrong. Customers expect:
- **Uptime SLA** — typically 99.5% to 99.95% depending on tier
- **Monitoring** — you know there's a problem before customers tell you
- **On-call rotation** — someone responds within 15 minutes during business hours
- **Incident postmortems** — when stuff breaks, you write up what happened
- **Status page** — public dashboard customers can check
- **Runbooks** — for common alerts, what to do

### Current state

ExpenseFlow has zero operational instrumentation. No metrics, no traces, no alerts, no on-call. If the demo crashed during a portfolio review, you'd find out from the reviewer.

### What's needed

1. **Application observability** — Sentry (errors) + Datadog or Prometheus (metrics) + structured logs
2. **Synthetic monitoring** — Pingdom / UptimeRobot — basic "is the site up?"
3. **On-call rotation** — PagerDuty / Opsgenie — even just one person, but defined
4. **Runbooks** — written response procedures for top 10 alerts
5. **Incident review template** — for every P1/P2, write up what happened
6. **Status page** — Statuspage / Better Uptime
7. **DB performance baseline** — slow query log, index audit
8. **Async job infrastructure** — Celery + Redis or RQ — current codebase blocks the API thread for OCR / pipeline

### Effort

**Continuous, but ~3 weeks initial setup**:
- Week 1: Sentry + structured logging + DB metrics
- Week 2: synthetic monitoring + status page + runbooks for 3 key alerts
- Week 3: async job migration (5-Skill pipeline → background worker)

Then ongoing: every incident → improve a runbook; every quarter → review SLO.

### Decision points

- **Self-host vs SaaS observability?** SaaS (Sentry / Datadog). Don't be a DBA.
- **What SLO?** Match what you can support. 99.5% = ~3.6h downtime/month (loose); 99.95% = ~22min/month (tight). Start at 99.5%.
- **On-call comp?** People hate being on-call without comp. Either pay or rotate weekly with light-touch alerts.

### Who to talk to

- Anyone who's been on-call at a B2B SaaS for 6+ months (will save you months of pain)
- Sentry + Datadog sales (free during evaluation)

### Dependencies

- All other gaps. Operational maturity is a horizontal — applies to everything.

---

## Gap 9 · Internal code health (duplication + coupling)

### What it is

Production-grade code is read more than it's written. Industrial customers don't see code, but their consequences do — a 9-place copy-paste means adding a new approval status takes a week and ships a bug; a missing state machine means audit-log inconsistency becomes a SOC 2 finding.

### Current state

ExpenseFlow has **~14% duplication** in application code (≈500 of 3,500 lines), concentrated in 5 hotspots:

1. **9 status-change endpoints** repeating the same load → check → mutate → audit → notify pattern (35-40% repeated logic)
2. **3 frontend list+detail pages** (`my-reports.html` / `queue.html` / `review.html`) with 70-85% function similarity, no shared module
3. **2 chat tool pairs** (`get_report_detail` vs `get_submission_for_review`) with ~60% serialization overlap
4. **54 hardcoded status strings** across 5 files, no enum
5. **15+ ad-hoc audit timeline writes** with ~45% boilerplate, no event helper

Detailed evidence + refactor paths live in [`code-health-audit.md`](code-health-audit.md).

### Root causes (from the audit)

1. **Role modeled as layout boundary, not policy boundary** — separate `frontend/{employee,manager,finance}/` folders physically duplicate the same domain object's renderer with different action buttons. Concur/Airwallex/Ramp use one list view + role-conditional buttons.
2. **Status machine doesn't exist** — 9 endpoints reimplement the same 5-step transition pattern. SAP rewrote Concur's approval engine for the same reason after acquisition.
3. **No "duplication budget" mechanism** — Rule of Three (refactor at the 3rd instance) wasn't enforced; multiple hotspots have 3-9 instances without the trigger ever firing.

### What's needed

In refactor-payback order (do early ones first; later ones depend on them):

1. **Status enum + state machine** — single `StrEnum` for all 11 status values + `transition(resource, from, to, actor, audit_message)` helper. Subsumes Hotspots 1 and 4. **0.5-1 day**, single PR.
2. **Audit event helper** — `audit_event(actor, action, resource, *, detail, phase)` replacing 15+ hand-built `f-string + dict` blocks. Falls out almost free once #1 lands. **1 hour**.
3. **Submission serializer** — `serialize_submission(sub, *, scope: "self" | "approver")` replacing the two divergent chat tools. **2 hours**.
4. **Frontend list module** — `frontend/shared/reports-table.js` (or a `<reports-table>` Web Component) used by all three role pages. Highest visible payoff, biggest effort. **1 day**.
5. **Domain status module** — `backend/domain/status.py` central + a transition graph declaring legal moves. Done concurrently with #1. **0.5 day**.

Total budget: **1.5-2 weeks of one engineer's time** for all 5 hotspots.

### Effort

**Triggered, not scheduled.** Each hotspot has a specific payback trigger — see [`code-health-audit.md`](code-health-audit.md) "Refactor budget" section. Examples:

- Hotspot 1: trigger is the **10th approval endpoint** OR the next added status
- Hotspot 2: trigger is the **4th list view** (auditor read-only, rejected archive, etc.)
- Hotspot 3: trigger is the **3rd serialization context** (admin export / partner API)

**Don't refactor for aesthetics.** Refactor when the duplication starts costing real time on real PRs.

### Decision points

- **Refactor in-place vs greenfield rewrite?** In-place. The duplication is local, not architectural. Greenfield burns 6 weeks for a 2-week problem.
- **Stop adding features until the audit is paid back?** No. The 14% waste is tolerable through Phase 1 (first paying customer). It becomes unacceptable in Phase 2 (multi-customer onboarding).
- **Block PRs that add to a hotspot?** Yes, after Phase 1. PR template grows a "did you add to a known hotspot?" checkbox; if yes, refactor must be in the same PR.

### Who to talk to

- Anyone who's done a B2B SaaS state-machine refactor (SAP / Concur / Workday alums — they've all done this exact migration)
- A senior frontend engineer for Hotspot 2 (Web Components vs ES module trade-off is real)
- An SRE / DevOps engineer for #5 (audit event format must survive SOC 2 evidence collection — see Gap 7)

### Dependencies

- **Gap 7 (SOC 2)** — audit log inconsistency from Hotspot 5 becomes a finding. Pay this back before SOC 2 audit window opens.
- **Gap 8 (Operational maturity)** — structured logging is harder when status strings are scattered. Hotspot 4 makes log analysis painful.
- **Gap 1 (Org tree)** — adding "manager-of-manager" approvals adds endpoints. Don't add the 10th endpoint without first paying back Hotspot 1.

---

## Sequencing — if you actually do all this, in what order?

```
Phase 0 (current state, demo)
  ✅ AI core (5-Skill, AmbiguityDetector, OODA agent)
  ✅ Eval framework (Cohen's κ, Hamel methodology)
  ✅ UI (employee submit, manager approval, finance review)
  ✅ Documentation (4 design docs)

Phase 1 — first paying customer (3 months)
  → Multi-tenancy (Gap 6)             [4-6 weeks, BLOCKING]
  → ERP CSV export — Path A only (Gap 4) [2 weeks]
  → Operational basics (Gap 8 abridged) [3 weeks]
  → Real Clerk auth (currently mock)
  → Org tree v1 minimum (Gap 1)        [1 week — flat, no matrix]

Phase 2 — first 5 paying customers (6 months)
  → SOC 2 Type II prep + audit (Gap 7)   [6-9 months overlapping]
  → N-level approval chains (Gap 2)      [3 weeks]
  → Multi-jurisdiction policy (Gap 3)    [3 weeks per region]
  → Real payment via Stripe (Gap 5)      [3 months]
  → Operational maturity full (Gap 8)    [continuous]
  → Pay back code-health hotspots (Gap 9) [1.5-2 weeks, triggered by 10th endpoint or 4th list view]

Phase 3 — enterprise tier (12+ months)
  → Direct ERP API integration (Gap 4 Path B)  [3+ months per ERP]
  → Matrix orgs, M:N assignments (Gap 1 v2)
  → Silo-model multi-tenancy option for regulated customers
  → ISO 27001 (in addition to SOC 2)
```

**Critical path** = Multi-tenancy → SOC 2 → first enterprise customer. Everything else fits around.

**What never gets built**: deep API integration with every regional ERP. Not worth it. Excel-as-bridge stays as the answer for long tail.

---

## What this doc deliberately doesn't say

- **It's not a project plan.** No team is staffed. No budget is allocated.
- **It's not a sales claim.** When asked "is this production-ready?" the answer is "no, here's what's deferred and why" — not "yes".
- **It's not exhaustive.** I haven't covered: i18n beyond zh/en, mobile app, accessibility (WCAG), data residency for EU, customer success operations, billing infrastructure, sales tooling, partner ecosystem. Each is real but lower priority than the 8 above.

---

## References

- [`hybrid-fraud-architecture.md`](hybrid-fraud-architecture.md) — what's built (the AI core)
- [`evals-reference.md`](evals-reference.md) — eval discipline
- [`multi-entity-design.md`](multi-entity-design.md) — Gap 3-adjacent (per-entity policy overrides)
- [`customer-segmentation.md`](customer-segmentation.md) — which segment to target first
- [`integration-design.md`](integration-design.md) — concrete NetSuite + Stripe Issuing API designs
- [`code-health-audit.md`](code-health-audit.md) — Gap 9 evidence: 5 duplication hotspots with refactor paths and payback triggers

---

*This doc is a contract. When the team chooses to ship industrial-grade, it executes against this map. When asked "why isn't X built?" the answer is "see this doc." That's the discipline.*
