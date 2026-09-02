"""The unauthenticated liveness endpoint.

Everything else on this server requires OAuth, which means the only way to
notice a problem was for a person to complain. The sharp case is not a crash
- PM2 notices those. It is the server running perfectly on a config it
refused to reload: the team lowers a limit, the edit is invalid, PolicyStore
correctly keeps the last good version, and nobody ever finds out the new
limit is not in force.
"""

from __future__ import annotations

import json
from pathlib import Path

import httpx
import pytest
from fastmcp import FastMCP

from gads_write.auth.roles import RoleStore
from gads_write.health import register_health_route
from gads_write.safety.policy import PolicyStore
from gads_write.settings import Settings


def _settings(tmp_path: Path, policy_path: Path, roles_path: Path) -> Settings:
    return Settings(
        env="test", host="127.0.0.1", port=8081, base_url="https://example.com",
        oauth_client_id="x.apps.googleusercontent.com", oauth_client_secret="s",
        jwt_signing_key="k", developer_token="d", login_customer_id="9999999999",
        write_enabled=False,
        policy_path=policy_path,
        roles_path=roles_path,
        audit_log_path=tmp_path / "audit.jsonl",
    )


def _app(tmp_path, write_policy, write_roles, *, break_policy: bool = False):
    policy_path = write_policy()
    roles_path = write_roles()
    policy_store = PolicyStore(policy_path)
    role_store = RoleStore(roles_path)

    if break_policy:
        # A bad edit. PolicyStore keeps the last good version by design, so
        # the server keeps serving - silently, until something says so.
        policy_path.write_text("version: 2\nallowed:\n  - [\n", encoding="utf-8")
        policy_store.current()

    mcp = FastMCP(name="test")
    register_health_route(
        mcp,
        settings=_settings(tmp_path, policy_path, roles_path),
        policy_store=policy_store,
        role_store=role_store,
    )
    return mcp


async def _get(mcp: FastMCP, path: str) -> httpx.Response:
    app = mcp.http_app()
    async with app.router.lifespan_context(app):
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(
            transport=transport, base_url="http://testserver"
        ) as client:
            return await client.get(path)


async def test_healthz_answers_without_authentication(
    tmp_path, write_policy, write_roles
) -> None:
    """A monitor must be able to call this. Everything else needs OAuth."""
    response = await _get(_app(tmp_path, write_policy, write_roles), "/healthz")

    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "ok"
    assert body["write_enabled"] is False


async def test_healthz_reports_a_refused_config_reload(
    tmp_path, write_policy, write_roles
) -> None:
    """The failure this endpoint exists for.

    The server is alive and serving correctly on its last good policy. Only
    this says the newer edit was rejected, so a monitor can page someone
    instead of the team believing a limit is in force that is not.
    """
    mcp = _app(tmp_path, write_policy, write_roles, break_policy=True)
    response = await _get(mcp, "/healthz")

    assert response.status_code == 503
    body = response.json()
    assert body["status"] == "degraded"
    assert body["policy_reload_error"]


async def test_healthz_leaks_nothing_sensitive(
    tmp_path, write_policy, write_roles
) -> None:
    """It is unauthenticated, so it must say nothing an attacker could use."""
    response = await _get(_app(tmp_path, write_policy, write_roles), "/healthz")
    raw = json.dumps(response.json()).lower()

    for forbidden in ("token", "secret", "client_id", "9999999999", "customer"):
        assert forbidden not in raw, f"/healthz exposed {forbidden!r}"
