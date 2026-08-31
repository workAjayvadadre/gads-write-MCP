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
# REMOVED is deliberately absent and must stay absent: pausing is
# reversible, removal is not.
CAMPAIGN_STATUS_OPERATIONS: dict[str, str] = {
    "pause_campaign": "PAUSED",
    "enable_campaign": "ENABLED",
}

# Per-operation update-mask allowlists.
#
# An update mask says which fields to overwrite, and a mask naming a field
# that is UNSET on the message blanks that field. That is how a status change
# silently erases a campaign name. The helper we use derives the mask from
# fields actually set on the message, so it cannot name an unset one; these
# allowlists are the second line of defence against a future edit that sets
# an extra field without thinking about it.
#
# Per-operation rather than global on purpose. A single union would let a
# budget mutation legally carry `status`, which is exactly the kind of
# cross-contamination this is meant to catch.
UPDATE_MASK_ALLOWLIST: dict[str, frozenset[str]] = {
    "pause_campaign": frozenset({"resource_name", "status"}),
    "enable_campaign": frozenset({"resource_name", "status"}),
    "update_campaign_budget": frozenset({"resource_name", "amount_micros"}),
    "update_ad_group_bid": frozenset({"resource_name", "cpc_bid_micros"}),
}

# Creates carry no update mask - there is no existing row to partially
# overwrite - so they are listed separately rather than given an empty mask.
CREATE_OPERATIONS: frozenset[str] = frozenset(
    {
        "add_campaign_negative_keyword",
        "add_ad_group_negative_keyword",
        "add_keyword",
        "create_responsive_search_ad",
    }
)

