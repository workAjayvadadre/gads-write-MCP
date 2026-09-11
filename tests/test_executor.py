"""The single execution path, across every operation it supports.

These build a REAL GoogleAdsClient (offline, fake token) and replace only the
`mutate_*` calls. So proto construction, enum lookup, the resource-path
helpers, `copy_from` and the real `protobuf_helpers.field_mask` are all
production code - only the gRPC call is fake.

The two things most worth asserting here:

  update masks   A mask names the fields to overwrite, and a mask naming a
                 field unset on the message BLANKS it. Each operation must
                 name only its own field.

  negative flags A negative keyword with `negative` unset is a positive
                 targeting criterion - the exact opposite of what was asked
                 for, and expensive.
"""

from __future__ import annotations

import inspect
from types import SimpleNamespace

import pytest
from google.oauth2.credentials import Credentials

import gads_write.ads.client as client_module
from gads_write.ads.api_version import API_VERSION
from gads_write.ads.executor import (
    CAMPAIGN_STATUS_OPERATIONS,
    CREATE_OPERATIONS,
    KNOWN_OPERATIONS,
    UPDATE_MASK_ALLOWLIST,
    ExecutorError,
    GoogleAdsExecutor,
    MutationRequest,
)

ACCOUNT = "1234567890"
BUDGET_RESOURCE = f"customers/{ACCOUNT}/campaignBudgets/777"


class RecordingService:
    """Delegates to the real service client; records every mutate_* call.

    The fake BINDS AGAINST THE REAL METHOD SIGNATURE before recording, which
    is the whole point. It used to be `def _mutate(**kwargs)`, which accepted
    absolutely anything - so the executor passing `partial_failure=` and
    `validate_only=` as keyword arguments looked fine here for the entire life
    of the project, and only failed on the first real mutation ever attempted.
    The generated clients take those as fields on the REQUEST, not as kwargs.

    `Signature.bind` raises TypeError on exactly the arguments the real client
    would reject, so that class of bug now fails the build instead of
    production.
    """

    def __init__(self, real, recorder: dict) -> None:
        self._real = real
        self._recorder = recorder

    def __getattr__(self, name: str):
        if not name.startswith("mutate"):
            return getattr(self._real, name)

        real_method = getattr(self._real, name)

        def _mutate(*args, **kwargs):
            # Raises TypeError for any argument the real client would refuse.
            inspect.signature(real_method).bind(*args, **kwargs)

            self._recorder["method"] = name
            self._recorder.setdefault("methods", []).append(name)
            self._recorder["call"] = kwargs
            # The request is a dict or a proto; normalise so assertions can
            # read fields without caring which.
            payload = kwargs.get("request") or (args[0] if args else {})
            self._recorder["request"] = (
                payload if isinstance(payload, dict) else payload
            )
            if self._recorder.get("raise"):
                raise self._recorder["raise"]
            names = self._recorder.get("resource_names", ["customers/x/things/1"])
            if name == "mutate":
                # The bulk, atomic form. Its response is shaped differently:
                # one entry per operation, each naming which resource it was.
                return SimpleNamespace(
                    mutate_operation_responses=[
                        SimpleNamespace(
                            campaign_budget_result=SimpleNamespace(
                                resource_name="customers/x/campaignBudgets/1"
                            ),
                            campaign_result=SimpleNamespace(resource_name=""),
                        ),
                        SimpleNamespace(
                            campaign_budget_result=SimpleNamespace(resource_name=""),
                            campaign_result=SimpleNamespace(
                                resource_name="customers/x/campaigns/2"
                            ),
                        ),
                    ]
                )
            return SimpleNamespace(
                results=[SimpleNamespace(resource_name=n) for n in names]
            )

        return _mutate


@pytest.fixture
def rec(monkeypatch):
    from google.ads.googleads.client import GoogleAdsClient

    box: dict = {}

    def fake_build_client(*, settings, access_token, login_customer_id=None):
        client = GoogleAdsClient(
            credentials=Credentials(token=access_token),
            developer_token="FAKE",
            login_customer_id="9999999999",
            version=API_VERSION,
            use_proto_plus=True,
        )
        real_get_service = client.get_service

        def get_service(name, *args, **kwargs):
            return RecordingService(real_get_service(name), box)

        client.get_service = get_service  # type: ignore[method-assign]
        return client

    monkeypatch.setattr(client_module, "build_client", fake_build_client)
    return box


def _executor() -> GoogleAdsExecutor:
    return GoogleAdsExecutor(
        settings=SimpleNamespace(developer_token="FAKE", login_customer_id="9999999999"),
        token_provider=lambda: "ya29.fake",
    )


