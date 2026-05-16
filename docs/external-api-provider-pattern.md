# External API Provider Pattern — Vendor-Agnostic Integration with Traceability

> **Status:** Pattern doc. Codifies how to integrate any external API
> (travel booking, ride-share, credit-card statements, ERP exports) into
> ExpenseFlow with three guarantees: vendor-agnostic, testable without
> the real vendor, every call traceable for audit.
>
> **Companion to:** [`agent-eval-dimensions.md`](agent-eval-dimensions.md)
> (this pattern enables dimensions ④⑤⑥), [`hybrid-fraud-architecture.md`](hybrid-fraud-architecture.md)
> (OODA agent calls these tools), [`integration-design.md`](integration-design.md)
> (ERP-side integrations follow the same shape).
>
> **Use this when:** the agent needs to read or write to a third-party
> system (Ctrip, DiDi, Amadeus, Stripe, NetSuite, 12306, credit-card
> issuer, …) and you don't want vendor coupling, can't always hit the
> real API in tests, and must satisfy audit traceability.

---

## TL;DR

Every external-API integration in ExpenseFlow follows the same 3-layer
shape:

```
┌──────────────────────────────────────────┐
│  Abstract interface (Protocol / ABC)     │  ← the contract
│  e.g. TravelProvider.get_trips(...)      │
└───────────────────┬──────────────────────┘
                    ↑ implemented by
┌─────────┬─────────┴────────┬─────────────┐
│ Mock    │ Sandbox / Real   │ Stub        │  ← swappable impls
│ Provider│ Provider (prod)  │ (raises NIE)│
└─────────┴────────┬─────────┴─────────────┘
                   ↓ wrapped in
┌──────────────────────────────────────────┐
│  TraceableProvider                        │  ← logs every call
│  records request + response + latency     │     to external_api_trace
└───────────────────┬──────────────────────┘
                    ↓ consumed by
┌──────────────────────────────────────────┐
│  OODA agent tool                          │  ← agent uses it
│  e.g. check_travel_booking(...)           │     just like any other tool
└──────────────────────────────────────────┘
```

**Selection** is via env var (`TRAVEL_PROVIDER=mock|amadeus|ctrip`); the
agent code never hardcodes a vendor. **Tracing** is automatic and lands
in a single audit table that the dashboard can replay.

---

## The 3 layers in detail

### Layer 1 — Abstract interface (the contract)

```python
# backend/services/travel_provider.py
from abc import ABC, abstractmethod
from typing import Optional

class Trip(BaseModel):
    employee_id: str
    trip_type: str           # flight / hotel / train / taxi
    departure_date: Optional[date]
    arrival_date: Optional[date]
    origin_city: Optional[str]
    destination_city: Optional[str]
    cost: float
    currency: str
    booking_reference: str
    provider_metadata: dict  # raw vendor blob for debugging

class TravelProvider(ABC):
    @abstractmethod
    async def get_trips(
        self,
        employee_id: str,
        date_range: tuple[date, date],
        *,
        trace_id: Optional[str] = None,
    ) -> list[Trip]:
        """Return all known trips for this employee in the date range."""
```

**Why a Protocol / ABC**: the agent and tool layer only see this
contract. Adding a new vendor = implement this interface. **Removing**
a vendor = delete one file. Zero ripple to agent code.

### Layer 2 — Implementations (swappable)

```python
# Mock — for dev, demos, eval
class MockTravelProvider(TravelProvider):
    """Loads from YAML fixture; deterministic per (employee_id, date_range)."""

    def __init__(self, fixture_path: Path = None):
        self._fixtures = yaml.safe_load(
            (fixture_path or DEFAULT_FIXTURES).read_text()
        )

    async def get_trips(self, employee_id, date_range, *, trace_id=None):
        start, end = date_range
        return [
            Trip(**t) for t in self._fixtures
            if t["employee_id"] == employee_id
            and start <= t["departure_date"] <= end
        ]

# Sandbox — real API but no production side effects
class AmadeusProvider(TravelProvider):
    """Amadeus Self-Service sandbox. Real shape, no real bookings."""
    def __init__(self):
        self._client = amadeus.Client(
            client_id=os.getenv("AMADEUS_CLIENT_ID"),
            client_secret=os.getenv("AMADEUS_CLIENT_SECRET"),
            hostname="test",  # sandbox
        )
    async def get_trips(self, employee_id, date_range, *, trace_id=None):
        # translate to Amadeus API, normalize to Trip
        ...

# Stub — production placeholder that screams loudly
class CtripProvider(TravelProvider):
    """Real Ctrip integration. Not built yet."""
    async def get_trips(self, *args, **kwargs):
        raise NotImplementedError(
            "Ctrip integration is a Phase-3 item. "
            "Use TRAVEL_PROVIDER=mock or amadeus."
        )
```

