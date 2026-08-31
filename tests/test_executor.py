"""The single execution path.

These tests build a REAL GoogleAdsClient (offline, with a fake token) and
replace only `mutate_campaigns`. So the proto construction, the enum lookup,
`campaign_path`, `copy_from` and the real `protobuf_helpers.field_mask` are
all the production code path - only the gRPC call is fake.

That matters most for the update mask. A mask is what tells Google which
fields to overwrite, and a mask naming a field that is unset on the message
blanks that field. Asserting on the exact mask is the difference between
"changes the status" and "changes the status and erases the campaign name".
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest
from google.oauth2.credentials import Credentials

import gads_write.ads.client as client_module
from gads_write.ads.api_version import API_VERSION
from gads_write.ads.executor import (
    ALLOWED_MASK_PATHS,
    CAMPAIGN_STATUS_OPERATIONS,
    ExecutorError,
    GoogleAdsExecutor,
    MutationRequest,
)

ACCOUNT = "1234567890"


class RecordingCampaignService:
    """Wraps the real service client; only mutate_campaigns is fake."""

    def __init__(self, real, recorder: dict) -> None:
        self._real = real
        self._recorder = recorder

    def campaign_path(self, customer_id, campaign_id):
        return self._real.campaign_path(customer_id, campaign_id)

    def mutate_campaigns(self, **kwargs):
        self._recorder["call"] = kwargs
        if self._recorder.get("raise"):
            raise self._recorder["raise"]
        names = self._recorder.get(
            "resource_names", [f"customers/{ACCOUNT}/campaigns/55"]
        )
        return SimpleNamespace(
            results=[SimpleNamespace(resource_name=n) for n in names]
        )


@pytest.fixture
def recorder(monkeypatch):
    """Patch build_client so the executor gets a real-but-offline client."""
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
            return RecordingCampaignService(real_get_service(name), box)

        client.get_service = get_service  # type: ignore[method-assign]
        return client

    monkeypatch.setattr(client_module, "build_client", fake_build_client)
    return box


def _executor() -> GoogleAdsExecutor:
    return GoogleAdsExecutor(
        settings=SimpleNamespace(
            developer_token="FAKE", login_customer_id="9999999999"
        ),
        token_provider=lambda: "ya29.fake",
    )


# ---------------------------------------------------------------------------
# the update mask
# ---------------------------------------------------------------------------

async def test_the_update_mask_names_only_the_status(recorder) -> None:
    """The test that stops a status change from erasing a campaign name."""
    await _executor().apply(
        MutationRequest(
            customer_id=ACCOUNT,
            operation="pause_campaign",
            payload={"campaign_id": "55"},
        )
    )

    operation = recorder["call"]["operations"][0]
    paths = set(operation.update_mask.paths)

    assert paths <= ALLOWED_MASK_PATHS
    assert "status" in paths
    # Nothing that could blank real content.
    for forbidden in ("name", "campaign_budget", "advertising_channel_type"):
        assert forbidden not in paths


async def test_the_operation_targets_the_right_campaign(recorder) -> None:
    await _executor().apply(
        MutationRequest(
            customer_id=ACCOUNT,
            operation="pause_campaign",
            payload={"campaign_id": "55"},
        )
    )
    operation = recorder["call"]["operations"][0]
    assert operation.update.resource_name == f"customers/{ACCOUNT}/campaigns/55"
    assert recorder["call"]["customer_id"] == ACCOUNT


@pytest.mark.parametrize(
    ("operation", "expected_status"), sorted(CAMPAIGN_STATUS_OPERATIONS.items())
)
async def test_each_operation_sets_its_own_status(
    recorder, operation, expected_status
) -> None:
    await _executor().apply(
        MutationRequest(
            customer_id=ACCOUNT, operation=operation, payload={"campaign_id": "55"}
        )
    )
    sent = recorder["call"]["operations"][0].update
    assert sent.status.name == expected_status


def test_removal_is_not_an_available_operation() -> None:
    """v1 has no irreversible operations, enforced at the executor too."""
    assert "remove_campaign" not in CAMPAIGN_STATUS_OPERATIONS
    assert all("remove" not in name for name in CAMPAIGN_STATUS_OPERATIONS)
    assert "REMOVED" not in CAMPAIGN_STATUS_OPERATIONS.values()


# ---------------------------------------------------------------------------
# request flags
# ---------------------------------------------------------------------------

async def test_partial_failure_is_off(recorder) -> None:
    """With partial_failure the API returns 200 and buries per-operation
    errors in the body, which is how a "successful" mutation silently does
    nothing. We want an exception instead."""
    await _executor().apply(
        MutationRequest(
            customer_id=ACCOUNT,
            operation="pause_campaign",
            payload={"campaign_id": "55"},
        )
    )
    assert recorder["call"]["partial_failure"] is False


async def test_validate_only_is_passed_through_and_returns_no_resources(
    recorder,
) -> None:
    result = await _executor().apply(
        MutationRequest(
            customer_id=ACCOUNT,
            operation="pause_campaign",
            payload={"campaign_id": "55"},
            validate_only=True,
        )
    )
    assert recorder["call"]["validate_only"] is True
    assert result.success is True
    assert result.resource_names == ()
    assert result.details["validate_only"] is True


# ---------------------------------------------------------------------------
# failing loudly
# ---------------------------------------------------------------------------

async def test_an_unknown_operation_is_refused(recorder) -> None:
    with pytest.raises(ExecutorError) as caught:
        await _executor().apply(
            MutationRequest(
                customer_id=ACCOUNT,
                operation="delete_everything",
                payload={"campaign_id": "55"},
            )
        )
    assert "unknown operation" in str(caught.value)
    assert "call" not in recorder  # nothing was sent


async def test_a_non_numeric_campaign_id_is_refused(recorder) -> None:
    with pytest.raises(ExecutorError):
        await _executor().apply(
            MutationRequest(
                customer_id=ACCOUNT,
                operation="pause_campaign",
                payload={"campaign_id": "55 OR 1=1"},
            )
        )
    assert "call" not in recorder


async def test_success_with_no_resource_names_is_treated_as_failure(
    recorder,
) -> None:
    """"It worked but we cannot say what changed" is not a success."""
    recorder["resource_names"] = []
    with pytest.raises(ExecutorError) as caught:
        await _executor().apply(
            MutationRequest(
                customer_id=ACCOUNT,
                operation="pause_campaign",
                payload={"campaign_id": "55"},
            )
        )
    assert "cannot confirm anything changed" in str(caught.value)


async def test_a_google_ads_exception_becomes_a_readable_executor_error(
    recorder,
) -> None:
    from google.ads.googleads.errors import GoogleAdsException

    failure = SimpleNamespace(
        errors=[SimpleNamespace(error_code="USER_PERMISSION_DENIED", message="nope")]
    )
    exc = GoogleAdsException.__new__(GoogleAdsException)
    exc.failure = failure
    exc.request_id = "req-123"
    recorder["raise"] = exc

    with pytest.raises(ExecutorError) as caught:
        await _executor().apply(
            MutationRequest(
                customer_id=ACCOUNT,
                operation="pause_campaign",
                payload={"campaign_id": "55"},
            )
        )

    message = str(caught.value)
    assert "USER_PERMISSION_DENIED" in message
    assert "nope" in message
    assert "req-123" in message
