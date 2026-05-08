"""Domain layer — status enums + transition rules + audit helpers.

Owns the invariants that should not be repeated across endpoints. Per
CLAUDE.md Protocol 4, this is the start of the gradual migration from
CRUD-shaped (status strings sprinkled through every endpoint) to
domain-shaped (status mutations go through ``transitions.transition()``).

This package is import-only — no FastAPI, no SQLAlchemy session, no I/O.
The transition function takes the current status + new status + actor
and returns the next status if legal, else raises ``IllegalTransition``.
Persistence is the caller's concern.
"""
