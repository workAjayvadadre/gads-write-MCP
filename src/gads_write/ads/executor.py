"""THE ONLY MODULE PERMITTED TO CALL A GOOGLE ADS MUTATE.

Read this before adding anything here.

Every mutation in this server funnels through one `Executor.apply` call. Tool
functions build a `MutationRequest` and hand it to the executor; they never
touch a Google Ads service object themselves. `tests/test_architecture.py`
fails the build if a mutate call or a `get_service(...)` appears anywhere
else in `src/`.

Why bother: a single execution path means the audit log cannot be bypassed,
`validate_only` cannot be forgotten, and there is exactly one place to look
when asking "what could possibly have changed this account?".

Phase 2 defines only the interface. There is no real implementation yet and
the `google-ads` library is not installed. Phase 3 adds
`GoogleAdsExecutor(Executor)` below; tests use a fake that satisfies the same
Protocol.

Python notes for a TypeScript reader:
  - `Protocol` is structural typing - the same idea as a TS `interface`. A
    class satisfies it by having the right methods, with no explicit
    `implements` and no inheritance.
  - `@runtime_checkable` allows `isinstance(x, Executor)`, which the
    architecture test uses.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Protocol, runtime_checkable


@dataclass(frozen=True)
class MutationRequest:
    """One mutation, fully described, with nothing left to interpret."""

    customer_id: str
    # Stable name for the kind of change, e.g. "pause_campaign".
    # Matched against policy.blocked_operations.
    operation: str
    # The Google Ads operation payload. Its exact shape is defined in Phase 3
    # alongside the real executor, so that it is written against the API
    # rather than guessed at now.
    payload: dict[str, Any] = field(default_factory=dict)
    # When true, the Ads API validates and reports errors without applying.
    # Phase 4 uses this for the first live mutation.
    validate_only: bool = False


@dataclass(frozen=True)
class MutationResult:
    """What came back. `resource_names` is what the audit log records."""

    success: bool
    resource_names: tuple[str, ...] = ()
    # Trimmed, non-sensitive summary of the API response for the audit line.
    details: dict[str, Any] = field(default_factory=dict)
    error: str | None = None


@runtime_checkable
class Executor(Protocol):
    """The one interface through which anything changes."""

    async def apply(self, request: MutationRequest) -> MutationResult:
        """Apply a mutation, or validate it if `request.validate_only`.

        Implementations must not swallow errors. A failed mutation returns
        `MutationResult(success=False, error=...)` or raises; it never
        returns success with an empty result.
        """
        ...
