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

    def _service(self, name: str):  # noqa: D102
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