# The complete list of mutations this server can perform. If it is not here,
# apply() refuses it.
KNOWN_OPERATIONS: frozenset[str] = frozenset(UPDATE_MASK_ALLOWLIST) | CREATE_OPERATIONS


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
        if request.operation not in KNOWN_OPERATIONS:
            # Fail closed. An unknown operation is a programming error, and
            # guessing at what it meant is how you mutate the wrong thing.
            raise ExecutorError(
                f"unknown operation {request.operation!r}. Known operations: "
                f"{sorted(KNOWN_OPERATIONS)}"
            )
        return await asyncio.to_thread(self._dispatch, request)

    # ------------------------------------------------------------------
    # plumbing
    # ------------------------------------------------------------------

    def _dispatch(self, request: MutationRequest) -> MutationResult:
        from .client import build_client

        client = build_client(
            settings=self._settings, access_token=self._token_provider()
        )
        handler = {
            "pause_campaign": self._set_campaign_status,
            "enable_campaign": self._set_campaign_status,
            "update_campaign_budget": self._update_campaign_budget,
            "update_ad_group_bid": self._update_ad_group_bid,
            "add_campaign_negative_keyword": self._add_campaign_negative_keyword,
            "add_ad_group_negative_keyword": self._add_ad_group_negative_keyword,
            "add_keyword": self._add_keyword,
            "create_responsive_search_ad": self._create_responsive_search_ad,
        }[request.operation]
        return handler(request, client)

    @staticmethod
    def _seal_mask(operation_name: str, operation: Any, message: Any, client: Any) -> None:
        """Derive the update mask from set fields, then check the allowlist."""
        from google.api_core import protobuf_helpers

        client.copy_from(
            operation.update_mask, protobuf_helpers.field_mask(None, message._pb)
        )
        paths = set(operation.update_mask.paths)
        allowed = UPDATE_MASK_ALLOWLIST[operation_name]
        unexpected = paths - allowed
        if unexpected:
            raise ExecutorError(
                f"refusing to send an update mask containing {sorted(unexpected)} "
                f"for {operation_name}. Only {sorted(allowed)} may be written; an "
                "unexpected path would overwrite a field this operation never set."
            )

    @staticmethod
    def _send(call: Any, request: MutationRequest, **kwargs: Any) -> Any:
        """Invoke a mutate with the flags every mutation in this server uses."""
        from google.ads.googleads.errors import GoogleAdsException

        try:
            return call(
                customer_id=request.customer_id,
                # All-or-nothing. With partial_failure the API returns 200 and
                # buries per-operation errors in the response body, which is
                # exactly how a "successful" mutation silently does nothing.
                partial_failure=False,
                validate_only=request.validate_only,
                **kwargs,
            )
        except GoogleAdsException as exc:
            raise ExecutorError(_describe(exc)) from exc

    @staticmethod
    def _finish(
        response: Any, request: MutationRequest, details: dict[str, Any]
    ) -> MutationResult:
        if request.validate_only:
            # validate_only returns no results by design: nothing was written.
            return MutationResult(
                success=True,
                resource_names=(),
                details={"validate_only": True, "operation": request.operation, **details},
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
            details={"operation": request.operation, **details},
        )

    # ------------------------------------------------------------------
    # operations
    # ------------------------------------------------------------------

    def _set_campaign_status(self, request: MutationRequest, client: Any) -> MutationResult:
        status_name = CAMPAIGN_STATUS_OPERATIONS[request.operation]
        campaign_id = _digits(request.payload, "campaign_id")

        service = client.get_service("CampaignService")
        operation = client.get_type("CampaignOperation")
        campaign = operation.update
        campaign.resource_name = service.campaign_path(request.customer_id, campaign_id)
        campaign.status = client.enums.CampaignStatusEnum[status_name]
        self._seal_mask(request.operation, operation, campaign, client)

        response = self._send(
            service.mutate_campaigns, request, operations=[operation]
        )
        return self._finish(response, request, {"new_status": status_name})

    def _update_campaign_budget(
        self, request: MutationRequest, client: Any
    ) -> MutationResult:
        """Change a campaign budget's daily amount.

        Takes the budget's resource name rather than a campaign id, because a
        budget is a separate resource that campaigns point at. The caller is
        responsible for having established that the budget belongs to exactly
        one campaign - see tools/writes.py, which refuses shared budgets.
        """
        budget_resource = str(request.payload.get("budget_resource_name", "")).strip()
        if not budget_resource.startswith("customers/"):
            raise ExecutorError(
                f"budget_resource_name must be a Google Ads resource name, got "
                f"{budget_resource!r}"
            )
        amount_micros = _micros(request.payload, "amount_micros")

        service = client.get_service("CampaignBudgetService")
        operation = client.get_type("CampaignBudgetOperation")
        budget = operation.update
        budget.resource_name = budget_resource
        budget.amount_micros = amount_micros
        self._seal_mask(request.operation, operation, budget, client)

        response = self._send(
            service.mutate_campaign_budgets, request, operations=[operation]
        )
        return self._finish(
            response, request, {"new_amount_micros": amount_micros}
        )

    def _update_ad_group_bid(
        self, request: MutationRequest, client: Any
    ) -> MutationResult:
        ad_group_id = _digits(request.payload, "ad_group_id")
        cpc_bid_micros = _micros(request.payload, "cpc_bid_micros")

        service = client.get_service("AdGroupService")
        operation = client.get_type("AdGroupOperation")
        ad_group = operation.update
        ad_group.resource_name = service.ad_group_path(
            request.customer_id, ad_group_id
        )
        ad_group.cpc_bid_micros = cpc_bid_micros
        self._seal_mask(request.operation, operation, ad_group, client)

        response = self._send(service.mutate_ad_groups, request, operations=[operation])
        return self._finish(
            response, request, {"new_cpc_bid_micros": cpc_bid_micros}
        )

    def _add_campaign_negative_keyword(
        self, request: MutationRequest, client: Any
    ) -> MutationResult:
        campaign_id = _digits(request.payload, "campaign_id")
        text, match_type = _keyword(request.payload)

        service = client.get_service("CampaignCriterionService")
        operation = client.get_type("CampaignCriterionOperation")
        criterion = operation.create
        criterion.campaign = service.campaign_path(request.customer_id, campaign_id)
        # The whole point of this operation. Without it we would be ADDING a
        # positive targeting criterion - the exact opposite, and expensive.
        criterion.negative = True
        criterion.keyword.text = text
        criterion.keyword.match_type = client.enums.KeywordMatchTypeEnum[match_type]

        response = self._send(
            service.mutate_campaign_criteria, request, operations=[operation]
        )
        return self._finish(
            response, request, {"negative": True, "keyword": text, "match_type": match_type}
        )

    def _add_ad_group_negative_keyword(
        self, request: MutationRequest, client: Any
    ) -> MutationResult:
        ad_group_id = _digits(request.payload, "ad_group_id")
        text, match_type = _keyword(request.payload)

        service = client.get_service("AdGroupCriterionService")
        operation = client.get_type("AdGroupCriterionOperation")
        criterion = operation.create
        criterion.ad_group = service.ad_group_path(request.customer_id, ad_group_id)
        criterion.negative = True
        criterion.keyword.text = text
        criterion.keyword.match_type = client.enums.KeywordMatchTypeEnum[match_type]

        response = self._send(
            service.mutate_ad_group_criteria, request, operations=[operation]
        )
        return self._finish(
            response, request, {"negative": True, "keyword": text, "match_type": match_type}
        )

    def _add_keyword(self, request: MutationRequest, client: Any) -> MutationResult:
        ad_group_id = _digits(request.payload, "ad_group_id")
        text, match_type = _keyword(request.payload)
        status = _status(request.payload)

        service = client.get_service("AdGroupCriterionService")
        operation = client.get_type("AdGroupCriterionOperation")
        criterion = operation.create
        criterion.ad_group = service.ad_group_path(request.customer_id, ad_group_id)
        # Explicitly false. A positive keyword created with `negative` left to
        # chance would be a very expensive default.
        criterion.negative = False
        criterion.status = client.enums.AdGroupCriterionStatusEnum[status]
        criterion.keyword.text = text
        criterion.keyword.match_type = client.enums.KeywordMatchTypeEnum[match_type]

        response = self._send(
            service.mutate_ad_group_criteria, request, operations=[operation]
        )
        return self._finish(
            response,
            request,
            {"keyword": text, "match_type": match_type, "status": status},
        )

    def _create_responsive_search_ad(
        self, request: MutationRequest, client: Any
    ) -> MutationResult:
        ad_group_id = _digits(request.payload, "ad_group_id")
        status = _status(request.payload)
        headlines = list(request.payload.get("headlines") or [])
        descriptions = list(request.payload.get("descriptions") or [])
        final_urls = list(request.payload.get("final_urls") or [])
        if not (headlines and descriptions and final_urls):
            raise ExecutorError(
                "a responsive search ad needs headlines, descriptions and at "
                "least one final URL"
            )

        service = client.get_service("AdGroupAdService")
        operation = client.get_type("AdGroupAdOperation")
        ad_group_ad = operation.create
        ad_group_ad.ad_group = service.ad_group_path(request.customer_id, ad_group_id)
        ad_group_ad.status = client.enums.AdGroupAdStatusEnum[status]
        ad_group_ad.ad.final_urls.extend(final_urls)

        for headline in headlines:
            asset = client.get_type("AdTextAsset")
            asset.text = headline
            ad_group_ad.ad.responsive_search_ad.headlines.append(asset)
        for description in descriptions:
            asset = client.get_type("AdTextAsset")
            asset.text = description
            ad_group_ad.ad.responsive_search_ad.descriptions.append(asset)

        path1 = str(request.payload.get("path1") or "").strip()
        path2 = str(request.payload.get("path2") or "").strip()
        if path1:
            ad_group_ad.ad.responsive_search_ad.path1 = path1
        if path2:
            ad_group_ad.ad.responsive_search_ad.path2 = path2

        response = self._send(
            service.mutate_ad_group_ads, request, operations=[operation]
        )
        return self._finish(
            response,
            request,
            {
                "status": status,
                "headline_count": len(headlines),
                "description_count": len(descriptions),
                "final_urls": final_urls,
            },
        )


