"""Audit log: redaction, structure, and readability after a crash."""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

from gads_write.safety.audit import AuditLog, AuditRecord, local_date_for, redact


def _record(**overrides) -> AuditRecord:
    base = dict(
        user_email="lead@example.com",
        tool="update_campaign_budget",
        verdict="allowed",
        account_timezone="Asia/Kolkata",
        arguments={"campaign_id": "111", "new_daily_budget": 1200},
        applied=True,
        customer_id="1234567890",
        plan_id="abc123",
        spend_delta_units="200",
        resource_names=["customers/1234567890/campaignBudgets/999"],
    )
    base.update(overrides)
    return AuditRecord.build(**base)


# ---------------------------------------------------------------------------
# redaction
# ---------------------------------------------------------------------------

def test_credential_shaped_keys_are_redacted() -> None:
    cleaned = redact(
        {
            "access_token": "ya29.live-token",
            "refreshToken": "1//refresh",
            "client_secret": "GOCSPX-x",
            "Authorization": "Bearer abc",
            "api_key": "k",
            "campaign_id": "111",
        }
    )
    assert cleaned["campaign_id"] == "111"
    for key in ("access_token", "refreshToken", "client_secret", "Authorization", "api_key"):
        assert cleaned[key] == "[redacted]"


def test_redaction_reaches_into_nested_structures() -> None:
    cleaned = redact({"outer": {"inner": [{"password": "hunter2"}]}})
    assert cleaned["outer"]["inner"][0]["password"] == "[redacted]"


def test_long_strings_are_capped() -> None:
    cleaned = redact({"note": "x" * 2000})
    assert len(cleaned["note"]) < 600
    assert "more chars" in cleaned["note"] or "chars]" in cleaned["note"]


def test_long_lists_are_capped() -> None:
    cleaned = redact({"keywords": [f"kw{i}" for i in range(200)]})
    assert len(cleaned["keywords"]) <= 51


def test_redaction_is_applied_by_build_not_left_to_callers() -> None:
    record = _record(arguments={"refresh_token": "1//secret", "campaign_id": "111"})
    assert record.arguments["refresh_token"] == "[redacted]"
    assert "1//secret" not in record.to_json_line()


def test_unserialisable_values_become_strings_rather_than_failing() -> None:
    # An audit write must never fail because of an odd argument type.
    line = _record(arguments={"path": Path("/tmp/x"), "when": datetime(2026, 1, 1)}).to_json_line()
    assert json.loads(line)["arguments"]["path"]


# ---------------------------------------------------------------------------
# structure
# ---------------------------------------------------------------------------

def test_record_has_every_required_field() -> None:
    parsed = json.loads(_record().to_json_line())
    for key in (
        "timestamp",
        "local_date",
        "user_email",
        "tool",
        "arguments",
        "plan_id",
        "verdict",
        "dry_run",
        "applied",
        "resource_names",
        "customer_id",
        "spend_delta_units",
    ):
        assert key in parsed, f"missing {key}"


def test_timestamp_is_utc_but_local_date_is_the_account_timezone() -> None:
    # 20:00 UTC on the 1st is 01:30 on the 2nd in Kolkata. The daily ceiling
    # must roll over on the account's clock, not UTC's.
    moment = datetime(2026, 1, 1, 20, 0, tzinfo=timezone.utc)
    record = AuditRecord.build(
        user_email="a@b.com",
        tool="t",
        verdict="allowed",
        account_timezone="Asia/Kolkata",
        now=moment,
    )
    assert record.timestamp.startswith("2026-01-01T20:00")
    assert record.local_date == "2026-01-02"


def test_unknown_timezone_falls_back_to_utc_rather_than_crashing() -> None:
    moment = datetime(2026, 1, 1, 20, 0, tzinfo=timezone.utc)
    assert local_date_for(moment, "Not/AZone") == "2026-01-01"


# ---------------------------------------------------------------------------
# file round trip
# ---------------------------------------------------------------------------

def test_append_and_read_back(tmp_path: Path) -> None:
    log = AuditLog(tmp_path / "nested" / "audit.jsonl")
    log.append(_record())
    log.append(_record(tool="pause_campaign"))

    records = list(log.iter_records())
    assert [r["tool"] for r in records] == [
        "update_campaign_budget",
        "pause_campaign",
    ]


def test_a_truncated_final_line_does_not_break_reading(tmp_path: Path) -> None:
    # A process killed mid-write leaves a partial line. The ledger still has
    # to be rebuildable from what survived.
    path = tmp_path / "audit.jsonl"
    log = AuditLog(path)
    log.append(_record())
    with path.open("a", encoding="utf-8") as handle:
        handle.write('{"tool": "update_camp')

    records = list(log.iter_records())
    assert len(records) == 1


def test_reading_a_missing_file_is_empty_not_an_error(tmp_path: Path) -> None:
    assert list(AuditLog(tmp_path / "none.jsonl").iter_records()) == []


def test_filter_by_local_date(tmp_path: Path) -> None:
    log = AuditLog(tmp_path / "audit.jsonl")
    log.append(_record(now=datetime(2026, 1, 1, 6, 0, tzinfo=timezone.utc)))
    log.append(_record(now=datetime(2026, 1, 2, 6, 0, tzinfo=timezone.utc)))

    assert len(list(log.iter_records(local_date="2026-01-01"))) == 1


# ---------------------------------------------------------------------------
# retention: the log has to look after itself
# ---------------------------------------------------------------------------
# This server is meant to run unattended. A log that needs a human to rotate
# it every few months is a log that eventually fills a disk, and a logrotate
# rule written by someone who does not know the daily spend ceiling is
# derived from this file would silently reset every user's allowance.


def test_records_past_the_retention_window_are_pruned_automatically(
    tmp_path: Path,
) -> None:
    """No cron, no logrotate, no human. Appending is enough to keep it tidy."""
    log = AuditLog(
        tmp_path / "audit.jsonl",
        retention_days=30,
        clock=lambda: datetime(2026, 3, 1, 12, 0, tzinfo=timezone.utc),
    )

    log.append(_record(now=datetime(2026, 1, 1, 6, 0, tzinfo=timezone.utc)))
    log.append(_record(now=datetime(2026, 2, 28, 6, 0, tzinfo=timezone.utc)))

    assert list(log.iter_records(local_date="2026-01-01")) == []
    assert len(list(log.iter_records(local_date="2026-02-28"))) == 1


def test_a_pre_partition_audit_file_is_still_read(tmp_path: Path) -> None:
    """Deploying the partitioned log must not lose the history it inherits.

    The daily spend ceiling is derived from this log. If an existing
    audit.jsonl stopped being read, everyone's allowance would silently reset
    to zero-used on the deploy - the exact failure the partitioning is meant
    to prevent.
    """
    moment = datetime(2026, 1, 1, 6, 0, tzinfo=timezone.utc)
    legacy = tmp_path / "audit.jsonl"
    legacy.write_text(
        _record(now=moment, spend_delta_units="200").to_json_line() + "\n",
        encoding="utf-8",
    )

    log = AuditLog(legacy)
    log.append(_record(tool="pause_campaign", now=moment))

    tools = [r["tool"] for r in log.iter_records(local_date="2026-01-01")]
    assert "update_campaign_budget" in tools, "inherited history was dropped"
    assert "pause_campaign" in tools, "newly written record was dropped"
