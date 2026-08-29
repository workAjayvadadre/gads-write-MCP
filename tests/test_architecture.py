"""Structural rules enforced as tests, so a violation fails the build.

The single-execution-path rule is worth nothing if it lives only in a
comment. These tests read the source tree and fail if it drifts.
"""

from __future__ import annotations

import re
from pathlib import Path

SRC = Path(__file__).resolve().parents[1] / "src" / "gads_write"

# The one file allowed to call a Google Ads mutate.
EXECUTOR = SRC / "ads" / "executor.py"

# `campaign_service.mutate_campaigns(...)` and friends.
MUTATE_CALL = re.compile(r"\.mutate_\w+\s*\(")
# `client.get_service("CampaignService")` - the gateway to a mutate.
GET_SERVICE = re.compile(r"\bget_service\s*\(")


def _python_files() -> list[Path]:
    return sorted(SRC.rglob("*.py"))


def test_only_executor_may_call_mutate() -> None:
    offenders: list[str] = []
    for path in _python_files():
        if path == EXECUTOR:
            continue
        text = path.read_text(encoding="utf-8")
        for number, line in enumerate(text.splitlines(), start=1):
            if MUTATE_CALL.search(line) or GET_SERVICE.search(line):
                offenders.append(f"{path.relative_to(SRC)}:{number}: {line.strip()}")

    assert not offenders, (
        "Google Ads mutate calls are only permitted in ads/executor.py.\n"
        "Move this through Executor.apply() so it cannot bypass the guard "
        "chain or the audit log.\n  " + "\n  ".join(offenders)
    )


def test_safety_package_never_imports_the_ads_client() -> None:
    # The safety core must stay testable with no credentials and no network.
    offenders: list[str] = []
    for path in (SRC / "safety").rglob("*.py"):
        text = path.read_text(encoding="utf-8")
        for number, line in enumerate(text.splitlines(), start=1):
            stripped = line.strip()
            if stripped.startswith(("import ", "from ")) and "google.ads" in stripped:
                offenders.append(f"{path.relative_to(SRC)}:{number}: {stripped}")

    assert not offenders, (
        "safety/ must not import the Google Ads client library:\n  "
        + "\n  ".join(offenders)
    )


def test_no_hardcoded_customer_ids_outside_config() -> None:
    # Ten consecutive digits in source is almost always a customer ID that
    # belongs in policy.yaml. Tests are exempt; they use fixtures.
    pattern = re.compile(r"(?<!\d)\d{10}(?!\d)")
    offenders: list[str] = []
    for path in _python_files():
        for number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
            if pattern.search(line) and "noqa: customer-id" not in line:
                offenders.append(f"{path.relative_to(SRC)}:{number}: {line.strip()}")

    assert not offenders, (
        "Customer IDs belong in config/policy.yaml, not in Python:\n  "
        + "\n  ".join(offenders)
    )


def test_no_remove_or_delete_tools_exist() -> None:
    # v1 has no irreversible operations. This catches one being added by
    # habit rather than by decision.
    pattern = re.compile(r"^\s*(async\s+)?def\s+(remove|delete)_\w+", re.MULTILINE)
    offenders: list[str] = []
    for path in _python_files():
        for match in pattern.finditer(path.read_text(encoding="utf-8")):
            offenders.append(f"{path.relative_to(SRC)}: {match.group(0).strip()}")

    assert not offenders, (
        "v1 has no remove/delete operations - pausing is reversible, removal "
        "is not:\n  " + "\n  ".join(offenders)
    )
