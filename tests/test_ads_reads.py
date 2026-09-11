"""The read path: query construction, injection refusal, row shaping.

No network. A fake service object stands in for Google, which lets these
tests assert on the exact GAQL that would have been sent - the thing that is
otherwise invisible until it fails in production.

The most valuable test in this file is
`test_every_selected_field_exists_in_the_pinned_api_version`. It parses the
real queries and checks each field against the generated protos, so a Google
field rename, or someone bumping API_VERSION without looking, fails the
build instead of failing a marketing report.
"""

from __future__ import annotations

import re
from types import SimpleNamespace

import pytest

from gads_write.ads.reads import (
    AdsReadError,
    GoogleAdsReader,
    MAX_ROWS,
    _literal,
)


class FakeService:
    """Stands in for GoogleAdsServiceClient / CustomerServiceClient."""

    def __init__(self, rows: list | None = None, resource_names: list | None = None) -> None:
        self.rows = rows or []
        self.resource_names = resource_names or []
        self.queries: list[tuple[str, str]] = []
        self.explode: Exception | None = None
        # login_customer_id passed for each call: None = through the manager,
        # NO_MANAGER = direct.
        self.managers: list = []

    def search(self, *, customer_id: str, query: str):
        self.queries.append((customer_id, query))
        if self.explode:
            raise self.explode
        return iter(self.rows)

    def list_accessible_customers(self):
        if self.explode:
            raise self.explode
        return SimpleNamespace(resource_names=self.resource_names)


class StubReader(GoogleAdsReader):
    """The real reader, with only the service lookup replaced.

    Subclassing rather than mocking on purpose: every query string, every
    literal assertion and every row mapping below is the production code
    path. Only the gRPC call at the very bottom is fake.
    """

    def __init__(self, service: FakeService) -> None:
        self._fake = service
        self._settings = SimpleNamespace(
            developer_token="devtoken", login_customer_id="9999999999"
        )
        self._token_provider = lambda: "ya29.fake"
        # Same state the real __init__ builds. _search_rows remembers per
        # caller and account whether the manager header must be omitted.
        self._direct_access: dict[tuple[str, str], bool] = {}

    def _service(self, name: str, *, manager=None):  # noqa: D102
        # Record which framing was used so a test can assert on the fallback.
        self._fake.managers.append(manager)
        return self._fake


def _metrics(impressions=0, clicks=0, cost_micros=0, conversions=0.0):
    return SimpleNamespace(
        impressions=impressions,
        clicks=clicks,
        cost_micros=cost_micros,
        conversions=conversions,
    )


def _campaign_row(**kwargs):
    return SimpleNamespace(
        campaign=SimpleNamespace(
            id=kwargs.get("id", 1),
            name=kwargs.get("name", "Brand"),
            status=SimpleNamespace(name="ENABLED"),
            advertising_channel_type=SimpleNamespace(name="SEARCH"),
        ),
        campaign_budget=SimpleNamespace(
            amount_micros=kwargs.get("budget_micros", 500_000_000)
        ),
        metrics=_metrics(**kwargs.get("metrics", {})),
    )


# ---------------------------------------------------------------------------
# GAQL literal safety
# ---------------------------------------------------------------------------

@pytest.mark.parametrize(
    "hostile",
    [
        "1234567890' OR '1'='1",
        "2026-01-01' AND campaign.id > '0",
        "abc; DROP",
        "a b",
        "'",
        "",
    ],
)
def test_literals_that_could_change_a_query_are_refused(hostile: str) -> None:
    with pytest.raises(AdsReadError):
        _literal(hostile, field="customer_id")


def test_ordinary_literals_pass() -> None:
    assert _literal("1234567890", field="customer_id") == "1234567890"
    assert _literal("2026-08-31", field="start_date") == "2026-08-31"


async def test_an_injected_date_never_reaches_the_query() -> None:
    service = FakeService()
    reader = StubReader(service)

    with pytest.raises(AdsReadError):
        await reader.campaign_performance(
            customer_id="1234567890",
            start_date="2026-01-01' OR '1'='1",
            end_date="2026-01-31",
            limit=10,
        )

    # The point: it failed BEFORE anything was sent.
    assert service.queries == []


async def test_a_limit_beyond_the_cap_is_refused() -> None:
    reader = StubReader(FakeService())
    with pytest.raises(AdsReadError):
        await reader.campaign_performance(
            customer_id="1234567890",
            start_date="2026-01-01",
            end_date="2026-01-31",
            limit=MAX_ROWS + 1,
        )


