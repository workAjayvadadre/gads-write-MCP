"""Load, hot-reload, and evaluate config/policy.yaml.

Two responsibilities, deliberately kept apart:

  1. `PolicyStore` owns the file and its reloading. It is the only mutable
     thing here.
  2. `Policy` is an immutable snapshot, and the `evaluate_*` functions are
     pure. Given the same snapshot and the same inputs they always return
     the same verdict, which is what makes boundary testing meaningful.

Hot reload contract, which matters because your marketing lead edits this
file on a live server:

  - A valid edit takes effect on the next call. No restart.
  - An INVALID edit is refused. The last known-good policy stays in force
    and the error is logged. We never fall back to permissive defaults and
    we never crash a request because someone mistyped YAML.
  - A guard evaluation reads the snapshot ONCE and uses it throughout, so a
    save that lands mid-evaluation cannot produce a half-old, half-new
    verdict.

Python notes for a TypeScript reader:
  - `Decimal` again for money. Never float.
  - `frozenset` is an immutable Set. Using it makes accidental mutation of
    a shared snapshot a TypeError rather than a silent policy change.
"""

from __future__ import annotations

import logging
import os
import threading
from dataclasses import dataclass, replace
from decimal import Decimal
from pathlib import Path
from typing import Any

import yaml

from ..auth.tiers import Tier
from .units import MoneyError, coerce_units, percent_change

logger = logging.getLogger(__name__)

SUPPORTED_VERSION = 2

# Tiers that may never change anything, enforced in code rather than trusted
# to config. See parse_policy.
NON_WRITING_TIERS = (Tier.NONE, Tier.READONLY)


class PolicyError(RuntimeError):
    """Raised when policy.yaml cannot be interpreted."""


# ---------------------------------------------------------------------------
# immutable snapshot
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class BudgetLimits:
    min_daily_units: Decimal
    max_daily_units: Decimal
    max_increase_percent: Decimal
    max_total_increase_per_user_per_day_units: Decimal


@dataclass(frozen=True)
class BidLimits:
    max_cpc_units: Decimal
    max_increase_percent: Decimal


@dataclass(frozen=True)
class TierLimits:
    budget: BudgetLimits
    bids: BidLimits


@dataclass(frozen=True)
class Rules:
    new_entities_start_paused: bool
    block_broad_match_with_manual_cpc: bool
    allowed_final_url_domains: frozenset[str]


@dataclass(frozen=True)
class Policy:
    version: int
    allowed_customer_ids: frozenset[str]
    currency_code: str
    timezone: str
    rules: Rules
    blocked_operations: frozenset[str]
    plan_ttl_seconds: int
    plan_single_use: bool
    _limits_by_tier: dict[str, TierLimits]

    def limits_for(self, tier: Tier) -> TierLimits:
        """Limits for a tier, with defaults already merged in."""
        try:
            return self._limits_by_tier[tier.value]
        except KeyError as exc:  # pragma: no cover - guarded at load time
            raise PolicyError(f"no limits configured for tier {tier.value!r}") from exc

    def allows_customer(self, customer_id: str) -> bool:
        return str(customer_id).strip() in self.allowed_customer_ids

    def blocks_operation(self, operation: str) -> bool:
        return str(operation).strip() in self.blocked_operations


# ---------------------------------------------------------------------------
# parsing
# ---------------------------------------------------------------------------

def _require(mapping: dict[str, Any], key: str, where: str) -> Any:
    if key not in mapping:
        raise PolicyError(f"{where}: missing required key {key!r}")
    return mapping[key]


def _money(value: Any, where: str) -> Decimal:
    try:
        amount = coerce_units(value, field=where)
    except MoneyError as exc:
        raise PolicyError(str(exc)) from exc
    if amount < 0:
        raise PolicyError(f"{where}: must not be negative, got {amount}")
    return amount


