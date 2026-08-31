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

`GoogleAdsExecutor` at the bottom is the real implementation, added in
Phase 4. Tests use a fake that satisfies the same Protocol, so nothing in the
test suite can reach Google.

Python notes for a TypeScript reader:
  - `Protocol` is structural typing - the same idea as a TS `interface`. A
    class satisfies it by having the right methods, with no explicit
    `implements` and no inheritance.
  - `@runtime_checkable` allows `isinstance(x, Executor)`, which the
    architecture test uses.
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, field
from typing import Any, Protocol, runtime_checkable

logger = logging.getLogger(__name__)


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


# ---------------------------------------------------------------------------
# The real implementation
# ---------------------------------------------------------------------------
# Everything below this line can spend money. Read the whole file before
# adding an operation.

# operation name -> the CampaignStatus enum member it sets.
#
# This table is the complete list of mutations this server can perform. It is
# a table rather than a set of methods so that "what can this thing do?" is
# answerable by reading four lines. REMOVED is deliberately absent and must
# stay absent: pausing is reversible, removal is not.
CAMPAIGN_STATUS_OPERATIONS: dict[str, str] = {
    "pause_campaign": "PAUSED",
    "enable_campaign": "ENABLED",
}

# The only field paths any Phase 4 mutation may write.
#
# An update mask says which fields to overwrite. A mask naming a field that
# is unset on the message blanks that field, so an over-broad mask is how you
# silently erase a campaign name while changing its status. The helper we use
# derives the mask from fields actually set on the message, which cannot
# include an unset one - this allowlist is the second line of defence against
# a future edit that sets an extra field without thinking about it.
ALLOWED_MASK_PATHS = frozenset({"resource_name", "status"})


class ExecutorError(RuntimeError):
    """A mutation failed. Never swallowed, never downgraded to success."""


class GoogleAdsExecutor(Executor):
    """Applies mutations with the CALLING USER's own Google credential.

    Constructed once at startup but holds no credential: `token_provider` is
    called per request, exactly as in ads/reads.py. Google's own permission
    model therefore stays the outermost guard - a user removed from the
    account cannot mutate it, whatever this server's policy says.
    """

    def __init__(self, *, settings: Any, token_provider: Any) -> None:
        self._settings = settings
        self._token_provider = token_provider

    async def apply(self, request: MutationRequest) -> MutationResult:
        """Dispatch one mutation. Blocking gRPC runs off the event loop."""
        if request.operation not in CAMPAIGN_STATUS_OPERATIONS:
            # Fail closed. An unknown operation is a programming error, and
            # guessing at what it meant is how you mutate the wrong thing.
            raise ExecutorError(
                f"unknown operation {request.operation!r}. Known operations: "
                f"{sorted(CAMPAIGN_STATUS_OPERATIONS)}"
            )
        return await asyncio.to_thread(self._set_campaign_status, request)

    # ------------------------------------------------------------------

    def _set_campaign_status(self, request: MutationRequest) -> MutationResult:
        """Change one campaign's status. The only mutation Phase 4 performs."""
        # Imported here rather than at module scope so that importing this
        # module stays cheap and the dependency is visible at the call site.
        from google.ads.googleads.errors import GoogleAdsException
        from google.api_core import protobuf_helpers

        from .client import build_client

        status_name = CAMPAIGN_STATUS_OPERATIONS[request.operation]
        campaign_id = str(request.payload.get("campaign_id", "")).strip()
        if not campaign_id.isdigit():
            raise ExecutorError(
                f"campaign_id must be numeric, got {campaign_id!r}"
            )

        client = build_client(
            settings=self._settings, access_token=self._token_provider()
        )
        service = client.get_service("CampaignService")

        operation = client.get_type("CampaignOperation")
        campaign = operation.update
        campaign.resource_name = service.campaign_path(
            request.customer_id, campaign_id
        )
        campaign.status = client.enums.CampaignStatusEnum[status_name]

        # Derive the mask from the fields actually set above, which is the
        # idiom Google's own samples use, then verify it against the
        # allowlist before sending it.
        client.copy_from(
            operation.update_mask,
            protobuf_helpers.field_mask(None, campaign._pb),
        )
        paths = set(operation.update_mask.paths)
        unexpected = paths - ALLOWED_MASK_PATHS
        if unexpected:
            raise ExecutorError(
                f"refusing to send an update mask containing {sorted(unexpected)}. "
                f"Only {sorted(ALLOWED_MASK_PATHS)} may be written. An unexpected "
                "path here would overwrite a field this operation never set."
            )

        try:
            response = service.mutate_campaigns(
                customer_id=request.customer_id,
                operations=[operation],
                # All-or-nothing. With partial_failure the API returns 200
                # and buries per-operation errors in the response body, which
                # is exactly how a "successful" mutation silently does
                # nothing. We would rather have an exception.
                partial_failure=False,
                validate_only=request.validate_only,
            )
        except GoogleAdsException as exc:
            raise ExecutorError(_describe(exc)) from exc

        if request.validate_only:
            # validate_only returns no results by design: nothing was written.
            return MutationResult(
                success=True,
                resource_names=(),
                details={
                    "validate_only": True,
                    "operation": request.operation,
                    "would_set_status": status_name,
                },
            )

        resource_names = tuple(result.resource_name for result in response.results)
        if not resource_names:
            # A success with nothing changed is not a success we accept.
            raise ExecutorError(
                f"{request.operation} reported success but returned no resource "
                "names, so we cannot confirm anything changed. Check the account."
            )

        logger.info(
            "APPLIED %s customer=%s resources=%s",
            request.operation,
            request.customer_id,
            list(resource_names),
        )
        return MutationResult(
            success=True,
            resource_names=resource_names,
            details={"operation": request.operation, "new_status": status_name},
        )


def _describe(exc: Any) -> str:
    """Flatten a GoogleAdsException into one readable line.

    The default repr is a wall of protobuf. What an operator needs is the
    error code and message, and the request_id for a support ticket.
    """
    parts: list[str] = []
    failure = getattr(exc, "failure", None)
    for error in getattr(failure, "errors", []) or []:
        code = getattr(error, "error_code", None)
        detail = getattr(error, "message", "")
        parts.append(f"{code}: {detail}" if code else str(detail))
    request_id = getattr(exc, "request_id", None)
    summary = "; ".join(parts) if parts else str(exc)
    return f"Google Ads rejected the mutation: {summary} (request_id={request_id})"
