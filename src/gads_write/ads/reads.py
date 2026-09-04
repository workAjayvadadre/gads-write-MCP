"""THE ONLY MODULE PERMITTED TO RUN A GOOGLE ADS READ.

The mirror image of ads/executor.py. Executor owns everything that changes
an account; this owns everything that looks at one. Tool functions call
neither the API nor a service object directly - they call one of the narrow
methods below and get back plain frozen dataclasses.

Why reads deserve their own confined module, when they cannot spend money:

  GAQL is a query language, and every query here is built by string
  interpolation because the API takes a query string, not bound parameters.
  There is no `?` placeholder to hide behind. So every value that reaches a
  query is asserted safe first, in one place, by `_literal()`. A tool that
  built its own query string would be a tool that could build an injected
  one - and a model that has been talked into passing
  `2026-01-01' OR campaign.id > '0` is not a hypothetical.

  It also keeps every field name in one file. When Google renames something
  in a future API version, there is exactly one place to fix, and
  tests/test_ads_reads.py asserts each query still parses into fields that
  exist in the pinned version's protos.

Everything here was verified against google-ads 31.4.0 / Google Ads API v25
by reading the generated protos, not the documentation:

  customer_user_access  user_id, email_address, access_role
  customer              id, descriptive_name, currency_code, time_zone,
                        manager, test_account, status
  campaign              id, name, status, advertising_channel_type
  campaign_budget       amount_micros
  search_term_view      search_term, status, ad_group
  metrics               impressions (int64), clicks (int64),
                        cost_micros (int64), conversions (double)

Deliberately NOT exposed: `metrics.average_cpc`. It is a double, and the
generated stubs carry no unit comment, so whether it is micros or currency
units could not be established without guessing. Average CPC is therefore
computed here from cost_micros / clicks, both of which are certain. A
1,000,000x error in a number a human uses to set a bid is exactly the
failure this repo's "no guessing" rule exists to prevent.

Python notes for a TypeScript reader:
  - The Google Ads client is synchronous and blocking. `asyncio.to_thread`
    moves each call onto a worker thread so a slow Google response does not
    freeze the whole server's event loop - the same reason you would not run
    a sync DB driver on Node's main thread.
  - `Protocol` is a structural interface. Tests implement `AdsReader` with a
    fake and never touch the network.
"""

from __future__ import annotations

import asyncio
import logging
import re
from dataclasses import dataclass
from typing import Any, Callable, Protocol, runtime_checkable

from ..settings import Settings
from .client import build_client

logger = logging.getLogger(__name__)


class AdsReadError(RuntimeError):
    """A read against the Google Ads API failed.

    Never swallowed and never turned into an empty result. An empty list and
    a failed query mean very different things: "this campaign spent nothing"
    versus "we could not find out what it spent". Reporting the second as
    the first is how someone concludes a campaign is safe to raise.
    """


# ---------------------------------------------------------------------------
# GAQL literal safety
# ---------------------------------------------------------------------------
# Every value interpolated into a query must match one of these. This is a
# backstop, not the primary defence: tools validate their inputs first and
# return a friendly message. This exists so that a validation step someone
# forgets to call cannot become an injected query.
_SAFE_LITERAL = re.compile(r"^[A-Za-z0-9_.:@-]+$")


def _literal(value: object, *, field: str) -> str:
    """Assert a value is safe to interpolate into GAQL, and return it.

    Raises rather than sanitising. Silently stripping a quote would turn an
    attack into a subtly wrong query; refusing turns it into a stack trace
    that someone reads.
    """
    text = str(value)
    if not _SAFE_LITERAL.fullmatch(text):
        raise AdsReadError(
            f"refusing to build a query: {field}={text!r} contains characters "
            "that are not permitted in a GAQL literal"
        )
    return text


def _int_literal(value: object, *, field: str, minimum: int, maximum: int) -> int:
    """Assert a value is an int in range. Used for LIMIT."""
    try:
        number = int(value)
    except (TypeError, ValueError) as exc:
        raise AdsReadError(f"{field} must be a whole number, got {value!r}") from exc
    if not (minimum <= number <= maximum):
        raise AdsReadError(
            f"{field} must be between {minimum} and {maximum}, got {number}"
        )
    return number