# ---------------------------------------------------------------------------
# payload helpers
# ---------------------------------------------------------------------------
# These are backstops, not the primary validation. Tools validate first and
# return a friendly message; these exist so that a validation step somebody
# forgets to call cannot become a malformed mutation.


def _digits(payload: dict[str, Any], key: str) -> str:
    value = str(payload.get(key, "")).strip()
    if not value.isdigit():
        raise ExecutorError(f"{key} must be numeric, got {value!r}")
    return value


def _micros(payload: dict[str, Any], key: str) -> int:
    raw = payload.get(key)
    try:
        value = int(raw)  # type: ignore[arg-type]
    except (TypeError, ValueError) as exc:
        raise ExecutorError(f"{key} must be a whole number of micros, got {raw!r}") from exc
    if value <= 0:
        raise ExecutorError(f"{key} must be above zero, got {value}")
    return value


def _keyword(payload: dict[str, Any]) -> tuple[str, str]:
    text = str(payload.get("keyword_text", "")).strip()
    if not text:
        raise ExecutorError("keyword_text must not be empty")
    match_type = str(payload.get("match_type", "")).strip().upper()
    if match_type not in ("EXACT", "PHRASE", "BROAD"):
        raise ExecutorError(
            f"match_type must be EXACT, PHRASE or BROAD, got {match_type!r}"
        )
    return text, match_type


def _status(payload: dict[str, Any]) -> str:
    """ENABLED or PAUSED. REMOVED is refused here as well as in policy."""
    status = str(payload.get("status", "PAUSED")).strip().upper()
    if status not in ("ENABLED", "PAUSED"):
        raise ExecutorError(
            f"status must be ENABLED or PAUSED, got {status!r}. Removal is out "
            "of scope for v1."
        )
    return status


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
