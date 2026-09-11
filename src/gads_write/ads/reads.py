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
  campaign              id, name, status, advertising_channel_type,
                        geo_target_type_setting.positive_geo_target_type
  campaign_budget       amount_micros
  campaign_criterion    criterion_id, location.geo_target_constant,
                        negative, status, display_name
  geo_target_constant   id, resource_name, name, canonical_name,
                        country_code, target_type (a STRING, not an enum),
                        status
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
import hashlib
import logging
import re
from dataclasses import dataclass
from typing import Any, Callable, Protocol, runtime_checkable

from ..settings import Settings
from .client import NO_MANAGER, build_client

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


# Free text that reaches a GAQL LIKE clause. Deliberately far narrower than
# _SAFE_LITERAL: no quote characters (which could close the string literal),
# no backslash, and none of the four characters GAQL treats as LIKE wildcards
# (`%`, `_`, `[`, `]` - the grammar escapes those by bracketing them, which is
# a scheme we have not verified and are not going to implement blind). What is
# left cannot change the shape of a query or the meaning of the pattern.
#
# The cost is that a name containing an apostrophe - "Cote d'Ivoire" - cannot
# be searched for here. Geo target constant names are English by definition
# (the v25 proto says "Geo target constant English name"), so this is a narrow
# loss, and refusing beats inventing an escaping scheme for a grammar whose
# escape rules are not documented.
# The safety property is the character SET, not where in the string a
# character sits. Only the FIRST character is pinned to alphanumeric, so a
# name cannot start with punctuation; "Washington, D.C." ends in a period and
# is a real place.
_SAFE_TEXT = re.compile(r"^[A-Za-z0-9][A-Za-z0-9 .,&-]*$")


def _text_literal(value: object, *, field: str) -> str:
    """Assert a free-text value is safe inside a quoted GAQL literal.

    Same two-layer arrangement as `_literal`: tools validate first and return
    a message a human can act on, this is the backstop against a validation
    step somebody forgets to call.
    """
    text = str(value).strip()
    if not _SAFE_TEXT.fullmatch(text):
        raise AdsReadError(
            f"refusing to build a query: {field}={text!r} contains characters "
            "that are not permitted in a GAQL text literal. Use letters, "
            "digits, spaces, and . , & -"
        )
    return text


def _digit_literal(value: object, *, field: str) -> str:
    """Digits only. Stricter than `_literal`, which also permits letters.

    Used for ids that are composed into a resource name, where a letter would
    produce a resource name that is merely wrong rather than dangerous - but
    "merely wrong" is not a category this module trades in.
    """
    text = str(value).strip()
    if not text.isdigit():
        raise AdsReadError(f"{field} must be numeric, got {text!r}")
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
    # Whether the campaign serves to people IN its targeted locations
    # (PRESENCE) or also to people merely INTERESTED in them
    # (PRESENCE_OR_INTEREST, Google's default). Campaign-level, not a
    # criterion - which is why adding a location does not restrict a campaign
    # on its own. Empty when Google did not report it.
    positive_geo_target_type: str = ""


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
    # The CAMPAIGN's status, not this ad group's. An ENABLED ad group inside a
    # PAUSED campaign still does not serve, so enable_ad_group has to be able
    # to say so - otherwise it reports success on a change with no visible
    # effect, and the person is left wondering what went wrong.
    campaign_status: str = ""


@dataclass(frozen=True)
class GeoTargetRow:
    """One geo target constant - a place Google will let you target.

    `canonical_name` is the field that earns this row its existence. "Delhi"
    in Google's data is a city, a state AND a union territory, as three
    separate targets with three different ids; only
    "New Delhi,Delhi,India" against "Delhi,India" tells them apart. Anything
    that resolved a bare name to one of them silently would be picking, on
    someone's behalf, which part of a country their money is spent in.
    """

    geo_target_id: str
    resource_name: str
    name: str
    canonical_name: str
    country_code: str
    # A plain string in the API, not an enum - "City", "Region", "Country",
    # "Postal Code" and so on. Verified in the v25 proto.
    target_type: str
    status: str

    def describe(self) -> str:
        """The one line a human needs to tell two 'Delhi's apart."""
        label = self.canonical_name or self.name
        return f"{label} ({self.target_type})" if self.target_type else label


