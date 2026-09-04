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
# The one file allowed to run a Google Ads read.
READER = SRC / "ads" / "reads.py"

# `campaign_service.mutate_campaigns(...)` AND the bare
# `google_ads_service.mutate(...)` bulk mutate. The bare form matters: it is
# a real method on GoogleAdsServiceClient (verified against v25), so a
# pattern that only caught `mutate_<something>` would have left the single
# most powerful call in the API unguarded.
MUTATE_CALL = re.compile(r"\.mutate(_\w+)?\s*\(")

# `client.get_service("CampaignService")` - the gateway to any API call.
GET_SERVICE = re.compile(r"\bget_service\s*\(")

# Services ads/reads.py is permitted to ask for. Neither can mutate anything
# through the methods used there, and confining the list means a future edit
# that reaches for CampaignService in the read path fails the build.
READ_SAFE_SERVICES = {"GoogleAdsService", "CustomerService"}
SERVICE_NAME = re.compile(r"get_service\(\s*[\"'](\w+)[\"']")


def _python_files() -> list[Path]:
    return sorted(SRC.rglob("*.py"))


def test_only_executor_may_call_mutate() -> None:
    offenders: list[str] = []
    for path in _python_files():
        if path == EXECUTOR:
            continue
        text = path.read_text(encoding="utf-8")
        for number, line in enumerate(text.splitlines(), start=1):
            if MUTATE_CALL.search(line):
                offenders.append(f"{path.relative_to(SRC)}:{number}: {line.strip()}")

    assert not offenders, (
        "Google Ads mutate calls are only permitted in ads/executor.py.\n"
        "Move this through Executor.apply() so it cannot bypass the guard "
        "chain or the audit log.\n  " + "\n  ".join(offenders)
    )


def test_get_service_is_confined_to_the_two_api_modules() -> None:
    """Only the executor and the reader may reach for a service client.

    Everything else - tools, guards, resolvers - goes through one of their
    narrow interfaces. This is what stops a tool quietly acquiring its own
    API access and skipping the gate.
    """
    offenders: list[str] = []
    for path in _python_files():
        if path in (EXECUTOR, READER):
            continue
        text = path.read_text(encoding="utf-8")
        for number, line in enumerate(text.splitlines(), start=1):
            if GET_SERVICE.search(line):
                offenders.append(f"{path.relative_to(SRC)}:{number}: {line.strip()}")

    assert not offenders, (
        "get_service() is only permitted in ads/executor.py (mutations) and "
        "ads/reads.py (reads):\n  " + "\n  ".join(offenders)
    )


def test_the_read_path_asks_only_for_read_safe_services() -> None:
    text = READER.read_text(encoding="utf-8")
    requested = set(SERVICE_NAME.findall(text))
    unexpected = requested - READ_SAFE_SERVICES
    assert not unexpected, (
        f"ads/reads.py requested {sorted(unexpected)}. The read path may only "
        f"use {sorted(READ_SAFE_SERVICES)}; anything that can mutate belongs "
        "in ads/executor.py."
    )


def test_tools_never_import_the_ads_client() -> None:
    """Tool functions call ads/reads.py and ads/executor.py, never Google.

    tools/__init__.py promises tools are thin. This is that promise enforced:
    a tool that imported the client library could build its own query or its
    own mutation and never touch the gate.
    """
    offenders: list[str] = []
    for path in (SRC / "tools").rglob("*.py"):
        for number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
            stripped = line.strip()
            if stripped.startswith(("import ", "from ")) and "google.ads" in stripped:
                offenders.append(f"{path.relative_to(SRC)}:{number}: {stripped}")

    assert not offenders, (
        "tools/ must not import the Google Ads client library:\n  "
        + "\n  ".join(offenders)
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
    # belongs in the environment or comes from the MCC. Tests are exempt;
    # they use fixtures.
    pattern = re.compile(r"(?<!\d)\d{10}(?!\d)")
    offenders: list[str] = []
    for path in _python_files():
        for number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
            if pattern.search(line) and "noqa: customer-id" not in line:
                offenders.append(f"{path.relative_to(SRC)}:{number}: {line.strip()}")

    assert not offenders, (
        "Customer IDs come from the MCC or the environment, never from Python:\n  "
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


def test_every_write_tool_has_checks_defined() -> None:
    # Three tables describe a write tool: registry.py says which tier it
    # needs, operations.py says how to check it, executor.py says how to
    # apply it. Nothing cross-checks them, and a tool registered without an
    # OPERATIONS entry is draftable but can never be confirmed - which is
    # the safe direction to get it wrong, but a silent one. This makes the
    # omission fail the build instead.
    from gads_write.tools.operations import OPERATIONS
    from gads_write.tools.registry import all_specs

    drafting_tools = {
        name
        for name, spec in all_specs().items()
        if spec.writes and not spec.applies_plan
    }
    missing = sorted(drafting_tools - set(OPERATIONS))
    assert not missing, (
        "these write tools are registered but have no entry in "
        "tools/operations.py, so they can be drafted and never confirmed:\n  "
        + "\n  ".join(missing)
    )

    unregistered = sorted(set(OPERATIONS) - drafting_tools)
    assert not unregistered, (
        "these tools have checks defined but are not registered as writes "
        "in tools/registry.py:\n  " + "\n  ".join(unregistered)
    )
