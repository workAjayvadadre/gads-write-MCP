"""FastMCP server entry point.

Phase 1 scope: authenticate the caller, resolve their tier, report it back.
There is no Google Ads client in this phase and the `google-ads` library is
not even installed. Nothing here can spend money.

Run locally:   python -m gads_write.server
Run in prod:   see ops/run.sh (PM2 -> this module -> Nginx)
"""

from __future__ import annotations

import inspect
import logging
import sys

from fastmcp import FastMCP
from fastmcp.server.auth.providers.google import GoogleProvider

from .auth.identity import AuthError, current_caller, has_google_access_token
from .auth.roles import RoleTable
from .auth.tiers import Tier
from .settings import ConfigError, Settings, load_settings

logger = logging.getLogger("gads_write")


# ---------------------------------------------------------------------------
# OAuth scopes
# ---------------------------------------------------------------------------
# `adwords` is requested from Phase 1 even though nothing uses it until
# Phase 3. Adding a scope later forces every user to re-consent, so we ask
# once, up front.
REQUIRED_SCOPES = [
    "openid",
    "https://www.googleapis.com/auth/userinfo.email",
    "https://www.googleapis.com/auth/adwords",
]


def _configure_logging(settings: Settings) -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)-8s %(name)s: %(message)s",
        stream=sys.stdout,  # PM2 captures stdout
    )
    # These libraries log request bodies at DEBUG, which can include tokens.
    # Pin them at WARNING so a stray root-level DEBUG cannot leak credentials.
    for noisy in ("httpx", "httpcore", "authlib", "urllib3"):
        logging.getLogger(noisy).setLevel(logging.WARNING)


def _build_auth_provider(settings: Settings) -> GoogleProvider:
    """Construct the Google OAuth provider.

    `jwt_signing_key` is passed only if this installed version of FastMCP
    accepts it. The parameter is what keeps user sessions alive across a
    restart; if it is absent we still start, but we say so loudly, because
    the symptom otherwise is "everyone was randomly logged out after a
    deploy" and that is miserable to diagnose.
    """
    kwargs: dict[str, object] = {
        "client_id": settings.oauth_client_id,
        "client_secret": settings.oauth_client_secret,
        "base_url": settings.base_url,
        "required_scopes": REQUIRED_SCOPES,
        "redirect_path": "/auth/callback",
    }

    accepted = set(inspect.signature(GoogleProvider.__init__).parameters)
    if settings.jwt_signing_key:
        if "jwt_signing_key" in accepted:
            kwargs["jwt_signing_key"] = settings.jwt_signing_key
        else:
            logger.warning(
                "This FastMCP version's GoogleProvider does not accept "
                "jwt_signing_key. Sessions will not survive a restart; users "
                "will have to re-authorise after every deploy."
            )

    return GoogleProvider(**kwargs)  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# Boot. Any failure here kills the process before it can serve a request.
# ---------------------------------------------------------------------------
try:
    SETTINGS: Settings = load_settings()
except ConfigError as exc:
    print(f"\n{exc}\n", file=sys.stderr)
    raise SystemExit(1) from exc

_configure_logging(SETTINGS)
ROLES = RoleTable.load(SETTINGS.roles_path)

mcp = FastMCP(name="gads-write-mcp", auth=_build_auth_provider(SETTINGS))


# ---------------------------------------------------------------------------
# Tools
# ---------------------------------------------------------------------------

@mcp.tool
async def health_check() -> dict:
    """Report who you are, what tier you have, and whether writes are enabled.

    Use this to confirm the server sees you as the right person before
    trying anything else. It performs no Google Ads API calls.
    """
    try:
        caller = current_caller()
    except AuthError as exc:
        # Surfaced as a readable message rather than a stack trace.
        return {
            "ok": False,
            "error": str(exc),
        }

    tier = ROLES.tier_for(caller.email)

    return {
        "ok": True,
        "you": {
            "email": caller.email,
            "name": caller.name,
            "tier": tier.value,
        },
        "server": {
            "name": "gads-write-mcp",
            "phase": "1 - skeleton, no Google Ads API access",
            "environment": SETTINGS.env,
            "base_url": SETTINGS.base_url,
        },
        "safety": {
            # The kill switch. False means every write is refused at the guard,
            # regardless of tier, regardless of policy.
            "write_enabled": SETTINGS.write_enabled,
            "roles_file": str(ROLES.source_path),
            "policy_file": str(SETTINGS.policy_path),
        },
        "credentials": {
            # Boolean only. The token itself never leaves auth/identity.py.
            "google_token_available": has_google_access_token(),
        },
        "note": (
            "Tier 'none' means you are authenticated but not listed in "
            "config/roles.yaml. Ask a lead to add you."
            if tier is Tier.NONE
            else "No write tools exist yet. They arrive in Phase 4."
        ),
    }


def main() -> None:
    logger.info(
        "starting gads-write-mcp env=%s host=%s port=%s base_url=%s write_enabled=%s "
        "known_users=%d",
        SETTINGS.env,
        SETTINGS.host,
        SETTINGS.port,
        SETTINGS.base_url,
        SETTINGS.write_enabled,
        len(ROLES.users),
    )
    if SETTINGS.write_enabled:
        logger.warning("GADS_WRITE_ENABLED is true — this server can mutate accounts.")

    mcp.run(transport="http", host=SETTINGS.host, port=SETTINGS.port)


if __name__ == "__main__":
    main()
