"""Append-only audit log. One JSON object per line, one line per decision.

Every guard decision is logged, not only the ones that go through. A refused
change is often the more interesting record: it is how you notice someone
repeatedly trying to push past a cap, or that a limit is set too tight for
the team to do their job.

The file is JSON Lines so it can be tailed, grepped, and shipped to a log
service without a parser. Phase 6 wires up shipping and alerting.

Two hard rules:
  - Never write a token, secret, or credential. `redact()` runs over every
    argument dict before it is serialised, and it is applied by
    `AuditRecord.build`, not left to each caller to remember.
  - Never let an audit failure take down a request... except when the write
    is about to happen. See `AuditLog.append`.

Python notes for a TypeScript reader:
  - `zoneinfo` is the stdlib timezone database (Python 3.9+). On Windows it
    needs the `tzdata` package, which is why that is a dependency.
  - Opening a file in "a" mode and writing a single line is atomic enough
    for our purposes on a single process; PM2 runs exactly one instance.
"""

from __future__ import annotations

import json
import logging
from collections.abc import Iterator
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone as dt_timezone
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

logger = logging.getLogger(__name__)

# Any dict key containing one of these substrings has its value replaced.
# Substring matching on purpose: it catches `access_token`, `refreshToken`,
# `client_secret`, `X-Api-Key` and anything else shaped like a credential.
SENSITIVE_KEY_PARTS = (
    "token",
    "secret",
    "password",
    "passwd",
    "credential",
    "authorization",
    "auth",
    "api_key",
    "apikey",
    "private",
)

REDACTED = "[redacted]"

# Caps so one absurd argument cannot produce a multi-megabyte audit line.
MAX_STRING_CHARS = 500
MAX_SEQUENCE_ITEMS = 50


def redact(value: Any, *, _depth: int = 0) -> Any:
    """Recursively strip anything credential-shaped and cap the size.

    Errs towards over-redacting. An audit line that says [redacted] where it
    did not need to is a minor annoyance; one that contains a live OAuth
    token is an incident.
    """
    if _depth > 6:
        return "[too deeply nested]"

    if isinstance(value, dict):
        cleaned: dict[str, Any] = {}
        for key, item in value.items():
            key_text = str(key)
            if any(part in key_text.lower() for part in SENSITIVE_KEY_PARTS):
                cleaned[key_text] = REDACTED
            else:
                cleaned[key_text] = redact(item, _depth=_depth + 1)
        return cleaned

    if isinstance(value, (list, tuple)):
        items = [redact(v, _depth=_depth + 1) for v in value[:MAX_SEQUENCE_ITEMS]]
        if len(value) > MAX_SEQUENCE_ITEMS:
            items.append(f"[+{len(value) - MAX_SEQUENCE_ITEMS} more]")
        return items

    if isinstance(value, str):
        if len(value) > MAX_STRING_CHARS:
            return value[:MAX_STRING_CHARS] + f"[+{len(value) - MAX_STRING_CHARS} chars]"
        return value

    if isinstance(value, (int, float, bool)) or value is None:
        return value

    # Decimal, Path, enums, dataclasses and anything else become their string
    # form so json.dumps can never fail on an audit write.
    return str(value)


@dataclass
class AuditRecord:
    """One decision. Written verbatim as a single JSON line."""

    timestamp: str            # ISO 8601, UTC, always
    local_date: str           # YYYY-MM-DD in the account timezone
    user_email: str
    tool: str
    verdict: str              # "allowed" | "denied" | "error"
    dry_run: bool
    applied: bool
    arguments: dict[str, Any] = field(default_factory=dict)
    customer_id: str | None = None
    plan_id: str | None = None
    denied_reasons: list[str] = field(default_factory=list)
    resource_names: list[str] = field(default_factory=list)
    # Currency units, as a string so Decimal precision survives the round
    # trip. Read back by safety/spend.py to rebuild the daily ceiling.
    spend_delta_units: str | None = None
    result: dict[str, Any] | None = None
    error: str | None = None

    @classmethod
    def build(
        cls,
        *,
        user_email: str,
        tool: str,
        verdict: str,
        account_timezone: str = "UTC",
        now: datetime | None = None,
        arguments: dict[str, Any] | None = None,
        **rest: Any,
    ) -> "AuditRecord":
        """Construct a record with redaction and timestamps already applied.

        Callers do not get the chance to forget to redact.
        """
        moment = now or datetime.now(dt_timezone.utc)
        if moment.tzinfo is None:
            moment = moment.replace(tzinfo=dt_timezone.utc)

        return cls(
            timestamp=moment.astimezone(dt_timezone.utc).isoformat(),
            local_date=local_date_for(moment, account_timezone),
            user_email=user_email,
            tool=tool,
            verdict=verdict,
            dry_run=bool(rest.pop("dry_run", False)),
            applied=bool(rest.pop("applied", False)),
            arguments=redact(arguments or {}),
            **rest,
        )

    def to_json_line(self) -> str:
        return json.dumps(asdict(self), ensure_ascii=False, sort_keys=True)


def local_date_for(moment: datetime, account_timezone: str) -> str:
    """The calendar date in the account's timezone.

    The daily spend ceiling resets at midnight where the accounts live, not
    at midnight UTC. Getting this wrong would reset the cap at 5:30am local
    for an India-timezone account.
    """
    try:
        zone = ZoneInfo(account_timezone)
    except (ZoneInfoNotFoundError, ValueError):
        logger.warning(
            "unknown timezone %r in policy.yaml, falling back to UTC for the "
            "daily spend ceiling",
            account_timezone,
        )
        zone = dt_timezone.utc
    return moment.astimezone(zone).date().isoformat()


class AuditLog:
    """Append-only JSONL writer and reader."""

    def __init__(self, path: Path) -> None:
        self._path = Path(path)
        self._path.parent.mkdir(parents=True, exist_ok=True)

    @property
    def path(self) -> Path:
        return self._path

    def append(self, record: AuditRecord) -> None:
        """Write one line and flush it.

        This raises on failure rather than swallowing the error. If we cannot
        record what we are about to do, we do not do it: an unlogged mutation
        is worse than a refused one, because nobody can find it afterwards.
        The caller decides how to surface that.
        """
        line = record.to_json_line()
        with self._path.open("a", encoding="utf-8") as handle:
            handle.write(line + "\n")
            handle.flush()

    def iter_records(self, *, local_date: str | None = None) -> Iterator[dict[str, Any]]:
        """Read records back, optionally filtered to one local date.

        Malformed lines are skipped with a warning rather than raising. A
        process killed mid-write can leave a truncated final line, and that
        must not stop the daily ceiling from being rebuilt.
        """
        if not self._path.exists():
            return

        with self._path.open("r", encoding="utf-8") as handle:
            for number, line in enumerate(handle, start=1):
                text = line.strip()
                if not text:
                    continue
                try:
                    record = json.loads(text)
                except json.JSONDecodeError:
                    logger.warning(
                        "skipping malformed audit line %s:%d", self._path, number
                    )
                    continue
                if local_date is None or record.get("local_date") == local_date:
                    yield record
