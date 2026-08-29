"""FastMCP server entry point and dependency wiring.

Everything the server needs is constructed once, here, at import time, and
passed explicitly to whatever needs it. Think of this as the composition
root: no module reaches for a global, so tests build their own Guard with
fakes and never touch this file.

Phase 2 scope: authenticate, resolve a tier, filter the tool list, and gate
every call. There is still no Google Ads client and the `google-ads` library
is not installed, so nothing here can spend money.

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
from .auth.roles import FileTierResolver, RoleStore
from .auth.tiers import Tier, TierResolver
from .mcp_middleware import TierMiddleware
from .safety.audit import AuditLog
from .safety.guards import Guard
from .safety.plans import PlanStore
from .safety.policy import PolicyStore
from .safety.spend import DailySpendLedger
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
    accepts it. That parameter is what keeps user sessions alive across a
    restart; if it is absent we still start, but we say so loudly, because
    the symptom otherwise is "everyone was randomly logged out after a
    deploy", which is miserable to diagnose.
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


def _build_tier_resolver(role_store: RoleStore) -> TierResolver:
    """Pick the tier backing named by roles.yaml `mode`.

    `google_ads` is refused rather than silently falling back to the file.
    Flipping that switch before the resolver exists would otherwise look like
    it worked while everyone quietly kept their file-assigned tiers.
    """
    mode = role_store.current().mode
    if mode == "file":
        return FileTierResolver(role_store)
    raise ConfigError(
        f"roles.yaml sets mode: {mode!r}, but the Google Ads tier resolver "
        "does not exist yet (Phase 3). Set mode: file until it does."
    )


# ---------------------------------------------------------------------------
# Composition root. Any failure here kills the process before it serves.
# ---------------------------------------------------------------------------
try:
    SETTINGS: Settings = load_settings()
    _configure_logging(SETTINGS)

    ROLE_STORE = RoleStore(SETTINGS.roles_path)
    TIER_RESOLVER: TierResolver = _build_tier_resolver(ROLE_STORE)
    POLICY_STORE = PolicyStore(SETTINGS.policy_path)
    AUDIT_LOG = AuditLog(SETTINGS.audit_log_path)
    SPEND_LEDGER = DailySpendLedger(AUDIT_LOG)
    PLAN_STORE = PlanStore()

    GUARD = Guard(
        settings=SETTINGS,
        policy_store=POLICY_STORE,
        tier_resolver=TIER_RESOLVER,
        audit_log=AUDIT_LOG,
        spend_ledger=SPEND_LEDGER,
    )
except ConfigError as exc:
    print(f"\n{exc}\n", file=sys.stderr)
    raise SystemExit(1) from exc


mcp = FastMCP(name="gads-write-mcp", auth=_build_auth_provider(SETTINGS))

# Registered before any tool so it wraps every one of them.
mcp.add_middleware(
    TierMiddleware(tier_resolver=TIER_RESOLVER, settings=SETTINGS)
)


# ---------------------------------------------------------------------------
# Tools
# ---------------------------------------------------------------------------

@mcp.tool
async def health_check() -> dict:
    """Report who you are, what you may do, and whether writes are enabled.

    Use this to confirm the server sees you as the right person before
    trying anything else. It performs no Google Ads API calls.
    """
    try:
        caller = current_caller()
    except AuthError as exc:
        return {"ok": False, "error": str(exc)}

    try:
        tier = await TIER_RESOLVER.visible_tier(caller)
        tier_error = None
    except Exception as exc:  # noqa: BLE001 - reported, never raised to the model
        tier = Tier.NONE
        tier_error = str(exc)

    policy = POLICY_STORE.current()

    return {
        "ok": True,
        "you": {
            "email": caller.email,
            "name": caller.name,
            "tier": tier.value,
            "tier_source": TIER_RESOLVER.source,
            "tier_error": tier_error,
        },
        "server": {
            "name": "gads-write-mcp",
            "phase": "2 - safety core wired, no Google Ads access yet",
            "environment": SETTINGS.env,
            "base_url": SETTINGS.base_url,
        },
        "safety": {
            # The kill switch. False means every write is refused at the
            # gate, regardless of tier, regardless of policy.
            "write_enabled": SETTINGS.write_enabled,
            "managed_accounts": len(policy.allowed_customer_ids),
            "open_plans": PLAN_STORE.open_count(),
            # Surfaced so a bad config edit is visible here rather than only
            # in the logs. Both should be null.
            "policy_reload_error": POLICY_STORE.last_error,
            "roles_reload_error": ROLE_STORE.last_error,
        },
        "credentials": {
            # Boolean only. The token itself never leaves auth/identity.py.
            "google_token_available": has_google_access_token(),
        },
        "note": (
            "Tier 'none' means you are authenticated but have no access. "
            "Ask a lead to add you."
            if tier is Tier.NONE
            else "No write tools exist yet. They arrive in Phase 4."
        ),
    }


def main() -> None:
    logger.info(
        "starting gads-write-mcp env=%s host=%s port=%s base_url=%s "
        "write_enabled=%s tiers=%s accounts=%d",
        SETTINGS.env,
        SETTINGS.host,
        SETTINGS.port,
        SETTINGS.base_url,
        SETTINGS.write_enabled,
        TIER_RESOLVER.source,
        len(POLICY_STORE.current().allowed_customer_ids),
    )
    if SETTINGS.write_enabled:
        logger.warning("GADS_WRITE_ENABLED is true - this server can mutate accounts.")

    mcp.run(transport="http", host=SETTINGS.host, port=SETTINGS.port)


if __name__ == "__main__":
    main()