def _deep_merge(base: dict[str, Any], override: dict[str, Any]) -> dict[str, Any]:
    """Merge `override` onto `base`, key by key, recursing into dicts.

    An override that names only `max_daily` leaves the other budget keys at
    their default. That is what makes the tier blocks in policy.yaml short.
    """
    merged = dict(base)
    for key, value in override.items():
        if isinstance(value, dict) and isinstance(merged.get(key), dict):
            merged[key] = _deep_merge(merged[key], value)
        else:
            merged[key] = value
    return merged


def _parse_tier_limits(raw: dict[str, Any], where: str) -> TierLimits:
    budget_raw = _require(raw, "budget", where)
    bids_raw = _require(raw, "bids", where)

    budget = BudgetLimits(
        min_daily_units=_money(_require(budget_raw, "min_daily", where), f"{where}.budget.min_daily"),
        max_daily_units=_money(_require(budget_raw, "max_daily", where), f"{where}.budget.max_daily"),
        max_increase_percent=_money(
            _require(budget_raw, "max_increase_percent", where),
            f"{where}.budget.max_increase_percent",
        ),
        max_total_increase_per_user_per_day_units=_money(
            _require(budget_raw, "max_total_increase_per_user_per_day", where),
            f"{where}.budget.max_total_increase_per_user_per_day",
        ),
    )
    if budget.min_daily_units > budget.max_daily_units:
        raise PolicyError(
            f"{where}.budget: min_daily ({budget.min_daily_units}) is above "
            f"max_daily ({budget.max_daily_units}), which would block every change"
        )

    bids = BidLimits(
        max_cpc_units=_money(_require(bids_raw, "max_cpc", where), f"{where}.bids.max_cpc"),
        max_increase_percent=_money(
            _require(bids_raw, "max_increase_percent", where),
            f"{where}.bids.max_increase_percent",
        ),
    )
    return TierLimits(budget=budget, bids=bids)