async def _apply(operation: str, payload: dict, *, validate_only: bool = False):
    return await _executor().apply(
        MutationRequest(
            customer_id=ACCOUNT,
            operation=operation,
            payload=payload,
            validate_only=validate_only,
        )
    )


def _sent(rec) -> object:
    return rec["request"]["operations"][0]


# ---------------------------------------------------------------------------
# update masks - one field each, never another operation's
# ---------------------------------------------------------------------------

async def test_campaign_status_mask_names_only_status(rec) -> None:
    await _apply("pause_campaign", {"campaign_id": "55"})
    paths = set(_sent(rec).update_mask.paths)
    assert paths <= UPDATE_MASK_ALLOWLIST["pause_campaign"]
    assert "status" in paths
    for forbidden in ("name", "campaign_budget", "advertising_channel_type"):
        assert forbidden not in paths


async def test_budget_mask_names_only_the_amount(rec) -> None:
    await _apply(
        "update_campaign_budget",
        {"budget_resource_name": BUDGET_RESOURCE, "amount_micros": 20_000_000},
    )
    paths = set(_sent(rec).update_mask.paths)
    assert paths <= UPDATE_MASK_ALLOWLIST["update_campaign_budget"]
    assert "amount_micros" in paths
    # A budget mutation carrying `status` would be cross-contamination.
    assert "status" not in paths
    assert "name" not in paths


async def test_bid_mask_names_only_the_bid(rec) -> None:
    await _apply("update_ad_group_bid", {"ad_group_id": "66", "cpc_bid_micros": 20_000_000})
    paths = set(_sent(rec).update_mask.paths)
    assert paths <= UPDATE_MASK_ALLOWLIST["update_ad_group_bid"]
    assert "cpc_bid_micros" in paths
    assert "status" not in paths
    assert "name" not in paths


def test_every_update_operation_has_a_mask_allowlist() -> None:
    """A new update operation without an allowlist would KeyError in
    _seal_mask rather than silently sending an unchecked mask."""
    assert KNOWN_OPERATIONS == set(UPDATE_MASK_ALLOWLIST) | CREATE_OPERATIONS
    assert not (set(UPDATE_MASK_ALLOWLIST) & CREATE_OPERATIONS)


# ---------------------------------------------------------------------------
# the negative flag
# ---------------------------------------------------------------------------

async def test_campaign_negative_keyword_is_actually_negative(rec) -> None:
    """Unset, this would ADD targeting instead of excluding it."""
    await _apply(
        "add_campaign_negative_keyword",
        {"campaign_id": "55", "keyword_text": "free", "match_type": "PHRASE"},
    )
    criterion = _sent(rec).create
    assert criterion.negative is True
    assert criterion.keyword.text == "free"
    assert criterion.keyword.match_type.name == "PHRASE"
    assert criterion.campaign == f"customers/{ACCOUNT}/campaigns/55"


async def test_ad_group_negative_keyword_is_actually_negative(rec) -> None:
    await _apply(
        "add_ad_group_negative_keyword",
        {"ad_group_id": "66", "keyword_text": "cheap", "match_type": "EXACT"},
    )
    criterion = _sent(rec).create
    assert criterion.negative is True
    assert criterion.ad_group == f"customers/{ACCOUNT}/adGroups/66"


async def test_a_positive_keyword_is_explicitly_not_negative(rec) -> None:
    await _apply(
        "add_keyword",
        {
            "ad_group_id": "66",
            "keyword_text": "ivf treatment",
            "match_type": "EXACT",
            "status": "PAUSED",
        },
    )
    criterion = _sent(rec).create
    assert criterion.negative is False
    assert criterion.status.name == "PAUSED"
    assert criterion.keyword.text == "ivf treatment"


# ---------------------------------------------------------------------------
# responsive search ads
# ---------------------------------------------------------------------------

async def test_rsa_is_built_with_all_its_assets(rec) -> None:
    await _apply(
        "create_responsive_search_ad",
        {
            "ad_group_id": "66",
            "headlines": ["One", "Two", "Three"],
            "descriptions": ["Desc one", "Desc two"],
            "final_urls": ["https://indiraivf.com/x"],
            "path1": "ivf",
            "status": "PAUSED",
        },
    )
    created = _sent(rec).create
    rsa = created.ad.responsive_search_ad
    assert [a.text for a in rsa.headlines] == ["One", "Two", "Three"]
    assert [a.text for a in rsa.descriptions] == ["Desc one", "Desc two"]
    assert rsa.path1 == "ivf"
    assert list(created.ad.final_urls) == ["https://indiraivf.com/x"]
    assert created.status.name == "PAUSED"