@dataclass(frozen=True)
class CampaignLocationRow:
    """One location criterion already on a campaign.

    Read before adding any, for two reasons. A mutate is all-or-nothing here,
    so one duplicate would fail the whole batch; and a preview that offers to
    add a location the campaign already targets is a preview that lies.
    """

    criterion_id: str
    # Resource name, e.g. "geoTargetConstants/2356".
    geo_target_constant: str
    geo_target_id: str
    # True means the location is EXCLUDED, not targeted. See the v25 proto:
    # "Whether to target (false) or exclude (true) the criterion."
    negative: bool
    status: str
    display_name: str


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

    async def run_query(
        self, *, customer_id: str, query: str
    ) -> tuple[dict, ...]:
        """Run a caller-supplied GAQL query and return its rows as dicts.

        The one read here whose query this code does not own. The query is
        validated in safety/validators.py before it arrives; the account has
        already been checked against the MCC by the gate; and the call is made
        with the CALLER'S own token, so Google enforces what they may read.

        `search()` cannot mutate - the architecture test pins mutations to
        ads/executor.py - so this widens what can be READ, never what can be
        changed.
        """
        ...

    async def list_campaigns(self, customer_id: str) -> tuple[CampaignSummary, ...]:
        """Every campaign in the account, whether or not it has ever served.

        Deliberately NOT a performance report. `campaign_performance` filters
        on `segments.date`, so Google returns rows only for campaign-days that
        have data - which makes a newly created or long-dormant campaign
        invisible. Every write tool needs an id, so that left no way to find
        one without opening the Google Ads UI.
        """
        ...

    async def list_ad_groups(
        self, *, customer_id: str, campaign_id: str | None = None
    ) -> tuple[AdGroupSummary, ...]:
        """Ad groups in the account, optionally within one campaign.

        Same reason as `list_campaigns`: `add_keyword` and
        `update_ad_group_bid` both need an ad_group_id, and nothing else here
        returns one for an ad group that has never served.
        """
        ...

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

    async def find_geo_targets(
        self,
        *,
        customer_id: str,
        query: str,
        country_code: str | None = None,
        limit: int = 50,
    ) -> tuple[GeoTargetRow, ...]:
        """Places whose English name contains `query`.

        The disambiguation step. Returns every match rather than a best one,
        because "Delhi" legitimately matches several different places and this
        server does not choose between them on anyone's behalf.
        """
        ...

    async def geo_targets_by_id(
        self, *, customer_id: str, geo_target_ids: list[str]
    ) -> tuple[GeoTargetRow, ...]:
        """The named geo targets, for a preview that says where money goes.

        Filtered on `geo_target_constant.resource_name`, which is the one
        filter on this resource Google's own documentation demonstrates.
        A missing id simply does not come back; the caller compares what it
        asked for against what it got.
        """
        ...

    async def campaign_locations(
        self, *, customer_id: str, campaign_id: str
    ) -> tuple[CampaignLocationRow, ...]:
        """Location criteria already on a campaign, targeted or excluded."""
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

