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
        "create_campaign",
        "create_ad_group",
    }
)

# The one ad group type this server creates. `AdGroup.type_` is IMMUTABLE in
# the Google Ads API - verified in the v25 proto - and there is no tool here
# to remove an ad group, so a wrong type could never be corrected through this
# server. SEARCH_STANDARD is the only type that belongs in the Search
# campaigns create_campaign makes, and the only one add_keyword and
# create_responsive_search_ad can populate.
AD_GROUP_TYPE = "SEARCH_STANDARD"

# Bidding strategies this server will create a campaign with. Two, not the
# seventeen the API offers: Manual CPC needs no extra value and Maximize
# Clicks needs only an optional ceiling, whereas Target CPA and Target ROAS
# each require their own target figure - another question to ask and another
# way to get it wrong. Add one when somebody asks for it with a reason.
# The temporary resource id a campaign uses to reference the budget created
# alongside it in the same request. Any negative number works; it exists only
# until Google resolves the request.
TEMP_BUDGET_ID = -1

CAMPAIGN_BIDDING_STRATEGIES: frozenset[str] = frozenset(
    {"MANUAL_CPC", "MAXIMIZE_CLICKS"}
)

# The complete list of mutations this server can perform. If it is not here,
# apply() refuses it.
KNOWN_OPERATIONS: frozenset[str] = frozenset(UPDATE_MASK_ALLOWLIST) | CREATE_OPERATIONS