# ---------------------------------------------------------------------------
# row shaping
# ---------------------------------------------------------------------------

async def test_campaign_rows_are_mapped_with_units_intact() -> None:
    service = FakeService(
        rows=[
            _campaign_row(
                id=55,
                name="Brand - Exact",
                budget_micros=12_000_000_000,
                metrics={"impressions": 100, "clicks": 10, "cost_micros": 5_000_000},
            )
        ]
    )
    rows = await StubReader(service).campaign_performance(
        customer_id="1234567890",
        start_date="2026-08-01",
        end_date="2026-08-31",
        limit=50,
    )

    assert len(rows) == 1
    row = rows[0]
    assert row.campaign_id == "55"
    assert row.name == "Brand - Exact"
    assert row.status == "ENABLED"
    # Micros stay micros all the way through the reader. Formatting to
    # currency happens once, at the tool edge.
    assert row.daily_budget_micros == 12_000_000_000
    assert row.cost_micros == 5_000_000


async def test_average_cpc_is_computed_not_read() -> None:
    """The API's average_cpc is deliberately unused - see ads/reads.py.

    cost 5,000,000 micros over 10 clicks is 500,000 micros per click.
    """
    service = FakeService(
        rows=[_campaign_row(metrics={"clicks": 10, "cost_micros": 5_000_000})]
    )
    rows = await StubReader(service).campaign_performance(
        customer_id="1234567890", start_date="2026-08-01", end_date="2026-08-31", limit=5
    )
    assert rows[0].average_cpc_micros == 500_000


async def test_average_cpc_does_not_divide_by_zero() -> None:
    service = FakeService(
        rows=[_campaign_row(metrics={"clicks": 0, "cost_micros": 0})]
    )
    rows = await StubReader(service).campaign_performance(
        customer_id="1234567890", start_date="2026-08-01", end_date="2026-08-31", limit=5
    )
    assert rows[0].average_cpc_micros == 0


async def test_accessible_customer_ids_are_stripped_of_the_resource_prefix() -> None:
    service = FakeService(
        resource_names=["customers/1111111111", "customers/2222222222"]
    )
    ids = await StubReader(service).accessible_customer_ids()
    assert ids == ("1111111111", "2222222222")


# ---------------------------------------------------------------------------
# failure is never an empty result
# ---------------------------------------------------------------------------

async def test_a_failed_read_raises_rather_than_returning_nothing() -> None:
    """"This campaign spent nothing" and "we could not find out" must not
    look the same. Returning [] on failure is how someone concludes an
    account is idle and safe to raise."""
    service = FakeService()
    service.explode = RuntimeError("DEADLINE_EXCEEDED")

    with pytest.raises(AdsReadError) as caught:
        await StubReader(service).campaign_performance(
            customer_id="1234567890",
            start_date="2026-08-01",
            end_date="2026-08-31",
            limit=5,
        )
    assert "DEADLINE_EXCEEDED" in str(caught.value)


# ---------------------------------------------------------------------------
# access_role
# ---------------------------------------------------------------------------

def _access_row(email: str, role: str):
    return SimpleNamespace(
        customer_user_access=SimpleNamespace(
            email_address=email, access_role=SimpleNamespace(name=role)
        )
    )


async def test_access_role_matches_the_caller_case_insensitively() -> None:
    service = FakeService(
        rows=[
            _access_row("someone.else@example.com", "ADMIN"),
            _access_row("Target@Example.COM", "STANDARD"),
        ]
    )
    role = await StubReader(service).access_role(
        customer_id="1234567890", email="target@example.com"
    )
    assert role == "STANDARD"


async def test_access_role_is_none_when_the_table_is_readable_but_empty() -> None:
    """None means a definite "not in the list", which the resolver maps to
    tier none. An unreadable table raises instead - a different thing."""
    role = await StubReader(FakeService(rows=[])).access_role(
        customer_id="1234567890", email="nobody@example.com"
    )
    assert role is None


async def test_the_email_is_never_interpolated_into_the_query() -> None:
    """Matching happens in Python, so the address cannot shape the query.

    That also sidesteps whether customer_user_access.email_address is a
    filterable field, which is not established in the documentation.
    """
    service = FakeService(rows=[])
    await StubReader(service).access_role(
        customer_id="1234567890", email="weird'quote@example.com"
    )
    _, query = service.queries[0]
    assert "weird" not in query
    assert "WHERE" not in query.upper()


