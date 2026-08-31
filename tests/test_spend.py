"""Daily spend ceiling, derived from the audit log.

The property that matters most here is the last test: restarting the process
must not clear someone's daily allowance.
"""

from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path

from gads_write.safety.audit import AuditLog, AuditRecord
from gads_write.safety.spend import DailySpendLedger

TODAY = "2026-01-15"
NOON_UTC = datetime(2026, 1, 15, 4, 0, tzinfo=timezone.utc)  # 09:30 Kolkata


def _log(tmp_path: Path) -> AuditLog:
    return AuditLog(tmp_path / "audit.jsonl")


def _entry(log: AuditLog, **overrides) -> None:
    base = dict(
        user_email="operator@example.com",
        tool="update_campaign_budget",
        verdict="allowed",
        account_timezone="Asia/Kolkata",
        now=NOON_UTC,
        applied=True,
        spend_delta_units="500",
    )
    base.update(overrides)
    log.append(AuditRecord.build(**base))


def test_sums_applied_increases_for_one_user(tmp_path: Path) -> None:
    log = _log(tmp_path)
    _entry(log, spend_delta_units="500")
    _entry(log, spend_delta_units="300")

    ledger = DailySpendLedger(log)
    assert ledger.total_increase_units(
        user_email="operator@example.com", local_date=TODAY
    ) == Decimal(800)


def test_other_users_do_not_count_against_you(tmp_path: Path) -> None:
    log = _log(tmp_path)
    _entry(log, spend_delta_units="500")
    _entry(log, user_email="someone.else@example.com", spend_delta_units="9000")

    ledger = DailySpendLedger(log)
    assert ledger.total_increase_units(
        user_email="operator@example.com", local_date=TODAY
    ) == Decimal(500)


def test_decreases_do_not_create_headroom(tmp_path: Path) -> None:
    """The subtle one.

    If a decrease offset an increase, lowering campaign A by 5000 would fund
    raising campaign B by 5000 past the cap - net zero on paper, one campaign
    spending double.
    """
    log = _log(tmp_path)
    _entry(log, spend_delta_units="1000")
    _entry(log, spend_delta_units="-5000")

    ledger = DailySpendLedger(log)
    assert ledger.total_increase_units(
        user_email="operator@example.com", local_date=TODAY
    ) == Decimal(1000)


def test_refused_changes_consume_no_allowance(tmp_path: Path) -> None:
    log = _log(tmp_path)
    _entry(log, verdict="denied", applied=False, spend_delta_units="4000")
    _entry(log, spend_delta_units="200")

    ledger = DailySpendLedger(log)
    assert ledger.total_increase_units(
        user_email="operator@example.com", local_date=TODAY
    ) == Decimal(200)


def test_drafted_but_never_applied_consumes_no_allowance(tmp_path: Path) -> None:
    log = _log(tmp_path)
    _entry(log, applied=False, spend_delta_units="4000")

    ledger = DailySpendLedger(log)
    assert ledger.total_increase_units(
        user_email="operator@example.com", local_date=TODAY
    ) == Decimal(0)


def test_yesterday_does_not_count_against_today(tmp_path: Path) -> None:
    log = _log(tmp_path)
    _entry(log, now=datetime(2026, 1, 14, 4, 0, tzinfo=timezone.utc), spend_delta_units="9000")
    _entry(log, spend_delta_units="100")

    ledger = DailySpendLedger(log)
    assert ledger.total_increase_units(
        user_email="operator@example.com", local_date=TODAY
    ) == Decimal(100)


def test_headroom(tmp_path: Path) -> None:
    log = _log(tmp_path)
    _entry(log, spend_delta_units="800")

    snapshot = DailySpendLedger(log).snapshot(
        user_email="operator@example.com", local_date=TODAY
    )
    assert snapshot.headroom(Decimal(3000)) == Decimal(2200)
    assert snapshot.headroom(Decimal(500)) == Decimal(0)  # never negative
    assert snapshot.change_count == 1


def test_a_malformed_delta_does_not_hand_back_allowance(tmp_path: Path) -> None:
    log = _log(tmp_path)
    _entry(log, spend_delta_units="1000")
    _entry(log, spend_delta_units="not-a-number")

    ledger = DailySpendLedger(log)
    assert ledger.total_increase_units(
        user_email="operator@example.com", local_date=TODAY
    ) == Decimal(1000)


def test_the_ceiling_survives_a_restart(tmp_path: Path) -> None:
    """`pm2 restart` must not be a way to clear your daily cap.

    A new ledger over the same log file sees the same total, because the log
    is the only source of truth. An in-memory counter would read zero here.
    """
    log = _log(tmp_path)
    _entry(log, spend_delta_units="2500")

    before = DailySpendLedger(log).total_increase_units(
        user_email="operator@example.com", local_date=TODAY
    )

    restarted_log = AuditLog(tmp_path / "audit.jsonl")
    after = DailySpendLedger(restarted_log).total_increase_units(
        user_email="operator@example.com", local_date=TODAY
    )

    assert before == after == Decimal(2500)
