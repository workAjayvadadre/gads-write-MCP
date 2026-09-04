"""The break-glass tier overrides, and nothing else.

Tiers come from Google Ads. This file exists for one situation: Google cannot
tell us someone's role - an outage, or the still-open question of whether a
non-admin can read their own `customer_user_access` row - and the team is
locked out with no way back in but a developer redeploy.

An entry here says: "Google could not tell us this person's role, and a lead
has deliberately overridden it." It short-circuits Google entirely.

Three things follow from that, and they are why this stayed a FILE rather
than moving into .env with everything else:

  - It is a list of privilege grants, and `.env` is gitignored. In git it has
    a history, shows up in review, and a forgotten entry is visible. In .env
    none of that is true.
  - It hot-reloads. Break-glass is for the moment you are already locked out;
    a file edit applies on the next request, an env var needs a restart.
  - A map of emails to roles squeezed into one env string is easy to mistype,
    and a mistype there fails closed but silently.

An entry is not a privilege escalation, and that is worth knowing before
worrying about a stale one. Every Google Ads call still uses that person's
OWN OAuth token, so if Google has removed them, Google refuses the read or
write whatever tier we assigned. An override changes which tools appear in
their menu and which tier lands in the audit line; it cannot grant access to
an account Google will not let them touch.

THE FILE IS OPTIONAL. Normally it does not exist, and its absence means "no
overrides" - the ordinary, healthy state. Its presence is itself the signal
that something is wrong and wants undoing.

There is no `mode` any more. Tiers always come from Google Ads; for local
development without credentials, put yourself in this map, which is the same
path a real break-glass takes.
"""

from __future__ import annotations

import logging
import os
import threading
from dataclasses import dataclass
from pathlib import Path

import yaml

from .tiers import Tier

logger = logging.getLogger(__name__)

class RoleConfigError(RuntimeError):
    """Raised when roles.yaml cannot be interpreted."""


@dataclass(frozen=True)
class RoleTable:
    """An immutable snapshot of the override file. Usually empty."""

    users: dict[str, Tier]
    source_path: Path

    @classmethod
    def empty(cls, path: Path) -> "RoleTable":
        """No file, so no overrides. The ordinary state."""
        return cls(users={}, source_path=Path(path))

    @classmethod
    def parse(cls, raw: object, path: Path) -> "RoleTable":
        if raw is None:
            return cls.empty(path)
        if not isinstance(raw, dict):
            raise RoleConfigError(f"roles file {path} must be a YAML mapping")

        users_raw = raw.get("users") or {}
        if not isinstance(users_raw, dict):
            raise RoleConfigError(f"roles.users in {path} must be a mapping")

        users: dict[str, Tier] = {}
        for email, tier_value in users_raw.items():
            normalised = str(email).strip().lower()
            try:
                users[normalised] = Tier(str(tier_value).strip().lower())
            except ValueError as exc:
                raise RoleConfigError(
                    f"tier {tier_value!r} for {email!r} in {path} is not one of "
                    f"{[t.value for t in Tier]}"
                ) from exc

        return cls(users=users, source_path=Path(path))

    @classmethod
    def load(cls, path: Path) -> "RoleTable":
        """Read the file, or return an empty table if there is none.

        A missing file is NOT an error. Overrides are the exception; having
        none is the healthy state, and requiring the file would mean shipping
        an empty one just to say "nothing to see here".
        """
        path = Path(path)
        if not path.exists():
            return cls.empty(path)
        try:
            text = path.read_text(encoding="utf-8")
        except OSError as exc:
            raise RoleConfigError(f"cannot read roles file {path}: {exc}") from exc
        try:
            raw = yaml.safe_load(text)
        except yaml.YAMLError as exc:
            raise RoleConfigError(f"roles file {path} is not valid YAML: {exc}") from exc
        return cls.parse(raw, path)

    def override_for(self, email: str | None) -> Tier | None:
        """The deliberate override for this person, or None to ask Google.

        None means "no override", never "no access" - the difference matters,
        because the caller falls through to the real resolver on None.
        """
        if not email:
            return None
        return self.users.get(email.strip().lower())


class RoleStore:
    """Holds the current role table and reloads it when the file changes."""

    def __init__(self, path: Path) -> None:
        self._path = Path(path)
        self._lock = threading.Lock()
        # A failure here is fatal: refusing to start beats starting with an
        # unknown permission model.
        self._table = RoleTable.load(self._path)
        self._stamp = self._file_stamp()
        self._last_error: str | None = None
        self._reload_count = 0

    def _file_stamp(self) -> tuple[int, int] | None:
        try:
            stat = os.stat(self._path)
        except OSError:
            return None
        return (stat.st_mtime_ns, stat.st_size)

    def current(self) -> RoleTable:
        stamp = self._file_stamp()
        if stamp is None or stamp == self._stamp:
            return self._table

        with self._lock:
            if stamp == self._stamp:
                return self._table
            try:
                table = RoleTable.load(self._path)
            except Exception as exc:  # noqa: BLE001 - outcome, not type
                # Keep the last good table. A broken edit must not grant
                # anyone anything, and must not crash a live request.
                #
                # Broader than RoleConfigError on purpose, for the same
                # reason as PolicyStore: an unanticipated exception type
                # would otherwise reach a live request while last_error
                # stayed empty and /healthz went on reporting "ok".
                self._last_error = str(exc)
                self._stamp = stamp
                logger.error("roles reload REFUSED, keeping previous table: %s", exc)
                return self._table

            self._table = table
            self._stamp = stamp
            self._last_error = None
            self._reload_count += 1
            logger.info("roles reloaded from %s", self._path)
            return self._table

    @property
    def path(self) -> Path:
        return self._path

    @property
    def last_error(self) -> str | None:
        return self._last_error

    @property
    def reload_count(self) -> int:
        return self._reload_count