# ---------------------------------------------------------------------------
# the queries themselves
# ---------------------------------------------------------------------------

async def _capture_queries() -> list[str]:
    """Run every read once and collect the GAQL each produced."""
    service = FakeService(rows=[])
    reader = StubReader(service)

    await reader.account_summary("1234567890")
    await reader.managed_accounts(manager_customer_id="9999999999")
    await reader.access_role(customer_id="1234567890", email="a@b.com")
    await reader.campaign_by_id(customer_id="1234567890", campaign_id="55")
    await reader.campaign_performance(
        customer_id="1234567890", start_date="2026-08-01", end_date="2026-08-31", limit=10
    )
    await reader.search_terms(
        customer_id="1234567890", start_date="2026-08-01", end_date="2026-08-31", limit=10
    )
    await reader.search_terms(
        customer_id="1234567890",
        start_date="2026-08-01",
        end_date="2026-08-31",
        limit=10,
        campaign_id="777",
    )
    await reader.find_geo_targets(customer_id="1234567890", query="Delhi")
    await reader.find_geo_targets(
        customer_id="1234567890", query="Delhi", country_code="IN"
    )
    await reader.geo_targets_by_id(
        customer_id="1234567890", geo_target_ids=["2356", "1007751"]
    )
    await reader.campaign_locations(customer_id="1234567890", campaign_id="55")
    await reader.list_keywords(customer_id="1234567890")
    await reader.list_keywords(customer_id="1234567890", ad_group_id="66")
    await reader.keyword_by_id(
        customer_id="1234567890", ad_group_id="66", criterion_id="999"
    )
    await reader.list_ads(customer_id="1234567890")
    await reader.list_ads(customer_id="1234567890", ad_group_id="66")
    await reader.ad_by_id(customer_id="1234567890", ad_group_id="66", ad_id="888")
    return [query for _, query in service.queries]


async def test_reports_aggregate_over_the_range_rather_than_per_day() -> None:
    """segments.date belongs in WHERE only.

    Selecting it as well would silently turn a 30-day report into 30 rows
    per campaign, which reads as "the budget is tiny" to anyone skimming.
    """
    queries = await _capture_queries()
    for query in queries:
        if "segments.date" in query:
            select_clause = query.split(" FROM ")[0]
            assert "segments.date" not in select_clause


async def test_removed_campaigns_are_excluded_from_reports() -> None:
    queries = await _capture_queries()
    # The performance report, not the single-campaign lookup - both select
    # FROM campaign, only one is a report.
    campaign_query = next(
        q for q in queries if " FROM campaign " in q and "segments.date" in q
    )
    assert "campaign.status != 'REMOVED'" in campaign_query


async def test_the_campaign_filter_is_only_added_when_asked_for() -> None:
    queries = await _capture_queries()
    search_term_queries = [q for q in queries if "FROM search_term_view" in q]
    assert len(search_term_queries) == 2
    assert "campaign.id" not in search_term_queries[0].split(" WHERE ")[1]
    assert "AND campaign.id = 777" in search_term_queries[1]


