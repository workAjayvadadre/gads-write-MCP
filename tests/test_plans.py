"""Plan store: owner-bound, time-limited, single-use.

These four properties are the difference between "the model proposed a
change and it happened" and "a person approved a specific change".
"""

from __future__ import annotations

import pytest

from gads_write.safety.plans import (
    PlanAlreadyUsed,
    PlanExpired,
    PlanNotFound,
    PlanNotOwned,
    PlanStore,
    TooManyOpenPlans,
)

OWNER = "lead@example.com"
OTHER = "operator@example.com"


def _draft(store: PlanStore, *, owner: str = OWNER, ttl: int = 600):
    return store.draft(
        owner_email=owner,
        tool="update_campaign_budget",
        customer_id="1234567890",
        arguments={"campaign_id": "111", "new_daily_budget": 1200},
        preview="Raise Brand Search daily budget from INR 1,000.00 to INR 1,200.00",
        ttl_seconds=ttl,
        spend_delta_units="200",
    )


def test_draft_returns_a_preview_and_changes_nothing(clock) -> None:
    store = PlanStore(clock=clock)
    plan = _draft(store)
    summary = plan.summary()
    assert summary["plan_id"] == plan.plan_id
    assert "1,200.00" in summary["preview"]
    assert "Nothing has changed yet" in summary["next_step"]


def test_owner_can_confirm(clock) -> None:
    store = PlanStore(clock=clock)
    plan = _draft(store)
    consumed = store.consume(plan.plan_id, caller_email=OWNER)
    assert consumed.plan_id == plan.plan_id


def test_a_different_person_cannot_confirm_your_plan(clock) -> None:
    store = PlanStore(clock=clock)
    plan = _draft(store)
    with pytest.raises(PlanNotOwned, match="drafted by someone else"):
        store.consume(plan.plan_id, caller_email=OTHER)
    # and it is still available to its actual owner
    assert store.consume(plan.plan_id, caller_email=OWNER)


def test_owner_matching_ignores_case_and_whitespace(clock) -> None:
    store = PlanStore(clock=clock)
    plan = _draft(store, owner="Lead@Example.com")
    assert store.consume(plan.plan_id, caller_email="  LEAD@EXAMPLE.COM ")


def test_expired_plan_is_rejected(clock) -> None:
    store = PlanStore(clock=clock)
    plan = _draft(store, ttl=600)

    clock.advance(599)
    assert store.peek(plan.plan_id, caller_email=OWNER)

    clock.advance(2)  # now 601s old
    with pytest.raises(PlanExpired, match="expired"):
        store.consume(plan.plan_id, caller_email=OWNER)


def test_expiry_boundary_is_exact(clock) -> None:
    store = PlanStore(clock=clock)
    plan = _draft(store, ttl=600)
    clock.advance(600)  # exactly at the TTL
    with pytest.raises(PlanExpired):
        store.consume(plan.plan_id, caller_email=OWNER)


def test_a_plan_cannot_be_replayed(clock) -> None:
    store = PlanStore(clock=clock)
    plan = _draft(store)
    store.consume(plan.plan_id, caller_email=OWNER)

    with pytest.raises(PlanAlreadyUsed, match="already applied"):
        store.consume(plan.plan_id, caller_email=OWNER)


def test_consumption_happens_before_execution(clock) -> None:
    """A crash during apply must not leave the plan replayable.

    consume() marks the plan used and returns it; the caller executes
    afterwards. So even if the caller then explodes, a second attempt is
    refused and a human has to look at the account.
    """
    store = PlanStore(clock=clock)
    plan = _draft(store)

    consumed = store.consume(plan.plan_id, caller_email=OWNER)
    assert consumed.is_consumed

    with pytest.raises(PlanAlreadyUsed):
        store.consume(plan.plan_id, caller_email=OWNER)


def test_unknown_plan_id(clock) -> None:
    store = PlanStore(clock=clock)
    with pytest.raises(PlanNotFound):
        store.consume("nope", caller_email=OWNER)


def test_ownership_is_checked_before_state(clock) -> None:
    # Someone else's plan_id reveals nothing about whether it expired or
    # was used - the ownership error comes first either way.
    store = PlanStore(clock=clock)
    plan = _draft(store, ttl=10)
    clock.advance(100)
    with pytest.raises(PlanNotOwned):
        store.consume(plan.plan_id, caller_email=OTHER)


def test_plan_ids_are_unguessable(clock) -> None:
    store = PlanStore(clock=clock)
    ids = {_draft(store).plan_id for _ in range(50)}
    assert len(ids) == 50
    # 128 bits of entropy, urlsafe-base64 encoded
    assert all(len(plan_id) >= 20 for plan_id in ids)


def test_expired_plans_are_purged_so_memory_stays_bounded(clock) -> None:
    store = PlanStore(clock=clock)
    for _ in range(5):
        _draft(store, ttl=10)
    assert store.open_count() == 5

    clock.advance(60)
    assert store.open_count() == 0
    _draft(store)  # drafting triggers a purge
    assert len(store._plans) == 1  # noqa: SLF001 - asserting the purge happened


def test_open_plan_limit_refuses_rather_than_growing(clock) -> None:
    store = PlanStore(clock=clock, max_open_plans=3)
    for _ in range(3):
        _draft(store)
    with pytest.raises(TooManyOpenPlans):
        _draft(store)