async def test_an_rsa_without_urls_is_refused(rec) -> None:
    with pytest.raises(ExecutorError):
        await _apply(
            "create_responsive_search_ad",
            {
                "ad_group_id": "66",
                "headlines": ["One", "Two", "Three"],
                "descriptions": ["A", "B"],
                "final_urls": [],
            },
        )
    assert "call" not in rec


# ---------------------------------------------------------------------------
# flags applied to every mutation
# ---------------------------------------------------------------------------

@pytest.mark.parametrize(
    ("operation", "payload"),
    [
        ("pause_campaign", {"campaign_id": "55"}),
        ("update_campaign_budget", {"budget_resource_name": BUDGET_RESOURCE, "amount_micros": 20_000_000}),
        ("update_ad_group_bid", {"ad_group_id": "66", "cpc_bid_micros": 1_000_000}),
        ("add_campaign_negative_keyword", {"campaign_id": "55", "keyword_text": "x", "match_type": "BROAD"}),
        ("add_ad_group_negative_keyword", {"ad_group_id": "66", "keyword_text": "x", "match_type": "BROAD"}),
        ("add_keyword", {"ad_group_id": "66", "keyword_text": "x", "match_type": "EXACT"}),
        (
            "create_responsive_search_ad",
            {
                "ad_group_id": "66",
                "headlines": ["a", "b", "c"],
                "descriptions": ["d", "e"],
                "final_urls": ["https://indiraivf.com/"],
            },
        ),
    ],
)
async def test_partial_failure_is_off_for_every_operation(rec, operation, payload) -> None:
    await _apply(operation, payload)
    assert rec["request"]["partial_failure"] is False


async def test_validate_only_returns_no_resource_names(rec) -> None:
    result = await _apply("pause_campaign", {"campaign_id": "55"}, validate_only=True)
    assert rec["request"]["validate_only"] is True
    assert result.success is True
    assert result.resource_names == ()
    assert result.details["validate_only"] is True


@pytest.mark.parametrize(
    ("operation", "expected_status"), sorted(CAMPAIGN_STATUS_OPERATIONS.items())
)
async def test_each_status_operation_sets_its_own_status(rec, operation, expected_status) -> None:
    await _apply(operation, {"campaign_id": "55"})
    assert _sent(rec).update.status.name == expected_status


# ---------------------------------------------------------------------------
# failing loudly
# ---------------------------------------------------------------------------

def test_removal_is_not_an_available_operation() -> None:
    assert all("remove" not in name for name in KNOWN_OPERATIONS)
    assert all("delete" not in name for name in KNOWN_OPERATIONS)
    assert "REMOVED" not in CAMPAIGN_STATUS_OPERATIONS.values()


async def test_an_unknown_operation_is_refused(rec) -> None:
    with pytest.raises(ExecutorError) as caught:
        await _apply("remove_campaign", {"campaign_id": "55"})
    assert "unknown operation" in str(caught.value)
    assert "call" not in rec


@pytest.mark.parametrize(
    ("operation", "payload"),
    [
        ("pause_campaign", {"campaign_id": "55 OR 1=1"}),
        ("update_ad_group_bid", {"ad_group_id": "66", "cpc_bid_micros": 0}),
        ("update_ad_group_bid", {"ad_group_id": "66", "cpc_bid_micros": -5}),
        ("update_campaign_budget", {"budget_resource_name": "nonsense", "amount_micros": 1}),
        ("add_keyword", {"ad_group_id": "66", "keyword_text": "", "match_type": "EXACT"}),
        ("add_keyword", {"ad_group_id": "66", "keyword_text": "x", "match_type": "SORTOF"}),
        ("add_keyword", {"ad_group_id": "66", "keyword_text": "x", "match_type": "EXACT", "status": "REMOVED"}),
    ],
)
async def test_malformed_payloads_are_refused_before_sending(rec, operation, payload) -> None:
    with pytest.raises(ExecutorError):
        await _apply(operation, payload)
    assert "call" not in rec


async def test_success_with_no_resource_names_is_treated_as_failure(rec) -> None:
    rec["resource_names"] = []
    with pytest.raises(ExecutorError) as caught:
        await _apply("pause_campaign", {"campaign_id": "55"})
    assert "cannot confirm anything changed" in str(caught.value)