async def test_every_selected_field_exists_in_the_pinned_api_version() -> None:
    """The regression test for a Google field rename or an API_VERSION bump.

    Reads the real generated protos for the pinned version and checks every
    `resource.field` in every SELECT clause actually exists.
    """
    from gads_write.ads.api_version import API_VERSION

    from google.ads.googleads.v25.common.types import metrics as metrics_mod
    from google.ads.googleads.v25.common.types import segments as segments_mod
    from google.ads.googleads.v25.resources.types import ad_group as ad_group_mod
    from google.ads.googleads.v25.resources.types import campaign as campaign_mod
    from google.ads.googleads.v25.resources.types import (
        campaign_budget as campaign_budget_mod,
    )
    from google.ads.googleads.v25.resources.types import customer as customer_mod
    from google.ads.googleads.v25.resources.types import (
        customer_client as customer_client_mod,
    )
    from google.ads.googleads.v25.resources.types import (
        customer_user_access as cua_mod,
    )
    from google.ads.googleads.v25.resources.types import (
        ad_group_ad as ad_group_ad_mod,
    )
    from google.ads.googleads.v25.resources.types import (
        ad_group_criterion as ad_group_criterion_mod,
    )
    from google.ads.googleads.v25.resources.types import (
        campaign_criterion as campaign_criterion_mod,
    )
    from google.ads.googleads.v25.resources.types import (
        geo_target_constant as geo_mod,
    )
    from google.ads.googleads.v25.resources.types import (
        search_term_view as stv_mod,
    )

    assert API_VERSION == "v25", (
        "API_VERSION moved. Re-point the proto imports in this test at the new "
        "version and re-verify every field before trusting the result."
    )

    known = {
        "metrics": metrics_mod.Metrics,
        "segments": segments_mod.Segments,
        "ad_group": ad_group_mod.AdGroup,
        "ad_group_ad": ad_group_ad_mod.AdGroupAd,
        "ad_group_criterion": ad_group_criterion_mod.AdGroupCriterion,
        "campaign": campaign_mod.Campaign,
        "campaign_budget": campaign_budget_mod.CampaignBudget,
        "campaign_criterion": campaign_criterion_mod.CampaignCriterion,
        "geo_target_constant": geo_mod.GeoTargetConstant,
        "customer": customer_mod.Customer,
        "customer_client": customer_client_mod.CustomerClient,
        "customer_user_access": cua_mod.CustomerUserAccess,
        "search_term_view": stv_mod.SearchTermView,
    }

    def resolve(message: object, segment: str, reference: str) -> object:
        """One path segment against a proto message, or fail the test.

        proto-plus renames fields that collide with Python keywords by
        appending an underscore - `ad.type` in GAQL is `ad.type_` on the
        generated message - so both spellings are accepted. Nothing else is.
        """
        fields = message.meta.fields  # type: ignore[attr-defined]
        for candidate in (segment, f"{segment}_"):
            if candidate in fields:
                return fields[candidate]
        raise AssertionError(
            f"{reference}: {segment!r} does not exist in Google Ads API "
            f"{API_VERSION}. Either the field was renamed or API_VERSION moved."
        )

    checked = 0
    for query in await _capture_queries():
        select_clause = query.split(" FROM ")[0].replace("SELECT ", "")
        for reference in (part.strip() for part in select_clause.split(",")):
            if not reference:
                continue
            resource, *path = reference.split(".")
            assert resource in known, f"{reference}: unknown resource {resource!r}"
            assert path, f"{reference}: no field selected"

            # Walk the WHOLE path, descending into nested messages. The
            # previous version of this test matched `resource.field` with a
            # regex, which quietly mis-read a three-level path like
            # `ad_group_ad.ad.responsive_search_ad.headlines` as two unrelated
            # pairs - so the deepest and most rename-prone fields were the ones
            # it was not checking.
            message: object = known[resource]
            for index, segment in enumerate(path):
                field = resolve(message, segment, reference)
                is_last = index == len(path) - 1
                if not is_last:
                    nested = getattr(field, "message", None)
                    assert nested is not None, (
                        f"{reference}: {segment!r} is not a message, so "
                        f"{path[index + 1]!r} cannot be selected from it."
                    )
                    message = nested
            checked += 1

    # Guard against the loop silently checking nothing.
    assert checked > 40, f"expected to verify many fields, only checked {checked}"


# ---------------------------------------------------------------------------
# managed accounts - the query that replaces allowed_customer_ids
# ---------------------------------------------------------------------------


def _client_row(customer_id="1234567890", currency="INR", tz="Asia/Kolkata", manager=False):
    return SimpleNamespace(
        customer_client=SimpleNamespace(
            id=customer_id,
            descriptive_name="Brand India",
            currency_code=currency,
            time_zone=tz,
            manager=manager,
            test_account=False,
            status=SimpleNamespace(name="ENABLED"),
        )
    )


async def test_managed_accounts_are_queried_against_the_manager_account() -> None:
    """The set is derived from the MCC, so the query must be sent TO the MCC.

    Sending it to a child would return only that child's own subtree, which
    for a leaf account is nothing at all.
    """
    service = FakeService(rows=[_client_row()])
    reader = StubReader(service)

    await reader.managed_accounts(manager_customer_id="9999999999")

    sent_to, query = service.queries[0]
    assert sent_to == "9999999999"
    assert " FROM customer_client" in query


async def test_managed_accounts_map_currency_and_timezone_per_account() -> None:
    service = FakeService(
        rows=[
            _client_row("1234567890", currency="INR", tz="Asia/Kolkata"),
            _client_row("2222222222", currency="USD", tz="America/New_York"),
        ]
    )
    reader = StubReader(service)

    accounts = await reader.managed_accounts(manager_customer_id="9999999999")

    assert [(a.customer_id, a.currency_code, a.time_zone) for a in accounts] == [
        ("1234567890", "INR", "Asia/Kolkata"),
        ("2222222222", "USD", "America/New_York"),
    ]


