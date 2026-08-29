"""Shared fixtures. Tests build their own policy files rather than reading
config/policy.yaml, so that changing a real business limit never breaks the
test suite and a passing suite never implies the real limits are sane.
"""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

BASE_POLICY: dict = {
    "version": 2,
    "allowed_customer_ids": ["1234567890"],
    "currency_code": "INR",
    "timezone": "Asia/Kolkata",
    "limits": {
        "defaults": {
            "budget": {
                "min_daily": 50,
                "max_daily": 5000,
                "max_increase_percent": 25,
                "max_total_increase_per_user_per_day": 10000,
            },
            "bids": {"max_cpc": 200, "max_increase_percent": 30},
        },
        "tiers": {
            "operator": {
                "budget": {
                    "max_daily": 2000,
                    "max_increase_percent": 20,
                    "max_total_increase_per_user_per_day": 3000,
                },
                "bids": {"max_cpc": 100},
            },
            "lead": {},
        },
    },
    "rules": {
        "new_entities_start_paused": True,
        "block_broad_match_with_manual_cpc": True,
        "allowed_final_url_domains": ["indiraivf.com", "www.indiraivf.com"],
    },
    "blocked_operations": ["remove_campaign"],
    "plans": {"ttl_seconds": 600, "single_use": True},
}


def _deep_update(base: dict, patch: dict) -> dict:
    out = dict(base)
    for key, value in patch.items():
        if isinstance(value, dict) and isinstance(out.get(key), dict):
            out[key] = _deep_update(out[key], value)
        else:
            out[key] = value
    return out


@pytest.fixture
def write_policy(tmp_path: Path):
    """Write a policy file, optionally patched. Returns its path."""

    def _write(patch: dict | None = None, *, name: str = "policy.yaml") -> Path:
        data = _deep_update(BASE_POLICY, patch or {})
        path = tmp_path / name
        path.write_text(yaml.safe_dump(data, sort_keys=False), encoding="utf-8")
        return path

    return _write


BASE_ROLES: dict = {
    "mode": "file",
    "default_tier": "none",
    "users": {"lead@example.com": "lead", "op@example.com": "operator"},
}


@pytest.fixture
def write_roles(tmp_path: Path):
    """Write a roles file, optionally patched. Returns its path."""

    def _write(patch: dict | None = None, *, name: str = "roles.yaml") -> Path:
        patch = dict(patch or {})
        # `users` is REPLACED wholesale, not merged. Deep-merging it would
        # silently keep the base fixture's lead in a test that is trying to
        # prove what happens when there is no lead.
        users = patch.pop("users", None)
        data = _deep_update(BASE_ROLES, patch)
        if users is not None:
            data["users"] = users
        path = tmp_path / name
        path.write_text(yaml.safe_dump(data, sort_keys=False), encoding="utf-8")
        return path

    return _write


class FakeClock:
    """A monotonic clock the test drives by hand, instead of sleeping."""

    def __init__(self, start: float = 1000.0) -> None:
        self.now = start

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


@pytest.fixture
def clock() -> FakeClock:
    return FakeClock()