async def test_a_google_ads_exception_becomes_a_readable_error(rec) -> None:
    from google.ads.googleads.errors import GoogleAdsException

    exc = GoogleAdsException.__new__(GoogleAdsException)
    exc.failure = SimpleNamespace(
        errors=[SimpleNamespace(error_code="USER_PERMISSION_DENIED", message="nope")]
    )
    exc.request_id = "req-123"
    rec["raise"] = exc

    with pytest.raises(ExecutorError) as caught:
        await _apply("pause_campaign", {"campaign_id": "55"})

    message = str(caught.value)
    assert "USER_PERMISSION_DENIED" in message
    assert "nope" in message
    assert "req-123" in message


# ---------------------------------------------------------------------------
# create_campaign - the only operation that makes TWO mutations
# ---------------------------------------------------------------------------


async def test_the_budget_and_campaign_go_in_ONE_request(rec) -> None:
    """The fix for a real failure in production.

    This was two calls: create the budget, then the campaign. When the second
    failed - and it did, on the first live attempt, with `field_error:
    REQUIRED` - the first had already committed, leaving an orphaned budget in
    the account. Reporting the orphan was not good enough; a write server must
    not be able to leave debris.

    `GoogleAdsService.mutate` applies its operations as one transaction when
    partial_failure is off. Either both resources exist or neither does.
    """
    await _apply(
        "create_campaign",
        {"name": "ZZ Test", "budget_micros": 100_000_000,
         "bidding_strategy": "MANUAL_CPC"},
    )
    assert rec["methods"] == ["mutate"], (
        "campaign creation must be a single atomic mutate, not one call per "
        f"resource - got {rec['methods']}"
    )


async def test_a_failure_leaves_nothing_behind(rec) -> None:
    """The atomicity regression test.

    A failing mutate must leave NO objects. Because both operations travel in
    one request, there is no window in which the budget exists and the
    campaign does not - so a failure cannot orphan anything, and there is
    nothing for the error message to have to name.
    """
    real_send = GoogleAdsExecutor._send

    def failing_send(call, request, **kwargs):
        raise ExecutorError("field_error: REQUIRED")

    GoogleAdsExecutor._send = staticmethod(failing_send)
    try:
        with pytest.raises(ExecutorError) as caught:
            await _apply(
                "create_campaign",
                {"name": "ZZ Test", "budget_micros": 100_000_000,
                 "bidding_strategy": "MANUAL_CPC"},
            )
    finally:
        GoogleAdsExecutor._send = staticmethod(real_send)

    # Nothing was sent piecemeal, so nothing can be stranded.
    assert rec.get("methods") is None or rec["methods"] == []
    # And the message says nothing about leftovers, because there are none.
    message = str(caught.value)
    assert "orphan" not in message.lower()
    assert "already created" not in message.lower()


async def test_the_budget_operation_comes_first_in_the_request(rec) -> None:
    """Order still matters inside the single request: the campaign references
    the budget by a temporary resource name, and Google resolves operations in
    the order given."""
    await _apply(
        "create_campaign",
        {"name": "ZZ Test", "budget_micros": 100_000_000,
         "bidding_strategy": "MANUAL_CPC"},
    )
    ops = rec["request"]["mutate_operations"]
    assert ops[0].campaign_budget_operation.create.name == "ZZ Test - budget"
    assert ops[1].campaign_operation.create.name == "ZZ Test"


async def test_the_campaign_points_at_the_budget_created_alongside_it(rec) -> None:
    """A temporary negative id, resolved by Google within the request. There
    is no real id to reference until the request has run, which is what makes
    the two operations expressible together at all."""
    await _apply(
        "create_campaign",
        {"name": "ZZ Test", "budget_micros": 100_000_000,
         "bidding_strategy": "MANUAL_CPC"},
    )
    ops = rec["request"]["mutate_operations"]
    budget_name = ops[0].campaign_budget_operation.create.resource_name
    assert ops[1].campaign_operation.create.campaign_budget == budget_name
    assert budget_name.endswith("/-1")


async def test_a_created_campaign_is_paused_and_search_only(rec) -> None:
    """It cannot spend: paused, no ad groups, and search partners, Display and
    YouTube all off. A preview omitting those would be lying."""
    await _apply(
        "create_campaign",
        {"name": "ZZ Test", "budget_micros": 100_000_000,
         "bidding_strategy": "MANUAL_CPC"},
    )
    campaign = rec["request"]["mutate_operations"][1].campaign_operation.create

    assert campaign.status.name == "PAUSED"
    assert campaign.advertising_channel_type.name == "SEARCH"
    assert campaign.network_settings.target_google_search is True
    assert campaign.network_settings.target_search_network is False
    assert campaign.network_settings.target_content_network is False
    assert campaign.network_settings.target_partner_search_network is False