async def test_managed_accounts_exclude_cancelled_and_closed_accounts() -> None:
    service = FakeService(rows=[_client_row()])
    reader = StubReader(service)

    await reader.managed_accounts(manager_customer_id="9999999999")

    _, query = service.queries[0]
    assert "customer_client.status = 'ENABLED'" in query


async def test_a_hostile_manager_id_never_reaches_the_query() -> None:
    reader = StubReader(FakeService(rows=[]))
    with pytest.raises(AdsReadError):
        await reader.managed_accounts(manager_customer_id="999' OR '1'='1")


async def test_a_failed_manager_listing_raises_rather_than_returning_nothing() -> None:
    service = FakeService(rows=[])
    service.explode = RuntimeError("PERMISSION_DENIED")
    reader = StubReader(service)

    with pytest.raises(AdsReadError):
        await reader.managed_accounts(manager_customer_id="9999999999")


# ---------------------------------------------------------------------------
# reaching an account the caller holds DIRECTLY
# ---------------------------------------------------------------------------


class PermissionDeniedOnce(FakeService):
    """Refuses the manager framing, accepts the direct one.

    Stands in for the real case: somebody granted access to one sub-account
    and nothing on the manager.
    """

    def search(self, *, customer_id: str, query: str):
        from gads_write.ads.client import NO_MANAGER

        if self.managers and self.managers[-1] is not NO_MANAGER:
            raise RuntimeError(
                "PERMISSION_DENIED: User doesn't have permission to access "
                "customer. Note: If you're accessing a client customer, the "
                "manager's customer id must be set in the 'login-customer-id' "
                "header."
            )
        return super().search(customer_id=customer_id, query=query)


async def test_the_manager_framing_is_tried_first() -> None:
    """Most people here hold one access row on the manager and none on the
    accounts beneath it, so that framing is right for them and must not cost
    an extra round trip."""
    service = FakeService(rows=[])
    reader = StubReader(service)

    await reader.account_summary("1234567890")

    assert service.managers == [None], "should succeed through the manager"


async def test_a_direct_grant_falls_back_to_no_manager() -> None:
    """The bug this fixes. A user with a direct grant on one sub-account has
    no standing on the manager, so naming it makes Google refuse a request for
    an account they legitimately hold."""
    from gads_write.ads.client import NO_MANAGER

    service = PermissionDeniedOnce(rows=[])
    reader = StubReader(service)

    await reader.account_summary("1234567890")

    assert service.managers[0] is None          # tried the manager
    assert service.managers[-1] is NO_MANAGER   # then direct


async def test_the_working_framing_is_remembered() -> None:
    """Otherwise every call for such a user costs two round trips, and
    visible_tier loops over every managed account."""
    from gads_write.ads.client import NO_MANAGER

    service = PermissionDeniedOnce(rows=[])
    reader = StubReader(service)

    await reader.account_summary("1234567890")
    service.managers.clear()
    await reader.account_summary("1234567890")

    assert service.managers == [NO_MANAGER], "second call should go direct"


async def test_a_non_permission_failure_is_not_retried() -> None:
    """A bad query or an outage is surfaced immediately. Retrying it without
    the manager header would double the cost of every genuine failure and
    change nothing."""
    service = FakeService(rows=[])
    service.explode = RuntimeError("INVALID_ARGUMENT: bad query")
    reader = StubReader(service)

    with pytest.raises(AdsReadError, match="INVALID_ARGUMENT"):
        await reader.account_summary("1234567890")

    assert len(service.managers) == 1, "should not retry a non-permission error"


async def test_a_permission_failure_on_both_framings_still_raises() -> None:
    """No access either way is a real refusal, not something to paper over."""
    service = FakeService(rows=[])
    service.explode = RuntimeError("PERMISSION_DENIED")
    reader = StubReader(service)

    with pytest.raises(AdsReadError, match="PERMISSION_DENIED"):
        await reader.account_summary("1234567890")

    assert len(service.managers) == 2, "tried both framings before giving up"


# ---------------------------------------------------------------------------
# geo targets - the one place free text reaches a GAQL literal
# ---------------------------------------------------------------------------
# Every other read interpolates ids and dates, which `_literal` restricts to a
# character class no quote can hide in. A place-name search cannot: "New Delhi"
# has a space, so it needs its own, looser literal - and looser is exactly
# where injections live.