# How many places a name search will return. Enough that a real search is not
# truncated, few enough that a list of them is still something a person reads
# rather than skims.
MAX_LOCATION_MATCHES = 50


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
        # (caller, customer_id) -> True when the caller reaches this account
        # DIRECTLY and the manager header must be omitted. Populated by the
        # first successful read; see _search_rows.
        self._direct_access: dict[tuple[str, str], bool] = {}

    # -- plumbing ---------------------------------------------------------

    def _service(self, name: str, *, manager: Any = None) -> Any:
        client = build_client(
            settings=self._settings,
            access_token=self._token_provider(),
            login_customer_id=manager,
        )
        return client.get_service(name)

    def _caller_key(self) -> str:
        """A stable, non-reversible handle for the current caller.

        The direct-access cache is per person: whether a manager header works
        depends on whose token is asking. Hashed so no credential is used as a
        dictionary key or reachable from a memory dump of the cache.
        """
        return hashlib.sha256(self._token_provider().encode()).hexdigest()[:16]

    async def _search_rows(self, *, customer_id: str, query: str) -> list[Any]:
        """Run one GAQL query and return its rows.

        Tries THROUGH THE MANAGER first, and falls back to no manager at all
        if Google refuses on permissions.

        Most people here hold a single access row on the manager and none on
        the accounts beneath it, so the manager framing is right for them and
        is tried first. But somebody granted access directly to one
        sub-account has no standing on the manager, and naming it makes Google
        refuse a request for an account they legitimately hold - which locked
        exactly such a user out of their own account. Google only requires the
        header when reaching a client customer THROUGH a manager.

        Which framing worked is remembered per caller and account, so the
        second request costs one round trip rather than two.

        The blocking gRPC call is pushed to a worker thread. Errors are
        re-raised as AdsReadError with the Google failure text preserved -
        never downgraded to an empty result.
        """
        cache_key = (self._caller_key(), customer_id)
        direct = self._direct_access.get(cache_key)

        def run(manager: Any) -> list[Any]:
            service = self._service("GoogleAdsService", manager=manager)
            pager = service.search(customer_id=customer_id, query=query)
            return list(pager)

        attempts: list[Any] = (
            [NO_MANAGER] if direct else [None, NO_MANAGER]
        )
        last: Exception | None = None

        for index, manager in enumerate(attempts):
            try:
                rows = await asyncio.to_thread(run, manager)
            except Exception as exc:  # noqa: BLE001 - re-raised below
                last = exc
                is_last = index == len(attempts) - 1
                if is_last or not _is_permission_error(exc):
                    break
                continue
            self._direct_access[cache_key] = manager is NO_MANAGER
            return rows

        raise AdsReadError(
            f"Google Ads read failed for customer {customer_id}: {last}"
        ) from last

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

    async def run_query(
        self, *, customer_id: str, query: str
    ) -> tuple[dict, ...]:
        customer_id = _literal(customer_id, field="customer_id")
        rows = await self._search_rows(customer_id=customer_id, query=str(query))
        return tuple(
            # Only fields the row actually carries, and enum NAMES rather than
            # their integers - a model reading "ENABLED" needs no lookup table,
            # and the default-filled version of this is mostly noise.
            type(row).to_dict(
                row,
                including_default_value_fields=False,
                use_integers_for_enums=False,
            )
            for row in rows
        )

    async def list_campaigns(self, customer_id: str) -> tuple[CampaignSummary, ...]:
        customer_id = _literal(customer_id, field="customer_id")
        # No date segment, so campaigns with no statistics still come back.
        query = (
            f"SELECT {CAMPAIGN_FIELDS} "
            "FROM campaign "
            "WHERE campaign.status != 'REMOVED' "
            "ORDER BY campaign.name "
            f"LIMIT {MAX_ROWS}"
        )
        rows = await self._search_rows(customer_id=customer_id, query=query)
        return tuple(_campaign_summary(row) for row in rows)

    async def list_ad_groups(
        self, *, customer_id: str, campaign_id: str | None = None
    ) -> tuple[AdGroupSummary, ...]:
        customer_id = _literal(customer_id, field="customer_id")
        where = ["ad_group.status != 'REMOVED'", "campaign.status != 'REMOVED'"]
        if campaign_id is not None:
            safe_campaign = _literal(campaign_id, field="campaign_id")
            where.append(f"campaign.id = {safe_campaign}")

        query = (
            f"SELECT {AD_GROUP_FIELDS} "
            "FROM ad_group "
            "WHERE " + " AND ".join(where) + " "
            "ORDER BY campaign.name, ad_group.name "
            f"LIMIT {MAX_ROWS}"
        )
        rows = await self._search_rows(customer_id=customer_id, query=query)
        return tuple(_ad_group_summary(row) for row in rows)

    async def campaign_by_id(
        self, *, customer_id: str, campaign_id: str
    ) -> CampaignSummary | None:
        customer_id = _literal(customer_id, field="customer_id")
        safe_campaign = _literal(campaign_id, field="campaign_id")

        query = (
            f"SELECT {CAMPAIGN_FIELDS} "
            "FROM campaign "
            f"WHERE campaign.id = {safe_campaign} "
            "LIMIT 1"
        )
        rows = await self._search_rows(customer_id=customer_id, query=query)
        if not rows:
            return None

        return _campaign_summary(rows[0])

    async def ad_group_by_id(
        self, *, customer_id: str, ad_group_id: str
    ) -> AdGroupSummary | None:
        customer_id = _literal(customer_id, field="customer_id")
        safe_ad_group = _literal(ad_group_id, field="ad_group_id")

        # No status filter, deliberately. A REMOVED ad group has to come back
        # so a write tool can refuse it with a message that says why, rather
        # than reporting "no such ad group".
        query = (
            f"SELECT {AD_GROUP_FIELDS} "
            "FROM ad_group "
            f"WHERE ad_group.id = {safe_ad_group} "
            "LIMIT 1"
        )
        rows = await self._search_rows(customer_id=customer_id, query=query)
        if not rows:
            return None

        return _ad_group_summary(rows[0])

    async def find_geo_targets(
        self,
        *,
        customer_id: str,
        query: str,
        country_code: str | None = None,
        limit: int = MAX_LOCATION_MATCHES,
    ) -> tuple[GeoTargetRow, ...]:
        customer_id = _literal(customer_id, field="customer_id")
        text = _text_literal(query, field="query")
        row_limit = _int_literal(
            limit, field="limit", minimum=1, maximum=MAX_LOCATION_MATCHES
        )

        # REMOVAL_PLANNED targets still resolve but Google is retiring them,
        # so offering one would be handing somebody a target that stops
        # working. ENABLED is the only status worth suggesting.
        where = [
            f"geo_target_constant.name LIKE '%{text}%'",
            "geo_target_constant.status = 'ENABLED'",
        ]
        if country_code is not None:
            where.append(
                "geo_target_constant.country_code = "
                f"'{_literal(country_code, field='country_code')}'"
            )

        # No ORDER BY. Sorting happens in Python, which needs no assumption
        # about which fields of this resource are sortable.
        gaql = (
            f"SELECT {GEO_TARGET_FIELDS} "
            "FROM geo_target_constant "
            "WHERE " + " AND ".join(where) + " "
            f"LIMIT {row_limit}"
        )
        rows = await self._search_rows(customer_id=customer_id, query=gaql)
        return tuple(sorted(
            (_geo_target_row(row) for row in rows),
            key=lambda row: (row.country_code, row.canonical_name),
        ))

    async def geo_targets_by_id(
        self, *, customer_id: str, geo_target_ids: list[str]
    ) -> tuple[GeoTargetRow, ...]:
        customer_id = _literal(customer_id, field="customer_id")
        safe = [
            _digit_literal(value, field="geo_target_constant_id")
            for value in geo_target_ids
        ]
        if not safe:
            return ()

        # Composed from digit-only ids, so the resource names cannot carry
        # anything that would escape the literal. Resource-name filtering is
        # the one filter on this resource Google's own documentation shows.
        names = ", ".join(f"'geoTargetConstants/{value}'" for value in safe)
        gaql = (
            f"SELECT {GEO_TARGET_FIELDS} "
            "FROM geo_target_constant "
            f"WHERE geo_target_constant.resource_name IN ({names}) "
            f"LIMIT {len(safe)}"
        )
        rows = await self._search_rows(customer_id=customer_id, query=gaql)
        return tuple(_geo_target_row(row) for row in rows)

    async def campaign_locations(
        self, *, customer_id: str, campaign_id: str
    ) -> tuple[CampaignLocationRow, ...]:
        customer_id = _literal(customer_id, field="customer_id")
        safe_campaign = _literal(campaign_id, field="campaign_id")

        gaql = (
            "SELECT campaign_criterion.criterion_id, "
            "campaign_criterion.location.geo_target_constant, "
            "campaign_criterion.negative, campaign_criterion.status, "
            "campaign_criterion.display_name "
            "FROM campaign_criterion "
            f"WHERE campaign.id = {safe_campaign} "
            "AND campaign_criterion.type = 'LOCATION' "
            "AND campaign_criterion.status != 'REMOVED' "
            f"LIMIT {MAX_ROWS}"
        )
        rows = await self._search_rows(customer_id=customer_id, query=gaql)
        return tuple(
            CampaignLocationRow(
                criterion_id=str(row.campaign_criterion.criterion_id or ""),
                geo_target_constant=(
                    row.campaign_criterion.location.geo_target_constant or ""
                ),
                geo_target_id=_geo_target_id(
                    row.campaign_criterion.location.geo_target_constant or ""
                ),
                negative=bool(row.campaign_criterion.negative),
                status=_enum_name(row.campaign_criterion.status),
                display_name=row.campaign_criterion.display_name or "",
            )
            for row in rows
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


# The campaign fields every campaign read selects. One constant so a field
# added for the single-campaign lookup cannot go missing from the listing,
# which would surface as an AttributeError on a row rather than a clear error.
CAMPAIGN_FIELDS = (
    "campaign.id, campaign.name, campaign.status, "
    "campaign.advertising_channel_type, campaign.bidding_strategy_type, "
    "campaign.geo_target_type_setting.positive_geo_target_type, "
    "campaign_budget.resource_name, campaign_budget.id, "
    "campaign_budget.amount_micros, campaign_budget.reference_count"
)

# The ad group fields every ad group read selects. Same reason as
# CAMPAIGN_FIELDS: a field added for the single-ad-group lookup cannot then go
# missing from the listing, which would surface as an AttributeError on a row
# rather than a clear error.
AD_GROUP_FIELDS = (
    "ad_group.id, ad_group.name, ad_group.status, ad_group.cpc_bid_micros, "
    "campaign.id, campaign.name, campaign.status, "
    "campaign.bidding_strategy_type"
)

# The geo target fields every geo query selects. One constant so the
# by-name search and the by-id lookup cannot disagree about a field, which
# would surface as an AttributeError on a row rather than a clear error.
GEO_TARGET_FIELDS = (
    "geo_target_constant.id, geo_target_constant.resource_name, "
    "geo_target_constant.name, geo_target_constant.canonical_name, "
    "geo_target_constant.country_code, geo_target_constant.target_type, "
    "geo_target_constant.status"
)


def _campaign_summary(row: Any) -> CampaignSummary:
    """Map one campaign row. Shared by `campaign_by_id` and `list_campaigns`
    so the two can never disagree about a field."""
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
        positive_geo_target_type=_enum_name(
            row.campaign.geo_target_type_setting.positive_geo_target_type
        ),
    )