def parse_policy(raw: Any, *, source: str = "policy") -> Policy:
    """Turn parsed YAML into a validated, immutable Policy.

    Every problem raises. A policy that is half-understood is more dangerous
    than no policy, because it looks like it is protecting you.
    """
    if not isinstance(raw, dict):
        raise PolicyError(f"{source}: must be a YAML mapping")

    version = _require(raw, "version", source)
    if version != SUPPORTED_VERSION:
        raise PolicyError(
            f"{source}: version is {version!r}, this code understands "
            f"{SUPPORTED_VERSION}. Refusing to guess at the structure."
        )

    customer_ids = _require(raw, "allowed_customer_ids", source)
    if not isinstance(customer_ids, list) or not customer_ids:
        raise PolicyError(
            f"{source}.allowed_customer_ids: must be a non-empty list. "
            "There is no wildcard."
        )
    normalised_ids: set[str] = set()
    for entry in customer_ids:
        text = str(entry).strip()
        if not text.isdigit() or len(text) != 10:
            raise PolicyError(
                f"{source}.allowed_customer_ids: {entry!r} is not a 10-digit "
                "customer ID (digits only, no dashes)"
            )
        normalised_ids.add(text)

    limits_raw = _require(raw, "limits", source)
    defaults_raw = _require(limits_raw, "defaults", f"{source}.limits")
    if not isinstance(defaults_raw, dict):
        raise PolicyError(f"{source}.limits.defaults: must be a mapping")

    tier_overrides = limits_raw.get("tiers") or {}
    if not isinstance(tier_overrides, dict):
        raise PolicyError(f"{source}.limits.tiers: must be a mapping")
    for name in tier_overrides:
        if name not in {t.value for t in Tier}:
            raise PolicyError(
                f"{source}.limits.tiers: {name!r} is not a known tier "
                f"{sorted(t.value for t in Tier)}"
            )

    # `none` and `readonly` are pinned to zero below, in code. Their blocks
    # in policy.yaml are documentation, so they are not parsed at all --
    # otherwise a documentation-only block could fail boot on a rule that
    # does not apply to it (a `max_daily: 0` override tripping the
    # min-above-max check while min_daily was inherited from defaults).
    limits_by_tier: dict[str, TierLimits] = {}
    for tier in Tier:
        if tier in NON_WRITING_TIERS:
            continue
        override = tier_overrides.get(tier.value) or {}
        if not isinstance(override, dict):
            raise PolicyError(f"{source}.limits.tiers.{tier.value}: must be a mapping")
        merged = _deep_merge(defaults_raw, override)
        limits_by_tier[tier.value] = _parse_tier_limits(
            merged, f"{source}.limits[{tier.value}]"
        )

    # `none` and `readonly` must never permit a spend change, whatever the
    # file says. This is enforced in CODE rather than trusted to config: a
    # policy.yaml that simply omits these blocks would otherwise have them
    # silently inherit the permissive defaults, which is precisely the
    # failure a test caught while this module was being written.
    #
    # The blocks in config/policy.yaml are documentation. These lines are
    # the enforcement.
    denied_everything = TierLimits(
        budget=BudgetLimits(
            min_daily_units=Decimal(0),
            max_daily_units=Decimal(0),
            max_increase_percent=Decimal(0),
            max_total_increase_per_user_per_day_units=Decimal(0),
        ),
        bids=BidLimits(max_cpc_units=Decimal(0), max_increase_percent=Decimal(0)),
    )
    for non_writing_tier in NON_WRITING_TIERS:
        limits_by_tier[non_writing_tier.value] = denied_everything

    rules_raw = _require(raw, "rules", source)
    domains = rules_raw.get("allowed_final_url_domains") or []
    if not isinstance(domains, list):
        raise PolicyError(f"{source}.rules.allowed_final_url_domains: must be a list")

    rules = Rules(
        new_entities_start_paused=bool(
            _require(rules_raw, "new_entities_start_paused", f"{source}.rules")
        ),
        block_broad_match_with_manual_cpc=bool(
            _require(rules_raw, "block_broad_match_with_manual_cpc", f"{source}.rules")
        ),
        allowed_final_url_domains=frozenset(str(d).strip().lower() for d in domains),
    )

    plans_raw = _require(raw, "plans", source)
    ttl_seconds = int(_require(plans_raw, "ttl_seconds", f"{source}.plans"))
    if ttl_seconds <= 0:
        raise PolicyError(f"{source}.plans.ttl_seconds: must be positive")

    blocked = raw.get("blocked_operations") or []
    if not isinstance(blocked, list):
        raise PolicyError(f"{source}.blocked_operations: must be a list")

    return Policy(
        version=int(version),
        allowed_customer_ids=frozenset(normalised_ids),
        currency_code=str(_require(raw, "currency_code", source)).strip(),
        timezone=str(raw.get("timezone", "UTC")).strip() or "UTC",
        rules=rules,
        blocked_operations=frozenset(str(op).strip() for op in blocked),
        plan_ttl_seconds=ttl_seconds,
        plan_single_use=bool(plans_raw.get("single_use", True)),
        _limits_by_tier=limits_by_tier,
    )


def load_policy_file(path: Path) -> Policy:
    try:
        text = path.read_text(encoding="utf-8")
    except OSError as exc:
        raise PolicyError(f"cannot read policy file {path}: {exc}") from exc
    try:
        raw = yaml.safe_load(text)
    except yaml.YAMLError as exc:
        raise PolicyError(f"policy file {path} is not valid YAML: {exc}") from exc
    return parse_policy(raw, source=str(path))


# ---------------------------------------------------------------------------
# hot-reloading store
# ---------------------------------------------------------------------------