@pytest.mark.parametrize(
    "hostile",
    [
        "Delhi' OR '1'='1",
        "Delhi'",
        'Delhi"',
        "Delhi\\",
        # The four characters GAQL treats as LIKE wildcards. Harmless to the
        # query's shape, but they silently change what the pattern MEANS, and
        # the escape scheme for them is not one we have verified.
        "Del%hi",
        "Del_hi",
        "Del[hi",
        "Del]hi",
        "",
        "   ",
    ],
)
async def test_hostile_place_names_never_reach_a_query(hostile: str) -> None:
    from gads_write.ads.reads import _text_literal

    service = FakeService(rows=[])
    with pytest.raises(AdsReadError):
        _text_literal(hostile, field="query")

    with pytest.raises(AdsReadError):
        await StubReader(service).find_geo_targets(
            customer_id="1234567890", query=hostile
        )
    # The point: it failed BEFORE anything was sent.
    assert service.queries == []


@pytest.mark.parametrize(
    "ordinary", ["Delhi", "New Delhi", "St. Louis", "Washington, D.C.", "Sault Ste-Marie"]
)
def test_ordinary_place_names_pass(ordinary: str) -> None:
    from gads_write.ads.reads import _text_literal

    assert _text_literal(ordinary, field="query") == ordinary


async def test_a_name_search_looks_only_at_enabled_targets() -> None:
    """REMOVAL_PLANNED targets still resolve, but Google is retiring them, so
    suggesting one hands somebody a target that stops working."""
    service = FakeService(rows=[])
    await StubReader(service).find_geo_targets(
        customer_id="1234567890", query="Delhi"
    )
    _, query = service.queries[0]
    assert "geo_target_constant.status = 'ENABLED'" in query
    assert "geo_target_constant.name LIKE '%Delhi%'" in query


async def test_the_country_filter_is_only_added_when_asked_for() -> None:
    service = FakeService(rows=[])
    reader = StubReader(service)
    await reader.find_geo_targets(customer_id="1234567890", query="Delhi")
    await reader.find_geo_targets(
        customer_id="1234567890", query="Delhi", country_code="IN"
    )
    # In the WHERE clause, not the SELECT - country_code is selected either way.
    assert "geo_target_constant.country_code = " not in service.queries[0][1]
    assert "geo_target_constant.country_code = 'IN'" in service.queries[1][1]


async def test_a_hostile_country_code_never_reaches_the_query() -> None:
    service = FakeService(rows=[])
    with pytest.raises(AdsReadError):
        await StubReader(service).find_geo_targets(
            customer_id="1234567890", query="Delhi", country_code="IN' OR '1'='1"
        )
    assert service.queries == []


async def test_ids_are_looked_up_by_resource_name() -> None:
    """Resource-name filtering is the one filter on this resource Google's own
    documentation demonstrates, and the ids are digits-only by then, so the
    composed literal cannot carry anything that escapes it."""
    service = FakeService(rows=[])
    await StubReader(service).geo_targets_by_id(
        customer_id="1234567890", geo_target_ids=["2356", "1007751"]
    )
    _, query = service.queries[0]
    assert (
        "geo_target_constant.resource_name IN "
        "('geoTargetConstants/2356', 'geoTargetConstants/1007751')"
    ) in query


@pytest.mark.parametrize("hostile", ["2356'", "abc", "23 56", "geoTargetConstants/1"])
async def test_a_non_numeric_geo_target_id_is_refused(hostile: str) -> None:
    service = FakeService(rows=[])
    with pytest.raises(AdsReadError):
        await StubReader(service).geo_targets_by_id(
            customer_id="1234567890", geo_target_ids=[hostile]
        )
    assert service.queries == []


async def test_no_ids_asks_google_nothing() -> None:
    service = FakeService(rows=[])
    assert await StubReader(service).geo_targets_by_id(
        customer_id="1234567890", geo_target_ids=[]
    ) == ()
    assert service.queries == []


def _geo_row(gid="2356", name="Delhi", canonical="Delhi,India", target_type="Region"):
    return SimpleNamespace(
        geo_target_constant=SimpleNamespace(
            id=gid,
            resource_name=f"geoTargetConstants/{gid}",
            name=name,
            canonical_name=canonical,
            country_code="IN",
            target_type=target_type,
            status=SimpleNamespace(name="ENABLED"),
        )
    )