**The stub matters.** Without `CtripProvider` even as `NotImplementedError`,
nothing in the codebase signals "this vendor was considered." The stub
is the architectural commitment.

### Layer 3 — Traceable wrapper (the audit layer)

```python
# backend/services/traceable_provider.py
import uuid
from contextlib import asynccontextmanager

class TraceableTravelProvider(TravelProvider):
    """Decorator that logs every call to external_api_trace table."""

    def __init__(self, inner: TravelProvider):
        self._inner = inner
        self._provider_name = type(inner).__name__

    async def get_trips(self, employee_id, date_range, *, trace_id=None):
        request = {"employee_id": employee_id,
                   "date_range": [d.isoformat() for d in date_range]}
        timer = TraceTimer()
        try:
            with timer:
                response = await self._inner.get_trips(
                    employee_id, date_range, trace_id=trace_id
                )
            await self._log(
                trace_id=trace_id, request=request,
                response=[t.model_dump() for t in response],
                latency_ms=timer.elapsed_ms, error=None,
            )
            return response
        except Exception as exc:
            await self._log(
                trace_id=trace_id, request=request, response=None,
                latency_ms=timer.elapsed_ms, error=f"{type(exc).__name__}: {exc}",
            )
            raise

    async def _log(self, *, trace_id, request, response, latency_ms, error):
        async with get_session() as db:
            db.add(ExternalAPITrace(
                trace_id=trace_id or str(uuid.uuid4()),
                provider=self._provider_name,
                operation="get_trips",
                request=request,
                response=response,
                latency_ms=latency_ms,
                error=error,
            ))
            await db.commit()
```

**Why wrap rather than mix in**: keeps each impl focused on the contract
(no logging boilerplate per provider). Adding a new metric (e.g., cost
tracking) = update the wrapper once, applies to all providers.

### The factory (selection lives here, nowhere else)

```python
# backend/services/travel_provider.py

_PROVIDER_REGISTRY = {
    "mock":    MockTravelProvider,
    "amadeus": AmadeusProvider,
    "ctrip":   CtripProvider,
}

def get_travel_provider() -> TravelProvider:
    """Resolve the configured provider, wrapped in trace logging."""
    name = os.getenv("TRAVEL_PROVIDER", "mock")
    if name not in _PROVIDER_REGISTRY:
        raise ValueError(
            f"Unknown TRAVEL_PROVIDER={name}. "
            f"Valid: {sorted(_PROVIDER_REGISTRY)}"
        )
    return TraceableTravelProvider(_PROVIDER_REGISTRY[name]())
```

**Single point of truth**. Want to add a Chinese vendor like 飞猪 (Fliggy)?
Add one line to the registry. Want to A/B test two impls? Wrap the factory.

---

## The audit table

```sql
CREATE TABLE external_api_trace (
    id            TEXT    PRIMARY KEY,
    trace_id      TEXT    NOT NULL,   -- ties to submission processing flow
    provider      TEXT    NOT NULL,   -- "MockTravelProvider" / "AmadeusProvider"
    operation     TEXT    NOT NULL,   -- "get_trips" / "get_booking" / etc.
    request       JSON    NOT NULL,
    response      JSON,                -- NULL when error
    latency_ms    INTEGER,
    error         TEXT,                -- NULL on success
    created_at    TIMESTAMP NOT NULL DEFAULT NOW()
);

CREATE INDEX idx_eat_trace_id ON external_api_trace(trace_id);
CREATE INDEX idx_eat_provider_created ON external_api_trace(provider, created_at);
CREATE INDEX idx_eat_error ON external_api_trace(error) WHERE error IS NOT NULL;
```