# ---------------------------------------------------------------------------
# Row shapes
# ---------------------------------------------------------------------------
# Plain frozen dataclasses rather than proto objects, so that nothing outside
# this module has to know what a proto-plus message is, and so tests can
# build rows by hand.


@dataclass(frozen=True)
class AccountSummary:
    customer_id: str
    descriptive_name: str
    currency_code: str
    time_zone: str
    is_manager: bool
    is_test_account: bool
    status: str


@dataclass(frozen=True)
class CampaignRow:
    campaign_id: str
    name: str
    status: str
    channel_type: str
    # Named with its unit, always. See CLAUDE.md: a bare `budget` is how a
    # 1,000,000x error gets past review.
    daily_budget_micros: int
    impressions: int
    clicks: int
    cost_micros: int
    conversions: float

    @property
    def average_cpc_micros(self) -> int:
        """Computed, not read from the API. See the module docstring."""
        return int(self.cost_micros / self.clicks) if self.clicks else 0


@dataclass(frozen=True)
class CampaignSummary:
    """One campaign's current state, with no metrics.

    Used to build a write preview. A preview that cannot say what it is
    changing *from* is not worth approving, and fetching this also catches
    "no such campaign" before a plan is ever issued.
    """

    campaign_id: str
    name: str
    status: str
    channel_type: str
    daily_budget_micros: int
    # The budget is a separate resource that a campaign points at, so changing
    # a budget means mutating CampaignBudget, not Campaign.
    budget_resource_name: str = ""
    budget_id: str = ""
    # How many campaigns are actively using this budget. Anything but 1 is
    # refused: 2 or more means a change would affect campaigns the preview
    # does not name, and 0 means Google did not report a count, which is not
    # the same as knowing the budget is unshared.
    #
    # This is deliberately NOT campaign_budget.explicitly_shared. That field
    # records what someone INTENDED when the budget was created - Google's
    # own definition says it "defaults to true if unspecified in a create
    # operation" - so it is true for most budgets made through the API and
    # can be false on a budget three campaigns are drawing from today.
    # reference_count is the fact.
    budget_reference_count: int = 0
    # Needed for rules.block_broad_match_with_manual_cpc.
    bidding_strategy_type: str = ""


@dataclass(frozen=True)
class AdGroupSummary:
    """One ad group's current state. Used for bid previews."""

    ad_group_id: str
    name: str
    status: str
    campaign_id: str
    campaign_name: str
    # The ad group's default max CPC. Zero when the campaign uses an
    # automated bidding strategy and no manual bid applies.
    cpc_bid_micros: int
    bidding_strategy_type: str = ""


@dataclass(frozen=True)
class SearchTermRow:
    search_term: str
    status: str
    campaign_id: str
    campaign_name: str
    ad_group_id: str
    ad_group_name: str
    impressions: int
    clicks: int
    cost_micros: int
    conversions: float


# ---------------------------------------------------------------------------
# The interface
# ---------------------------------------------------------------------------


@runtime_checkable
class AdsReader(Protocol):
    """The one interface through which anything is read from Google Ads."""

    async def accessible_customer_ids(self) -> tuple[str, ...]:
        """Customer IDs this user's own credential can reach directly.

        Wraps CustomerService.ListAccessibleCustomers, which Google
        documents as needing no login-customer-id and returning the accounts
        "directly accessible with your OAuth credentials". Note *directly*:
        a user who has access through a manager account sees the manager
        here, not each child account.
        """
        ...

    async def account_summary(self, customer_id: str) -> AccountSummary | None: ...

    async def managed_accounts(
        self, *, manager_customer_id: str
    ) -> tuple[AccountSummary, ...]:
        """Every account under this manager account, with currency and timezone.

        This is what the managed-account set is derived from, in place of a
        hand-written allowlist. The query must be sent TO the manager: asking
        a child returns only that child's own subtree.

        Raises AdsReadError on failure. It must never report an empty set for
        an account it could not read - see safety/accounts.py.
        """
        ...

    async def access_role(self, *, customer_id: str, email: str) -> str | None:
        """This user's Google Ads access role on this account, or None.

        Returns the raw enum name (ADMIN / STANDARD / READ_ONLY /
        EMAIL_ONLY / UNKNOWN / UNSPECIFIED). Mapping to a Tier is the
        resolver's job, not this module's.

        None means "the access table was readable and this person is not in
        it". A failure to read raises AdsReadError. Those must never be
        conflated - see auth/tiers.py:TierLookupError.
        """
        ...

    async def campaign_performance(
        self, *, customer_id: str, start_date: str, end_date: str, limit: int
    ) -> tuple[CampaignRow, ...]: ...

    async def campaign_by_id(
        self, *, customer_id: str, campaign_id: str
    ) -> CampaignSummary | None:
        """One campaign's current state, or None if it does not exist.

        None is a definite "no such campaign in this account". A failure to
        look raises AdsReadError, as everywhere else here.
        """
        ...

    async def ad_group_by_id(
        self, *, customer_id: str, ad_group_id: str
    ) -> AdGroupSummary | None:
        """One ad group's current state, or None if it does not exist."""
        ...

    async def search_terms(
        self,
        *,
        customer_id: str,
        start_date: str,
        end_date: str,
        limit: int,
        campaign_id: str | None = None,
    ) -> tuple[SearchTermRow, ...]: ...