async def test_matches_come_back_with_what_tells_them_apart() -> None:
    """"Delhi" is a city, a state and a union territory. The canonical name
    and the target type are the only things that distinguish them, so a row
    missing either is useless for choosing."""
    service = FakeService(
        rows=[
            _geo_row("1007751", "Delhi", "Delhi,India", "Region"),
            _geo_row("9040379", "New Delhi", "New Delhi,Delhi,India", "City"),
        ]
    )
    rows = await StubReader(service).find_geo_targets(
        customer_id="1234567890", query="Delhi"
    )
    assert [row.describe() for row in rows] == [
        "Delhi,India (Region)",
        "New Delhi,Delhi,India (City)",
    ]


async def test_campaign_locations_exclude_removed_criteria() -> None:
    service = FakeService(rows=[])
    await StubReader(service).campaign_locations(
        customer_id="1234567890", campaign_id="55"
    )
    _, query = service.queries[0]
    assert "campaign_criterion.type = 'LOCATION'" in query
    assert "campaign_criterion.status != 'REMOVED'" in query
    assert "campaign.id = 55" in query


async def test_an_excluded_location_is_reported_as_excluded() -> None:
    """`negative` means EXCLUDE, not target. Reading it as the other way round
    would make the tool offer to add a place the campaign is blocking."""
    service = FakeService(
        rows=[
            SimpleNamespace(
                campaign_criterion=SimpleNamespace(
                    criterion_id=11,
                    location=SimpleNamespace(
                        geo_target_constant="geoTargetConstants/2356"
                    ),
                    negative=True,
                    status=SimpleNamespace(name="ENABLED"),
                    display_name="India",
                )
            )
        ]
    )
    rows = await StubReader(service).campaign_locations(
        customer_id="1234567890", campaign_id="55"
    )
    assert rows[0].negative is True
    assert rows[0].geo_target_id == "2356"


# ---------------------------------------------------------------------------
# keywords and ads
# ---------------------------------------------------------------------------

async def test_listing_keywords_excludes_negatives() -> None:
    """A negative keyword is also an ad_group_criterion of type KEYWORD.

    Listing the two together for somebody about to pause one would be
    dangerous: pausing a NEGATIVE keyword stops it excluding traffic, which
    INCREASES spend - the opposite of what "pause" suggests.
    """
    service = FakeService(rows=[])
    await StubReader(service).list_keywords(customer_id="1234567890")
    _, query = service.queries[0]

    assert "ad_group_criterion.type = 'KEYWORD'" in query
    assert "ad_group_criterion.negative = FALSE" in query
    assert "ad_group_criterion.status != 'REMOVED'" in query


async def test_a_keyword_lookup_filters_on_BOTH_ids() -> None:
    """Criterion ids are unique within an ad group, not within an account, so
    one id alone would be a silent way to address the wrong keyword."""
    service = FakeService(rows=[])
    await StubReader(service).keyword_by_id(
        customer_id="1234567890", ad_group_id="66", criterion_id="999"
    )
    _, query = service.queries[0]

    assert "ad_group.id = 66" in query
    assert "ad_group_criterion.criterion_id = 999" in query
    # And no status filter: a REMOVED keyword must come back so a write tool
    # can refuse it with a message that says why.
    assert "REMOVED" not in query


async def test_an_ad_lookup_filters_on_both_ids() -> None:
    service = FakeService(rows=[])
    await StubReader(service).ad_by_id(
        customer_id="1234567890", ad_group_id="66", ad_id="888"
    )
    _, query = service.queries[0]
    assert "ad_group.id = 66" in query
    assert "ad_group_ad.ad.id = 888" in query


@pytest.mark.parametrize(
    ("method", "kwargs"),
    [
        ("list_keywords", {"ad_group_id": "66' OR '1'='1"}),
        ("keyword_by_id", {"ad_group_id": "66", "criterion_id": "9' OR '1'='1"}),
        ("keyword_by_id", {"ad_group_id": "6' OR '1'='1", "criterion_id": "999"}),
        ("ad_by_id", {"ad_group_id": "66", "ad_id": "8' OR '1'='1"}),
        ("list_ads", {"ad_group_id": "66; DROP"}),
    ],
)
async def test_hostile_ids_never_reach_a_keyword_or_ad_query(method, kwargs) -> None:
    service = FakeService(rows=[])
    reader = StubReader(service)
    with pytest.raises(AdsReadError):
        await getattr(reader, method)(customer_id="1234567890", **kwargs)
    assert service.queries == []