**What this enables**:

| Use case | Query |
|---|---|
| "What did the agent see when investigating submission X?" | `SELECT * FROM external_api_trace WHERE trace_id = (SELECT trace_id FROM llm_traces WHERE submission_id = X)` |
| "Show me all Ctrip calls that errored this week" | `SELECT * FROM external_api_trace WHERE provider = 'CtripProvider' AND error IS NOT NULL AND created_at > NOW() - 7d` |
| "Replay this call in a test" | Load `request`, pass to the same provider, compare response |
| "P95 latency by provider" | `SELECT provider, percentile_cont(0.95) WITHIN GROUP (ORDER BY latency_ms) FROM external_api_trace GROUP BY 1` |

---

## OODA agent integration

The agent's tool registry sees a clean function; the 3-layer architecture
is invisible to it:

```python
# agent/investigation_tools.py

async def check_travel_booking(employee_id: str, date: str) -> dict:
    """Check Ctrip / Amadeus / mock for trip records on the given date.

    Returns:
        {
            "has_booking": bool,
            "trips": [...],                  # full Trip records
            "conflict_signals": [             # pre-computed flags
                "geo_mismatch_with_submission",
                "no_booking_for_claimed_train_fare",
                ...
            ]
        }
    """
    provider = get_travel_provider()
    target = date.fromisoformat(date)
    trips = await provider.get_trips(
        employee_id, (target, target),
        trace_id=current_trace_id(),  # propagated from OODA loop
    )
    return {
        "has_booking": len(trips) > 0,
        "trips": [t.model_dump() for t in trips],
        "conflict_signals": _detect_conflicts(trips, ...),
    }

INVESTIGATION_TOOLS["check_travel_booking"] = check_travel_booking
```

That's all the OODA agent ever sees. Provider selection, vendor SDK
quirks, trace logging — all hidden.

---

## What this pattern enables for eval

Maps to [`agent-eval-dimensions.md`](agent-eval-dimensions.md) dimensions:

| Dimension | What this pattern unlocks |
|---|---|
| ⑤ Tool-call stability | Mock provider can return errors / empty lists / weird shapes deterministically. Test that agent handles them. |
| ⑥ Conflict detection | Mock fixtures can inject contradictions (Ctrip says Beijing, submission says Shanghai). Test that agent catches them. |
| ④ Prompt injection | Mock can return strings like "Ignore previous instructions" in `description` fields. Test that agent treats them as data. |
| ⑦ Cross-model | Run the same fixture set through 4 LLM backends. The vendor is held constant; the LLM is the variable. |

### Capability case examples this unlocks

```yaml
# eval_capability_fraud_investigator.yaml additions
- id: fri_travel_001_geo_conflict
  description: "Submission claims Beijing trip; Ctrip mock returns Shanghai booking same day"
  fixtures:
    travel_provider: mock_geo_conflict.yaml
  submission:
    date: 2026-06-15
    employee_id: emp_dev
    city: Beijing
    category: accommodation
  expected_verdict: fraud
  must_call_tools: [check_travel_booking]

- id: fri_travel_002_no_booking_for_train_claim
  description: "Train fare claimed; Ctrip mock returns empty"
  fixtures:
    travel_provider: mock_empty.yaml
  submission:
    date: 2026-06-15
    category: transport
    description: "高铁 上海-北京"
    amount: 580
  expected_verdict: suspicious

- id: fri_travel_003_provider_500_error
  description: "Travel API returns 500; agent must fall back gracefully"
  fixtures:
    travel_provider: mock_always_error.yaml
  expected_verdict: suspicious  # NOT clean — failure to verify ≠ verified
  expected_reasoning_contains: "could not confirm via travel provider"
```

Cases like `fri_travel_003` are the **real test** of agent flexibility —
not "does the agent get the right answer when data is clean," but
"does the agent degrade gracefully when its data sources fail."

---

## When NOT to use this pattern

Don't reach for this when:

- **One-off integration with no test concerns** — if you call an external
  API exactly once at startup (e.g., loading a static config), just call
  it inline. Three layers of abstraction is overkill for one call site.
- **The vendor has no equivalent in your dev environment AND no sandbox**
  — without a Mock or Sandbox impl, the pattern degrades to "one Real
  impl plus dead stubs." Build the integration when you can build the
  Mock alongside it.
