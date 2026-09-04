"""The unauthenticated `/healthz` endpoint.

The failure it exists to catch is the quiet one: a refused config reload. The
server keeps serving perfectly on its last good version while the team
believes their edit is in force. Nothing errors and nobody finds out.

roles.yaml is now the only file that can fail this way. The spending rules
used to live in policy.yaml and were the original reason for this endpoint;
they are relative, fixed in code, or read from the environment now, so there
is no policy edit left to refuse.
"""

from __future__ import annotations

import json
from pathlib import Path

import httpx
from fastmcp import FastMCP

from gads_write.auth.roles import RoleStore
from gads_write.health import register_health_route
from gads_write.settings import Settings


def _settings(tmp_path: Path, roles_path: Path) -> Settings:
    return Settings(
        env="test", host="127.0.0.1", port=8081, base_url="https://example.com",
        oauth_client_id="x.apps.googleusercontent.com", oauth_client_secret="s",
        jwt_signing_key="k", developer_token="d", login_customer_id="9999999999",
        write_enabled=False,
        roles_path=roles_path,
        audit_log_path=tmp_path / "audit.jsonl",
    )


def _app(tmp_path, write_roles, *, break_roles: bool = False):
    roles_path = write_roles()
    role_store = RoleStore(roles_path)

    if break_roles:
        # A bad edit. RoleStore keeps the last good version by design, so the
        # server keeps serving - silently, until something says so.
        roles_path.write_text("mode: [\n", encoding="utf-8")
        role_store.current()

    mcp = FastMCP(name="test")
    register_health_route(
        mcp,
        settings=_settings(tmp_path, roles_path),
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


async def test_healthz_answers_without_authentication(tmp_path, write_roles) -> None:
    """A monitor must be able to call this. Everything else needs OAuth."""
    response = await _get(_app(tmp_path, write_roles), "/healthz")

    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "ok"
    assert body["write_enabled"] is False


async def test_healthz_reports_a_refused_config_reload(tmp_path, write_roles) -> None:
    """The failure this endpoint exists for.

    The server is alive and serving correctly on its last good roles table.
    Only this says the newer edit was rejected, so a monitor can page someone
    instead of the team believing a role change took effect that did not.
    """
    response = await _get(_app(tmp_path, write_roles, break_roles=True), "/healthz")

    assert response.status_code == 503
    body = response.json()
    assert body["status"] == "degraded"
    assert body["roles_reload_error"]


async def test_healthz_reports_a_structurally_valid_but_wrong_edit(
    tmp_path, write_roles
) -> None:
    """The quietest version of the failure.

    Valid YAML, so the edit looks fine to whoever saved it, but the tier
    named is not one this code understands. Alive-but-broken must read as
    degraded rather than going on answering 200 ok.
    """
    roles_path = write_roles()
    role_store = RoleStore(roles_path)
    assert role_store.current() is not None

    roles_path.write_text('users:\n  "a@b.com": superuser\n', encoding="utf-8")
    role_store.current()

    mcp = FastMCP(name="test")
    register_health_route(
        mcp, settings=_settings(tmp_path, roles_path), role_store=role_store
    )
    response = await _get(mcp, "/healthz")

    assert response.status_code == 503
    assert response.json()["status"] == "degraded"


async def test_healthz_leaks_nothing_sensitive(tmp_path, write_roles) -> None:
    """It is unauthenticated, so it must say nothing an attacker could use."""
    response = await _get(_app(tmp_path, write_roles), "/healthz")
    raw = json.dumps(response.json()).lower()

    for forbidden in ("token", "secret", "client_id", "9999999999", "customer"):
        assert forbidden not in raw, f"/healthz exposed {forbidden!r}"


async def test_a_degraded_response_still_leaks_nothing(tmp_path, write_roles) -> None:
    """The degraded path carries an error message from a file that holds
    email addresses, so it is the one most likely to say too much."""
    response = await _get(_app(tmp_path, write_roles, break_roles=True), "/healthz")
    raw = json.dumps(response.json()).lower()

    for forbidden in ("token", "secret", "client_id", "@", "9999999999"):
        assert forbidden not in raw, f"/healthz exposed {forbidden!r}"