class PolicyStore:
    """Holds the current policy and reloads it when the file changes.

    Thread-safe. FastMCP serves concurrent requests, and a reload must not
    be visible to a request halfway through evaluating a change.
    """

    def __init__(self, path: Path) -> None:
        self._path = Path(path)
        self._lock = threading.Lock()
        # A load failure at construction is fatal: refusing to start beats
        # starting with no limits.
        self._policy = load_policy_file(self._path)
        self._stamp = self._file_stamp()
        self._last_error: str | None = None
        self._reload_count = 0

    def _file_stamp(self) -> tuple[int, int] | None:
        """(mtime_ns, size). Size catches an edit within the same nanosecond."""
        try:
            stat = os.stat(self._path)
        except OSError:
            return None
        return (stat.st_mtime_ns, stat.st_size)

    def current(self) -> Policy:
        """Return the active policy, reloading first if the file changed."""
        stamp = self._file_stamp()
        if stamp is None or stamp == self._stamp:
            return self._policy

        with self._lock:
            # Re-check inside the lock: another thread may have just reloaded.
            if stamp == self._stamp:
                return self._policy
            try:
                policy = load_policy_file(self._path)
            except PolicyError as exc:
                # Keep the last good policy. Do NOT relax to defaults and do
                # NOT raise into the caller's request.
                self._last_error = str(exc)
                self._stamp = stamp  # avoid re-parsing the same broken file
                logger.error(
                    "policy reload REFUSED, keeping previous policy: %s", exc
                )
                return self._policy

            self._policy = policy
            self._stamp = stamp
            self._last_error = None
            self._reload_count += 1
            logger.info(
                "policy reloaded from %s (reload #%d)", self._path, self._reload_count
            )
            return self._policy

    @property
    def path(self) -> Path:
        return self._path

    @property
    def last_error(self) -> str | None:
        """The error from the most recent refused reload, if any.

        Surfaced by health_check so a bad edit is visible without reading logs.
        """
        return self._last_error

    @property
    def reload_count(self) -> int:
        return self._reload_count


# ---------------------------------------------------------------------------
# evaluation - pure functions over a snapshot
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class PolicyVerdict:
    """Why a change was allowed or refused."""

    allowed: bool
    reasons: tuple[str, ...] = ()

    @classmethod
    def allow(cls) -> "PolicyVerdict":
        return cls(allowed=True)

    @classmethod
    def deny(cls, *reasons: str) -> "PolicyVerdict":
        return cls(allowed=False, reasons=tuple(reasons))

    def merged_with(self, other: "PolicyVerdict") -> "PolicyVerdict":
        if self.allowed and other.allowed:
            return self
        return replace(
            self, allowed=False, reasons=self.reasons + other.reasons
        )

    def describe(self) -> str:
        return "allowed" if self.allowed else "; ".join(self.reasons)


