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
        "campaign": campaign_mod.Campaign,
        "campaign_budget": campaign_budget_mod.CampaignBudget,
        "customer": customer_mod.Customer,
        "customer_client": customer_client_mod.CustomerClient,
        "customer_user_access": cua_mod.CustomerUserAccess,
        "search_term_view": stv_mod.SearchTermView,
    }

    checked = 0
    for query in await _capture_queries():
        select_clause = query.split(" FROM ")[0].replace("SELECT ", "")
        for reference in re.findall(r"[a-z_]+\.[a-z_]+", select_clause):
            resource, field_name = reference.split(".", 1)
            assert resource in known, f"{reference}: unknown resource {resource!r}"
            fields = known[resource].meta.fields
            assert field_name in fields, (
                f"{reference} does not exist in Google Ads API {API_VERSION}. "
                "Either the field was renamed or API_VERSION moved."
            )
            checked += 1

    # Guard against the loop silently checking nothing.
    assert checked > 20, f"expected to verify many fields, only checked {checked}"


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