def _is_permission_error(exc: Exception) -> bool:
    """Whether Google refused this on authorisation rather than anything else.

    Matched on the text because the exception type varies across the gRPC and
    google-ads layers. Only an authorisation refusal is safe to retry: it
    happens before any operation runs, so nothing was applied.
    """
    text = str(exc).upper()
    return "PERMISSION_DENIED" in text or "USER_PERMISSION_DENIED" in text


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
        """Run the mutation, THROUGH THE MANAGER first, then directly.

        Same reasoning as ads/reads.py: most people here hold one access row
        on the manager and none on the accounts beneath it, but somebody
        granted access directly to a single sub-account has no standing on the
        manager, and naming it makes Google refuse a change to an account they
        legitimately hold.

        Retrying a MUTATION needs the stronger argument that a read did not.
        It is safe here because the retry fires on a permission refusal only:
        Google rejected the request on authorisation, before any operation ran,
        so nothing was applied and there is nothing to double. `partial_failure`
        is false on every mutate, so a refusal is all-or-nothing by
        construction. Any other failure - including an ambiguous one - is
        surfaced immediately and never retried, because "we do not know whether
        it landed" must stay a reason to stop.
        """
        from .client import NO_MANAGER, build_client

        last: Exception | None = None
        for index, manager in enumerate((None, NO_MANAGER)):
            client = build_client(
                settings=self._settings,
                access_token=self._token_provider(),
                login_customer_id=manager,
            )
            try:
                return self._run(request, client)
            except ExecutorError as exc:
                last = exc
                if index == 1 or not _is_permission_error(exc):
                    raise
        # Unreachable: the loop either returns or re-raises on its last pass.
        raise AssertionError("mutation dispatch fell through")  # pragma: no cover

    def _run(self, request: MutationRequest, client: Any) -> MutationResult:
        handler = {
            "pause_campaign": self._set_campaign_status,
            "enable_campaign": self._set_campaign_status,
            "create_campaign": self._create_campaign,
            "create_ad_group": self._create_ad_group,
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
            # Both flags go in the REQUEST, never as keyword arguments. The
            # generated clients accept only (request, customer_id, operations,
            # retry, timeout, metadata) as kwargs; `partial_failure` and
            # `validate_only` are fields on the request message. Passing them
            # as kwargs raises TypeError before anything reaches Google, which
            # is how the first live mutation this server ever attempted failed.
            #
            # A plain dict is coerced by the client into whichever
            # Mutate*Request type that service expects, so this stays generic
            # across all six services rather than naming each type.
            return call(
                request={
                    "customer_id": request.customer_id,
                    # All-or-nothing. With partial_failure the API returns 200
                    # and buries per-operation errors in the response body,
                    # which is exactly how a "successful" mutation silently
                    # does nothing.
                    "partial_failure": False,
                    "validate_only": request.validate_only,
                    **kwargs,
                }
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

    def _create_campaign(
        self, request: MutationRequest, client: Any
    ) -> MutationResult:
        """Create a paused Search campaign and its budget, ATOMICALLY.

        A campaign cannot exist without a budget, and a budget is a separate
        resource. The obvious implementation - create the budget, then create
        the campaign - is wrong, and was wrong here: when the second call
        failed the first had already committed, leaving an orphaned budget in
        the account. Reporting the orphan is not good enough. A write server
        must not be able to leave debris.

        So both go in ONE request through `GoogleAdsService.mutate`, which
        applies its operations as a single transaction when partial_failure
        is off. Either both resources exist or neither does.

        The campaign refers to the budget by a TEMPORARY resource name with a
        negative id. Google resolves it within the request, which is what
        makes the two operations expressible together at all - there is no
        real id to reference until the request has run.
        """
        name = str(request.payload.get("name") or "").strip()
        budget_micros = int(request.payload.get("budget_micros") or 0)
        strategy = str(request.payload.get("bidding_strategy") or "").strip().upper()

        if not name:
            raise ExecutorError("a campaign needs a name")
        if budget_micros <= 0:
            raise ExecutorError("a campaign needs a daily budget above zero")
        if strategy not in CAMPAIGN_BIDDING_STRATEGIES:
            raise ExecutorError(
                f"unsupported bidding strategy {strategy!r}; this server "
                f"creates campaigns with {sorted(CAMPAIGN_BIDDING_STRATEGIES)}"
            )

        service = client.get_service("GoogleAdsService")
        budget_service = client.get_service("CampaignBudgetService")

        # Any negative id works; it exists only for the life of this request.
        temp_budget = budget_service.campaign_budget_path(
            request.customer_id, TEMP_BUDGET_ID
        )

        # ---- operation 1: the budget ---------------------------------------
        budget_operation = client.get_type("MutateOperation")
        budget = budget_operation.campaign_budget_operation.create
        budget.resource_name = temp_budget
        # Google requires budget names to be unique in an account. The campaign
        # name already has to be, so this cannot collide unless that would.
        budget.name = f"{name} - budget"
        budget.amount_micros = budget_micros
        budget.delivery_method = client.enums.BudgetDeliveryMethodEnum.STANDARD
        # NOT shared. A shared budget cannot be edited through this server at
        # all - the preview would name one campaign and change several - so
        # creating one would produce a campaign whose budget we then refuse to
        # touch.
        budget.explicitly_shared = False

        # ---- operation 2: the campaign -------------------------------------
        campaign_operation = client.get_type("MutateOperation")
        campaign = campaign_operation.campaign_operation.create
        campaign.name = name
        campaign.campaign_budget = temp_budget
        # Always paused. It has no ad groups, keywords or ads, so it could not
        # serve regardless - but status is what a person reads in the Google
        # Ads UI, and it should say plainly that nothing is live.
        campaign.status = client.enums.CampaignStatusEnum.PAUSED
        campaign.advertising_channel_type = (
            client.enums.AdvertisingChannelTypeEnum.SEARCH
        )

        # Google Search only. Search partners, Display and YouTube are each a
        # separate audience with its own cost profile; a preview that did not
        # mention them would be lying by omission.
        campaign.network_settings.target_google_search = True
        campaign.network_settings.target_search_network = False
        campaign.network_settings.target_content_network = False
        campaign.network_settings.target_partner_search_network = False

        # Mandatory on create, and the reason the first live attempt came back
        # `field_error: REQUIRED`. Left unset it defaults to UNSPECIFIED, which
        # Google rejects. It is a declaration, not a setting, so the preview
        # states it rather than making it quietly on someone's behalf.
        campaign.contains_eu_political_advertising = (
            client.enums.EuPoliticalAdvertisingStatusEnum
            .DOES_NOT_CONTAIN_EU_POLITICAL_ADVERTISING
        )

        if strategy == "MANUAL_CPC":
            campaign.manual_cpc.enhanced_cpc_enabled = False
        else:  # MAXIMIZE_CLICKS
            # TargetSpend in the API, "Maximize clicks" in the UI. No target
            # value: unset means "spend the budget", which is what the UI does.
            campaign.target_spend = client.get_type("TargetSpend")

        response = self._send(
            service.mutate,
            request,
            mutate_operations=[budget_operation, campaign_operation],
        )

        if request.validate_only:
            return MutationResult(
                success=True,
                resource_names=(),
                details={"validate_only": True, "operation": request.operation},
            )

        # Results come back in the order the operations were sent.
        created = tuple(
            r.campaign_budget_result.resource_name
            or r.campaign_result.resource_name
            for r in response.mutate_operation_responses
        )
        return MutationResult(
            success=True,
            resource_names=created,
            details={
                "operation": "create_campaign",
                "name": name,
                "status": "PAUSED",
                "bidding_strategy": strategy,
            },
        )

    def _create_ad_group(
        self, request: MutationRequest, client: Any
    ) -> MutationResult:
        """Create one ad group in an existing campaign.

        `cpc_bid_micros` is set only when the payload carries it, and is then
        omitted from the message entirely rather than sent as zero. On a create
        there is no update mask, so an absent field is simply not written -
        whereas an explicit zero is a real value Google would store, and a zero
        default bid under Manual CPC is an ad group that cannot win an auction.
        """
        campaign_id = _digits(request.payload, "campaign_id")
        name = str(request.payload.get("name") or "").strip()
        status = _status(request.payload)
        if not name:
            raise ExecutorError("an ad group needs a name")

        service = client.get_service("AdGroupService")
        operation = client.get_type("AdGroupOperation")
        ad_group = operation.create
        ad_group.campaign = service.campaign_path(request.customer_id, campaign_id)
        ad_group.name = name
        ad_group.status = client.enums.AdGroupStatusEnum[status]
        # Immutable once created. See AD_GROUP_TYPE.
        ad_group.type_ = client.enums.AdGroupTypeEnum[AD_GROUP_TYPE]

        cpc_bid_micros = request.payload.get("cpc_bid_micros")
        if cpc_bid_micros is not None:
            cpc_bid_micros = _micros(request.payload, "cpc_bid_micros")
            ad_group.cpc_bid_micros = cpc_bid_micros

        response = self._send(service.mutate_ad_groups, request, operations=[operation])
        return self._finish(
            response,
            request,
            {
                "name": name,
                "status": status,
                "ad_group_type": AD_GROUP_TYPE,
                "cpc_bid_micros": cpc_bid_micros,
            },
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