def evaluate_budget_change(
    policy: Policy,
    *,
    tier: Tier,
    current_units: object,
    new_units: object,
    already_increased_today_units: object = 0,
) -> PolicyVerdict:
    """Check one daily-budget change against the tier's limits.

    Boundaries are inclusive: exactly at a limit is allowed, a hair over is
    not. `already_increased_today_units` is this user's running total for
    the day, so the daily ceiling counts the change being proposed too.
    """
    limits = policy.limits_for(tier).budget
    reasons: list[str] = []

    current = coerce_units(current_units, field="current_units")
    proposed = coerce_units(new_units, field="new_units")
    spent_today = coerce_units(
        already_increased_today_units, field="already_increased_today_units"
    )
    code = policy.currency_code

    if proposed <= 0:
        reasons.append(f"a daily budget must be above zero, got {proposed} {code}")
        return PolicyVerdict.deny(*reasons)

    if proposed < limits.min_daily_units:
        reasons.append(
            f"{proposed} {code} is below the {tier.value} minimum of "
            f"{limits.min_daily_units} {code}"
        )
    if proposed > limits.max_daily_units:
        reasons.append(
            f"{proposed} {code} exceeds the {tier.value} cap of "
            f"{limits.max_daily_units} {code}"
        )

    delta = proposed - current
    if delta > 0:
        if current == 0:
            # percent_change from zero is undefined. Treat any rise off a
            # zero budget as unbounded and refuse it rather than inventing
            # a percentage.
            reasons.append(
                "cannot raise a budget from zero through this server; "
                "the percentage increase is undefined. Set it in the Google "
                "Ads UI once, then adjust it here."
            )
        else:
            increase_percent = percent_change(current, proposed)
            if increase_percent > limits.max_increase_percent:
                reasons.append(
                    f"a {increase_percent}% increase exceeds the {tier.value} "
                    f"limit of {limits.max_increase_percent}% per change "
                    f"({current} -> {proposed} {code})"
                )

        running_total = spent_today + delta
        if running_total > limits.max_total_increase_per_user_per_day_units:
            reasons.append(
                f"this raises your total budget increases today to "
                f"{running_total} {code}, over the {tier.value} daily ceiling of "
                f"{limits.max_total_increase_per_user_per_day_units} {code} "
                f"(already {spent_today} {code} today)"
            )

    return PolicyVerdict.deny(*reasons) if reasons else PolicyVerdict.allow()


def evaluate_bid_change(
    policy: Policy,
    *,
    tier: Tier,
    current_units: object,
    new_units: object,
) -> PolicyVerdict:
    """Check one keyword bid change against the tier's limits."""
    limits = policy.limits_for(tier).bids
    reasons: list[str] = []

    current = coerce_units(current_units, field="current_units")
    proposed = coerce_units(new_units, field="new_units")
    code = policy.currency_code

    if proposed <= 0:
        return PolicyVerdict.deny(f"a bid must be above zero, got {proposed} {code}")

    if proposed > limits.max_cpc_units:
        reasons.append(
            f"{proposed} {code} exceeds the {tier.value} max CPC of "
            f"{limits.max_cpc_units} {code}"
        )

    if proposed > current:
        if current == 0:
            reasons.append(
                "cannot raise a bid from zero through this server; "
                "the percentage increase is undefined"
            )
        else:
            increase_percent = percent_change(current, proposed)
            if increase_percent > limits.max_increase_percent:
                reasons.append(
                    f"a {increase_percent}% increase exceeds the {tier.value} "
                    f"limit of {limits.max_increase_percent}% per change "
                    f"({current} -> {proposed} {code})"
                )

    return PolicyVerdict.deny(*reasons) if reasons else PolicyVerdict.allow()


def evaluate_customer(policy: Policy, customer_id: str) -> PolicyVerdict:
    if not policy.allows_customer(customer_id):
        return PolicyVerdict.deny(
            f"account {customer_id} is not on the allowlist in policy.yaml. "
            "There is no wildcard; add it deliberately if it belongs."
        )
    return PolicyVerdict.allow()


def evaluate_operation(policy: Policy, operation: str) -> PolicyVerdict:
    if policy.blocks_operation(operation):
        return PolicyVerdict.deny(
            f"operation {operation!r} is on the blocked list in policy.yaml"
        )
    return PolicyVerdict.allow()


def evaluate_match_type_against_bidding(
    policy: Policy, *, match_type: str, bidding_strategy: str
) -> PolicyVerdict:
    """Broad match under manual CPC is how accounts quietly haemorrhage money."""
    if not policy.rules.block_broad_match_with_manual_cpc:
        return PolicyVerdict.allow()
    if (
        str(match_type).strip().upper() == "BROAD"
        and "MANUAL_CPC" in str(bidding_strategy).strip().upper()
    ):
        return PolicyVerdict.deny(
            "broad match is not permitted on a manual CPC campaign "
            "(rules.block_broad_match_with_manual_cpc). Use phrase or exact, "
            "or move the campaign to an automated bidding strategy."
        )
    return PolicyVerdict.allow()