async def test_the_eu_political_declaration_is_set(rec) -> None:
    """Mandatory on create, and the reason the first live attempt came back
    `field_error: REQUIRED`. Left unset it defaults to UNSPECIFIED, which
    Google rejects."""
    await _apply(
        "create_campaign",
        {"name": "ZZ Test", "budget_micros": 100_000_000,
         "bidding_strategy": "MANUAL_CPC"},
    )
    campaign = rec["request"]["mutate_operations"][1].campaign_operation.create
    assert (
        campaign.contains_eu_political_advertising.name
        == "DOES_NOT_CONTAIN_EU_POLITICAL_ADVERTISING"
    )


async def test_the_budget_it_creates_is_never_shared(rec) -> None:
    """A shared budget cannot be edited through this server at all, so
    creating one would produce a campaign whose budget we then refuse to
    touch."""
    await _apply(
        "create_campaign",
        {"name": "ZZ Test", "budget_micros": 100_000_000,
         "bidding_strategy": "MAXIMIZE_CLICKS"},
    )
    budget = rec["request"]["mutate_operations"][0].campaign_budget_operation.create
    assert budget.explicitly_shared is False
    assert budget.amount_micros == 100_000_000


@pytest.mark.parametrize("strategy", ["MANUAL_CPC", "MAXIMIZE_CLICKS"])
async def test_both_supported_strategies_are_accepted(rec, strategy: str) -> None:
    await _apply(
        "create_campaign",
        {"name": "ZZ Test", "budget_micros": 100_000_000,
         "bidding_strategy": strategy},
    )
    campaign = rec["request"]["mutate_operations"][1].campaign_operation.create
    assert campaign.name == "ZZ Test"


async def test_an_unsupported_strategy_is_refused(rec) -> None:
    """Target CPA and Target ROAS each need their own target figure - another
    question to ask and another way to get it wrong."""
    with pytest.raises(ExecutorError, match="bidding strategy"):
        await _apply(
            "create_campaign",
            {"name": "ZZ Test", "budget_micros": 100_000_000,
             "bidding_strategy": "TARGET_ROAS"},
        )


async def test_a_campaign_with_no_budget_is_refused(rec) -> None:
    with pytest.raises(ExecutorError, match="budget"):
        await _apply(
            "create_campaign",
            {"name": "ZZ Test", "budget_micros": 0,
             "bidding_strategy": "MANUAL_CPC"},
        )


# ---------------------------------------------------------------------------
# reaching an account the caller holds DIRECTLY
# ---------------------------------------------------------------------------


async def test_a_permission_refusal_is_retried_without_the_manager(rec) -> None:
    """Somebody granted access to one sub-account and nothing on the manager
    has no standing on it, so naming the manager makes Google refuse a change
    to an account they legitimately hold.

    Safe to retry because the refusal is an AUTHORISATION one: Google rejected
    the request before any operation ran, and partial_failure is false, so
    nothing was applied and there is nothing to double.
    """
    calls = {"n": 0}
    real_send = GoogleAdsExecutor._send

    def failing_first(call, request, **kwargs):
        calls["n"] += 1
        if calls["n"] == 1:
            raise ExecutorError("PERMISSION_DENIED: User doesn't have permission")
        return real_send(call, request, **kwargs)

    GoogleAdsExecutor._send = staticmethod(failing_first)
    try:
        result = await _apply(
            "pause_campaign", {"campaign_id": "55", "status": "PAUSED"}
        )
    finally:
        GoogleAdsExecutor._send = staticmethod(real_send)

    assert result.success
    assert calls["n"] == 2, "should have retried once, without the manager"


async def test_a_non_permission_failure_is_never_retried(rec) -> None:
    """The rule that makes retrying a mutation defensible at all. Anything
    that is not an authorisation refusal might have landed, and "we do not
    know whether it landed" must stay a reason to stop, not to try again."""
    calls = {"n": 0}
    real_send = GoogleAdsExecutor._send

    def always_failing(call, request, **kwargs):
        calls["n"] += 1
        raise ExecutorError("INTERNAL: connection reset")

    GoogleAdsExecutor._send = staticmethod(always_failing)
    try:
        with pytest.raises(ExecutorError, match="connection reset"):
            await _apply("pause_campaign", {"campaign_id": "55", "status": "PAUSED"})
    finally:
        GoogleAdsExecutor._send = staticmethod(real_send)

    assert calls["n"] == 1, "an ambiguous failure must not be retried"
