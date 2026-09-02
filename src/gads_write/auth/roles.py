"""The file-backed tier resolver: config/roles.yaml.

This is TEMPORARY BACKING for the TierResolver interface. In Phase 3 the
Google Ads resolver becomes the default and this file's role shrinks to a
break-glass override, used when Google cannot answer and a lead has
deliberately edited it.

Two things worth understanding:

  Hot reload.  The file is re-read when it changes on disk, so a demotion
               takes effect on the caller's very next request. Caching the
               table at startup would mean a removed operator kept their
               powers until they reconnected, which is exactly the failure
               the "resolve on every call" requirement exists to prevent.

  No accounts. A file cannot express "Standard on account A, Read-only on
               account B" without becoming a worse copy of Google's own
               permission model. So `resolve` ignores customer_id here and
               `visible_tier` returns the same answer. That flatness is a
               limitation of this backing, not of the interface, and it goes
               away in Phase 3.

Python notes for a TypeScript reader:
  - `RoleStore` mirrors `PolicyStore` in safety/policy.py: same mtime-stamp
    reload, same rule that a broken edit keeps the last good version rather
    than crashing a live request or falling back to something permissive.
"""

from __future__ import annotations

import logging
import os
import threading
from dataclasses import dataclass
from pathlib import Path

import yaml

from .tiers import Tier, TierResolver

logger = logging.getLogger(__name__)

# Which backing supplies tiers. `google_ads` is accepted by the parser now so
# that flipping it in Phase 3 is a config change, not a code change.
VALID_MODES = frozenset({"file", "google_ads"})


class RoleConfigError(RuntimeError):
    """Raised when roles.yaml cannot be interpreted."""


@dataclass(frozen=True)
class RoleTable:
    """An immutable snapshot of roles.yaml."""

    mode: str
    users: dict[str, Tier]
    default_tier: Tier
    source_path: Path

    @classmethod
    def parse(cls, raw: object, path: Path) -> "RoleTable":
        if not isinstance(raw, dict):
            raise RoleConfigError(f"roles file {path} must be a YAML mapping")

        mode = str(raw.get("mode", "file")).strip().lower()
        if mode not in VALID_MODES:
            raise RoleConfigError(
                f"roles file {path}: mode {mode!r} is not one of {sorted(VALID_MODES)}"
            )

        default_raw = str(raw.get("default_tier", "none")).lower()
        try:
            default_tier = Tier(default_raw)
        except ValueError as exc:
            raise RoleConfigError(
                f"default_tier {default_raw!r} in {path} is not one of "
                f"{[t.value for t in Tier]}"
            ) from exc

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

        if mode == "file" and Tier.LEAD not in users.values():
            raise RoleConfigError(
                f"roles file {path}: mode is 'file' but no user has tier 'lead'. "
                "At least one lead is required, otherwise nobody can approve the "
                "highest-risk changes."
            )

        return cls(
            mode=mode, users=users, default_tier=default_tier, source_path=path
        )

    @classmethod
    def load(cls, path: Path) -> "RoleTable":
        try:
            text = Path(path).read_text(encoding="utf-8")
        except OSError as exc:
            raise RoleConfigError(f"cannot read roles file {path}: {exc}") from exc
        try:
            raw = yaml.safe_load(text) or {}
        except yaml.YAMLError as exc:
            raise RoleConfigError(f"roles file {path} is not valid YAML: {exc}") from exc
        return cls.parse(raw, Path(path))

    def tier_for(self, email: str | None) -> Tier:
        """Resolve a tier. Unknown or missing email gets the default tier.

        Fails closed: anything unrecognised lands on `default_tier`, which
        ships as "none".
        """
        if not email:
            return self.default_tier
        return self.users.get(email.strip().lower(), self.default_tier)


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


class FileTierResolver(TierResolver):
    """TierResolver backed by roles.yaml. Never raises TierLookupError.

    A local file cannot time out or rate-limit, so there is no indeterminate
    case here. That is another reason it is only temporary backing: it hides
    a failure mode that the real resolver has.
    """

    def __init__(self, store: RoleStore) -> None:
        self._store = store

    async def resolve(self, caller, customer_id: str) -> Tier:  # noqa: ANN001
        # customer_id is accepted and ignored: this backing has no per-account
        # concept. The parameter exists because the INTERFACE needs it, and
        # every caller is written against the interface.
        return self._store.current().tier_for(caller.email)

    async def visible_tier(self, caller) -> Tier:  # noqa: ANN001
        return self._store.current().tier_for(caller.email)

    @property
    def source(self) -> str:
        table = self._store.current()
        return f"roles.yaml (mode={table.mode}, {len(table.users)} users listed)"