def _keyword_row(criterion_id=999, text="ivf treatment", match="EXACT",
                 own_bid=0, effective=50_000_000, status="ENABLED"):
    return SimpleNamespace(
        ad_group_criterion=SimpleNamespace(
            criterion_id=criterion_id,
            keyword=SimpleNamespace(text=text, match_type=SimpleNamespace(name=match)),
            status=SimpleNamespace(name=status),
            cpc_bid_micros=own_bid,
            effective_cpc_bid_micros=effective,
        ),
        ad_group=SimpleNamespace(
            id=66, name="Core Terms", status=SimpleNamespace(name="ENABLED")
        ),
        campaign=SimpleNamespace(
            id=55, name="Brand - Exact", status=SimpleNamespace(name="ENABLED"),
            bidding_strategy_type=SimpleNamespace(name="MANUAL_CPC"),
        ),
    )


async def test_a_keyword_row_keeps_its_own_bid_and_its_effective_bid_apart() -> None:
    """They differ whenever the keyword has no bid and inherits the ad group
    default - which is the case the percentage backstop has to measure
    against, or setting a first keyword bid would read as a rise from zero."""
    service = FakeService(rows=[_keyword_row(own_bid=0, effective=50_000_000)])
    rows = await StubReader(service).list_keywords(customer_id="1234567890")

    assert rows[0].cpc_bid_micros == 0
    assert rows[0].effective_cpc_bid_micros == 50_000_000


@pytest.mark.parametrize(
    ("match", "expected"),
    [("EXACT", "[ivf]"), ("PHRASE", '"ivf"'), ("BROAD", "ivf")],
)
async def test_a_keyword_displays_in_the_ui_match_type_form(match, expected) -> None:
    """The form people read every day. Never accepted as INPUT - that is what
    validate_keyword_text refuses, so the brackets cannot become part of the
    keyword itself."""
    service = FakeService(rows=[_keyword_row(text="ivf", match=match)])
    rows = await StubReader(service).list_keywords(customer_id="1234567890")
    assert rows[0].display_text == expected


def _ad_row(ad_id=888, headlines=("Fertility Care", "IVF Experts"),
            status="ENABLED", approval="APPROVED"):
    return SimpleNamespace(
        ad_group_ad=SimpleNamespace(
            ad=SimpleNamespace(
                id=ad_id,
                type_=SimpleNamespace(name="RESPONSIVE_SEARCH_AD"),
                final_urls=["https://indiraivf.com/x"],
                responsive_search_ad=SimpleNamespace(
                    headlines=[SimpleNamespace(text=h) for h in headlines],
                    descriptions=[SimpleNamespace(text="Speak to a specialist.")],
                ),
            ),
            status=SimpleNamespace(name=status),
            policy_summary=SimpleNamespace(
                approval_status=SimpleNamespace(name=approval),
                review_status=SimpleNamespace(name="REVIEWED"),
            ),
        ),
        ad_group=SimpleNamespace(
            id=66, name="Core Terms", status=SimpleNamespace(name="ENABLED")
        ),
        campaign=SimpleNamespace(
            id=55, name="Brand - Exact", status=SimpleNamespace(name="ENABLED")
        ),
    )


async def test_an_ad_is_identified_by_its_first_headline() -> None:
    """An ad has no name, so the first headline is the only human handle there
    is. Without it a preview reads "ad 888 -> PAUSED", which nobody can
    approve."""
    service = FakeService(rows=[_ad_row()])
    rows = await StubReader(service).list_ads(customer_id="1234567890")

    assert rows[0].display_text == "Fertility Care"
    assert rows[0].headlines == ("Fertility Care", "IVF Experts")
    assert rows[0].approval_status == "APPROVED"


async def test_an_ad_with_no_headlines_still_has_a_handle() -> None:
    """Not every ad type is an RSA, and a preview must never be left with
    nothing but an id."""
    service = FakeService(rows=[_ad_row(headlines=())])
    rows = await StubReader(service).list_ads(customer_id="1234567890")
    assert rows[0].display_text == "https://indiraivf.com/x"


async def test_campaign_dates_are_read_from_the_date_time_fields() -> None:
    """v25 has NO campaign.start_date / campaign.end_date. The fields are
    start_date_time / end_date_time, strings in the customer's timezone."""
    service = FakeService(rows=[])
    await StubReader(service).campaign_by_id(
        customer_id="1234567890", campaign_id="55"
    )
    _, query = service.queries[0]

    assert "campaign.start_date_time" in query
    assert "campaign.end_date_time" in query
    assert "campaign.start_date," not in query
    assert "campaign.end_date," not in query