def _ad_group_summary(row: Any) -> AdGroupSummary:
    """Map one ad group row. Shared by `ad_group_by_id` and `list_ad_groups`
    so the two can never disagree about a field."""
    return AdGroupSummary(
        ad_group_id=str(row.ad_group.id),
        name=row.ad_group.name or "",
        status=_enum_name(row.ad_group.status),
        campaign_id=str(row.campaign.id),
        campaign_name=row.campaign.name or "",
        cpc_bid_micros=int(row.ad_group.cpc_bid_micros or 0),
        bidding_strategy_type=_enum_name(row.campaign.bidding_strategy_type),
        campaign_status=_enum_name(row.campaign.status),
    )


def _geo_target_row(row: Any) -> GeoTargetRow:
    """Map one geo target constant row. Shared by both geo queries."""
    constant = row.geo_target_constant
    return GeoTargetRow(
        geo_target_id=str(constant.id or ""),
        resource_name=constant.resource_name or "",
        name=constant.name or "",
        canonical_name=constant.canonical_name or "",
        country_code=constant.country_code or "",
        target_type=constant.target_type or "",
        status=_enum_name(constant.status),
    )


def _geo_target_id(resource_name: str) -> str:
    """The numeric id out of "geoTargetConstants/2356".

    Format verified twice: the v25 GeoTargetConstant proto documents it, and
    GeoTargetConstantServiceClient.geo_target_constant_path builds it.
    """
    tail = str(resource_name or "").rsplit("/", 1)[-1]
    return tail if tail.isdigit() else ""


def _is_permission_error(exc: Exception) -> bool:
    """Whether Google refused this for permissions rather than anything else.

    Matched on the text because the exception type varies across the gRPC and
    google-ads layers, and only the refusal reason decides whether retrying
    without the manager header is worth a round trip. Any other failure -
    a bad query, an outage - is surfaced immediately rather than retried.
    """
    text = str(exc).upper()
    return "PERMISSION_DENIED" in text or "USER_PERMISSION_DENIED" in text


def _enum_name(value: object) -> str:
    """Enum member name as a plain string.

    proto-plus enums carry `.name`; a fake in a test may hand back a plain
    string. Both are accepted so tests do not have to import proto types.
    """
    return getattr(value, "name", None) or str(value)