- **The API mutates external state and you cannot safely mock writes**
  — for write APIs like Stripe transfers or NetSuite journal posts, the
  pattern still helps but you need a **sandbox** that simulates writes,
  not just a fixture-replaying Mock.

---

## Sequencing for adding a new vendor

When adding `XyzProvider` for vendor Xyz:

1. **Day 0 — define the interface** *(if it's a new domain)*
   - Write the ABC + DTO types
   - Skeleton `MockXyzProvider` returning `[]`
   - Skeleton `XyzProvider(NotImplementedError)`
   - Wire factory + env var
2. **Day 1 — Mock is the real deliverable**
   - 10-30 fixture cases covering normal + edge + error
   - Mock provider returns from fixtures deterministically
   - One unit test confirms determinism
3. **Day 2 — agent tool**
   - Wrap provider call in a new `INVESTIGATION_TOOLS` entry
   - One eval case proving the agent calls it correctly
4. **Day 3 — capability cases**
   - 5-7 cases exploiting the Mock's edge fixtures
   - At least one "provider returns error" case
5. **Future — sandbox**
   - When the real vendor has a sandbox tier (Amadeus, Stripe), implement
   - Run capability cases against sandbox to verify Mock fidelity
6. **Future — production**
   - Replace `NotImplementedError` stub with real impl
   - Trace logging already in place (Layer 3 is provider-agnostic)
   - Watch `external_api_trace` for first-week anomalies

Total: **3-4 days to a fully eval-ready new vendor**. Production hookup
is a much smaller increment because the contract / tests / monitoring
are already there.

---

## Pattern checklist

When reviewing a PR that adds an external integration:

- [ ] Abstract interface in `backend/services/{vendor}_provider.py`?
- [ ] Mock impl with fixture file?
- [ ] Stub impl raising `NotImplementedError` for unbuilt vendors?
- [ ] Factory function reads selection from env var?
- [ ] `TraceableXxxProvider` wraps the factory output?
- [ ] `external_api_trace` table is written on every call?
- [ ] At least one eval case uses the Mock?
- [ ] At least one eval case tests provider failure (5xx / empty / malformed)?
- [ ] Vendor SDK leaked into agent code? **(NO — it should not)**

If any "no," the PR needs more work before merge.

---

## What this doc deliberately doesn't do

- **Doesn't dictate which vendors to integrate first.** That's a product
  decision driven by customer demand, not architecture.
- **Doesn't replace `integration-design.md`** — that doc handles the
  ERP-side designs (NetSuite, Stripe Issuing, Excel-as-bridge), which
  follow this same pattern but at the SaaS-to-SaaS boundary not the
  agent-to-vendor boundary.
- **Doesn't promise that any specific provider exists today.** As of
  this writing the only built provider is `MockTravelProvider`
  (when Phase 2 of the travel integration ships). The doc is the
  contract for adding more.
- **Doesn't introduce VCR / cassette recording.** Mock fixtures are
  simpler and don't require ever hitting the real API. If a future
  vendor requires record-replay (e.g., one-time real responses to
  capture), reach for `vcrpy`; until then, fixtures-from-scratch are
  enough.

---

## References

- [`agent-eval-dimensions.md`](agent-eval-dimensions.md) — dimensions
  ④⑤⑥ specifically enabled by this pattern (prompt injection /
  tool-call stability / conflict detection).
- [`hybrid-fraud-architecture.md`](hybrid-fraud-architecture.md) — the
  OODA agent that consumes provider-backed tools.
- [`integration-design.md`](integration-design.md) — ERP-side adapter
  design that mirrors this pattern for outbound integrations.
- [`code-health-audit.md`](code-health-audit.md) — the "name the
  abstraction, don't sprinkle vendor code through endpoints" discipline
  that this pattern operationalizes.
- Anthropic *Building Effective Agents* (2024) — the tool-as-boundary
  model that frames external APIs as just another tool.

---

*This doc is the contract for how ExpenseFlow talks to the outside
world. When asked "how do you integrate with X?" the answer is
"follow this pattern — implement the interface, write a Mock, drop the
factory entry, and the trace + eval surface come for free." That's the
discipline.*
