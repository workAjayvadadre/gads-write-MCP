"""Per-user daily spend ceiling, derived from the audit log.

The design decision worth explaining: this ledger keeps **no state of its
own**. It answers "how much has this person raised budgets by today?" by
reading today's audit lines every time it is asked.

The obvious alternative is an in-memory counter. It is faster and it is
wrong, for one reason: a counter resets when the process restarts, so
`pm2 restart` becomes a way to clear the daily cap. Deriving the total from
the append-only log means the ceiling survives restarts, deploys, and
crashes, and there is exactly one source of truth to reason about.

Two rules that are easy to get wrong:

  Only increases count. A decrease does not create headroom. Otherwise
  lowering campaign A by 5000 would fund raising campaign B by 5000 past the
  cap, netting to zero while doubling one campaign's spend.

  Only applied changes count. A refused or drafted-but-never-confirmed
  change consumes none of the allowance.

Performance: this scans today's lines on each check. For a team of this size
that is a few hundred lines - microseconds. If the log ever grows enough for
that to matter, Phase 6 adds a daily index; do not pre-optimise it into a
cache that can drift from the log.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal, InvalidOperation

from .audit import AuditLog


@dataclass(frozen=True)
class SpendSnapshot:
    """What one user has already used of today's allowance."""

    user_email: str
    local_date: str
    total_increase_units: Decimal
    change_count: int

    def headroom(self, ceiling_units: Decimal) -> Decimal:
        """How much more they may raise budgets by today. Never negative."""
        remaining = ceiling_units - self.total_increase_units
        return remaining if remaining > 0 else Decimal(0)


class DailySpendLedger:
    """Reads the audit log to answer daily-ceiling questions."""

    def __init__(self, audit_log: AuditLog) -> None:
        self._audit = audit_log

    def snapshot(self, *, user_email: str, local_date: str) -> SpendSnapshot:
        email = user_email.strip().lower()
        total = Decimal(0)
        count = 0

        for record in self._audit.iter_records(local_date=local_date):
            if not record.get("applied"):
                continue
            if record.get("verdict") != "allowed":
                continue
            if str(record.get("user_email", "")).strip().lower() != email:
                continue

            raw_delta = record.get("spend_delta_units")
            if raw_delta in (None, ""):
                continue
            try:
                delta = Decimal(str(raw_delta))
            except (InvalidOperation, ValueError):
                # A malformed delta must not silently reduce the total and
                # hand back allowance. Skip it; the line is still in the log
                # for a human to find.
                continue

            if delta > 0:
                total += delta
                count += 1

        return SpendSnapshot(
            user_email=email,
            local_date=local_date,
            total_increase_units=total,
            change_count=count,
        )

    def total_increase_units(self, *, user_email: str, local_date: str) -> Decimal:
        return self.snapshot(user_email=user_email, local_date=local_date).total_increase_units