# ---------------------------------------------------------------------------
# The real implementation
# ---------------------------------------------------------------------------

MAX_ROWS = 1000


class GoogleAdsReader(AdsReader):
    """Reads Google Ads using the calling user's own OAuth credential.

    Constructed once at startup, but holds no credential. `token_provider`
    is called per request and returns the current caller's access token from
    the request context, the same way `current_caller()` reads identity.
    That keeps one long-lived object without ever letting one user's
    credential leak into another user's request.
    """

    def __init__(
        self,
        *,
        settings: Settings,
        token_provider: Callable[[], str],
    ) -> None:
        self._settings = settings
        self._token_provider = token_provider

    # -- plumbing ---------------------------------------------------------

    def _service(self, name: str) -> Any:
        client = build_client(
            settings=self._settings, access_token=self._token_provider()
        )
        return client.get_service(name)

    async def _search_rows(self, *, customer_id: str, query: str) -> list[Any]:
        """Run one GAQL query and return its rows.

        The blocking gRPC call is pushed to a worker thread. Errors are
        re-raised as AdsReadError with the Google failure text preserved -
        never downgraded to an empty result.
        """

        def run() -> list[Any]:
            service = self._service("GoogleAdsService")
            pager = service.search(customer_id=customer_id, query=query)
            return list(pager)

        try:
            return await asyncio.to_thread(run)
        except AdsReadError:
            raise
        except Exception as exc:  # noqa: BLE001 - re-raised, never swallowed
            raise AdsReadError(
                f"Google Ads read failed for customer {customer_id}: {exc}"
            ) from exc

    # -- interface --------------------------------------------------------

    async def accessible_customer_ids(self) -> tuple[str, ...]:
        def run() -> tuple[str, ...]:
            service = self._service("CustomerService")
            response = service.list_accessible_customers()
            # Resource names come back as "customers/{customer_id}".
            return tuple(
                name.split("/")[-1] for name in response.resource_names
            )

        try:
            return await asyncio.to_thread(run)
        except Exception as exc:  # noqa: BLE001
            raise AdsReadError(f"could not list accessible customers: {exc}") from exc

    async def account_summary(self, customer_id: str) -> AccountSummary | None:
        customer_id = _literal(customer_id, field="customer_id")
        query = (
            "SELECT customer.id, customer.descriptive_name, "
            "customer.currency_code, customer.time_zone, customer.manager, "
            "customer.test_account, customer.status "
            "FROM customer LIMIT 1"
        )
        rows = await self._search_rows(customer_id=customer_id, query=query)
        if not rows:
            return None
        customer = rows[0].customer
        return AccountSummary(
            customer_id=str(customer.id),
            descriptive_name=customer.descriptive_name or "",
            currency_code=customer.currency_code or "",
            time_zone=customer.time_zone or "",
            is_manager=bool(customer.manager),
            is_test_account=bool(customer.test_account),
            status=_enum_name(customer.status),
        )

    async def managed_accounts(
        self, *, manager_customer_id: str
    ) -> tuple[AccountSummary, ...]:
        manager_customer_id = _literal(
            manager_customer_id, field="manager_customer_id"
        )
        # customer_client lists the manager's whole subtree, one row per
        # descendant, and carries the currency and timezone with it. That is
        # why one query can answer both "is this account ours?" and "in what
        # currency?" - see safety/accounts.py.
        #
        # CANCELLED and CLOSED accounts are filtered out here rather than
        # downstream: they cannot serve ads, so offering them would only
        # produce a confusing failure later.
        query = (
            "SELECT customer_client.id, customer_client.descriptive_name, "
            "customer_client.currency_code, customer_client.time_zone, "
            "customer_client.manager, customer_client.test_account, "
            "customer_client.status "
            "FROM customer_client "
            "WHERE customer_client.status = 'ENABLED'"
        )
        rows = await self._search_rows(
            customer_id=manager_customer_id, query=query
        )
        return tuple(
            AccountSummary(
                customer_id=str(row.customer_client.id),
                descriptive_name=row.customer_client.descriptive_name or "",
                currency_code=row.customer_client.currency_code or "",
                time_zone=row.customer_client.time_zone or "",
                is_manager=bool(row.customer_client.manager),
                is_test_account=bool(row.customer_client.test_account),
                status=_enum_name(row.customer_client.status),
            )
            for row in rows
        )

    async def access_role(self, *, customer_id: str, email: str) -> str | None:
        customer_id = _literal(customer_id, field="customer_id")

        # The email is matched in Python rather than in a GAQL WHERE clause.
        # Two reasons. It sidesteps whether customer_user_access.email_address
        # is a filterable field, which could not be established from the
        # documentation (the field reference pages are JavaScript-rendered and
        # the protos do not record filterability). And it keeps a
        # user-supplied-looking value out of the query string entirely.
        query = (
            "SELECT customer_user_access.user_id, "
            "customer_user_access.email_address, "
            "customer_user_access.access_role "
            "FROM customer_user_access"
        )
        rows = await self._search_rows(customer_id=customer_id, query=query)

        wanted = email.strip().lower()
        for row in rows:
            access = row.customer_user_access
            if (access.email_address or "").strip().lower() == wanted:
                return _enum_name(access.access_role)
        return None

    async def campaign_performance(
        self, *, customer_id: str, start_date: str, end_date: str, limit: int
    ) -> tuple[CampaignRow, ...]:
        customer_id = _literal(customer_id, field="customer_id")
        start_date = _literal(start_date, field="start_date")
        end_date = _literal(end_date, field="end_date")
        row_limit = _int_literal(limit, field="limit", minimum=1, maximum=MAX_ROWS)

        # segments.date appears only in WHERE, not SELECT, so metrics come
        # back aggregated across the range - one row per campaign, not one
        # row per campaign per day.
        query = (
            "SELECT campaign.id, campaign.name, campaign.status, "
            "campaign.advertising_channel_type, "
            "campaign_budget.amount_micros, "
            "metrics.impressions, metrics.clicks, metrics.cost_micros, "
            "metrics.conversions "
            "FROM campaign "
            f"WHERE segments.date BETWEEN '{start_date}' AND '{end_date}' "
            "AND campaign.status != 'REMOVED' "
            "ORDER BY metrics.cost_micros DESC "
            f"LIMIT {row_limit}"
        )
        rows = await self._search_rows(customer_id=customer_id, query=query)
        return tuple(
            CampaignRow(
                campaign_id=str(row.campaign.id),
                name=row.campaign.name or "",
                status=_enum_name(row.campaign.status),
                channel_type=_enum_name(row.campaign.advertising_channel_type),
                daily_budget_micros=int(row.campaign_budget.amount_micros or 0),
                impressions=int(row.metrics.impressions or 0),
                clicks=int(row.metrics.clicks or 0),
                cost_micros=int(row.metrics.cost_micros or 0),
                conversions=float(row.metrics.conversions or 0.0),
            )
            for row in rows
        )

    async def campaign_by_id(
        self, *, customer_id: str, campaign_id: str
    ) -> CampaignSummary | None:
        customer_id = _literal(customer_id, field="customer_id")
        safe_campaign = _literal(campaign_id, field="campaign_id")

        query = (
            "SELECT campaign.id, campaign.name, campaign.status, "
            "campaign.advertising_channel_type, campaign.bidding_strategy_type, "
            "campaign_budget.resource_name, campaign_budget.id, "
            "campaign_budget.amount_micros, campaign_budget.reference_count "
            "FROM campaign "
            f"WHERE campaign.id = {safe_campaign} "
            "LIMIT 1"
        )
        rows = await self._search_rows(customer_id=customer_id, query=query)
        if not rows:
            return None

        row = rows[0]
        return CampaignSummary(
            campaign_id=str(row.campaign.id),
            name=row.campaign.name or "",
            status=_enum_name(row.campaign.status),
            channel_type=_enum_name(row.campaign.advertising_channel_type),
            daily_budget_micros=int(row.campaign_budget.amount_micros or 0),
            budget_resource_name=row.campaign_budget.resource_name or "",
            budget_id=str(row.campaign_budget.id or ""),
            budget_reference_count=int(row.campaign_budget.reference_count or 0),
            bidding_strategy_type=_enum_name(row.campaign.bidding_strategy_type),
        )

    async def ad_group_by_id(
        self, *, customer_id: str, ad_group_id: str
    ) -> AdGroupSummary | None:
        customer_id = _literal(customer_id, field="customer_id")
        safe_ad_group = _literal(ad_group_id, field="ad_group_id")

        query = (
            "SELECT ad_group.id, ad_group.name, ad_group.status, "
            "ad_group.cpc_bid_micros, "
            "campaign.id, campaign.name, campaign.bidding_strategy_type "
            "FROM ad_group "
            f"WHERE ad_group.id = {safe_ad_group} "
            "LIMIT 1"
        )
        rows = await self._search_rows(customer_id=customer_id, query=query)
        if not rows:
            return None

        row = rows[0]
        return AdGroupSummary(
            ad_group_id=str(row.ad_group.id),
            name=row.ad_group.name or "",
            status=_enum_name(row.ad_group.status),
            campaign_id=str(row.campaign.id),
            campaign_name=row.campaign.name or "",
            cpc_bid_micros=int(row.ad_group.cpc_bid_micros or 0),
            bidding_strategy_type=_enum_name(row.campaign.bidding_strategy_type),
        )

    async def search_terms(
        self,
        *,
        customer_id: str,
        start_date: str,
        end_date: str,
        limit: int,
        campaign_id: str | None = None,
    ) -> tuple[SearchTermRow, ...]:
        customer_id = _literal(customer_id, field="customer_id")
        start_date = _literal(start_date, field="start_date")
        end_date = _literal(end_date, field="end_date")
        row_limit = _int_literal(limit, field="limit", minimum=1, maximum=MAX_ROWS)

        campaign_filter = ""
        if campaign_id is not None:
            safe_campaign = _literal(campaign_id, field="campaign_id")
            campaign_filter = f"AND campaign.id = {safe_campaign} "

        query = (
            "SELECT search_term_view.search_term, search_term_view.status, "
            "campaign.id, campaign.name, ad_group.id, ad_group.name, "
            "metrics.impressions, metrics.clicks, metrics.cost_micros, "
            "metrics.conversions "
            "FROM search_term_view "
            f"WHERE segments.date BETWEEN '{start_date}' AND '{end_date}' "
            f"{campaign_filter}"
            "ORDER BY metrics.cost_micros DESC "
            f"LIMIT {row_limit}"
        )
        rows = await self._search_rows(customer_id=customer_id, query=query)
        return tuple(
            SearchTermRow(
                search_term=row.search_term_view.search_term or "",
                status=_enum_name(row.search_term_view.status),
                campaign_id=str(row.campaign.id),
                campaign_name=row.campaign.name or "",
                ad_group_id=str(row.ad_group.id),
                ad_group_name=row.ad_group.name or "",
                impressions=int(row.metrics.impressions or 0),
                clicks=int(row.metrics.clicks or 0),
                cost_micros=int(row.metrics.cost_micros or 0),
                conversions=float(row.metrics.conversions or 0.0),
            )
            for row in rows
        )


def _enum_name(value: object) -> str:
    """Enum member name as a plain string.

    proto-plus enums carry `.name`; a fake in a test may hand back a plain
    string. Both are accepted so tests do not have to import proto types.
    """
    return getattr(value, "name", None) or str(value)
