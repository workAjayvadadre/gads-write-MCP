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

from .ads.api_version import API_VERSION
from .ads.executor import GoogleAdsExecutor
from .ads.reads import GoogleAdsReader
from .auth.google_ads_roles import (
    GoogleAdsTierResolver,
    OverridingTierResolver,
    TierCache,
)
from .auth.identity import (
    AuthError,
    current_caller,
    google_access_token,
    has_google_access_token,
)
from .auth.roles import RoleStore
from .auth.tiers import Tier, TierResolver
from .health import register_health_route
from .mcp_middleware import TierMiddleware
from .safety.accounts import AccountLookupError, ManagedAccountStore
from .safety.audit import AuditLog
from .safety.guards import Guard
from .safety.plans import PlanStore
from .safety.policy import PolicyStore
from .safety.spend import DailySpendLedger
from .settings import ConfigError, Settings, load_settings
from .tools.confirm import register_confirm_tool
from .tools.reads import register_read_tools
from .tools.writes import register_write_tools

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


def _build_tier_resolver(
    *,
    settings: Settings,
    role_store: RoleStore,
    managed_accounts: ManagedAccountStore,
    reader: GoogleAdsReader,
) -> TierResolver:
    """Tiers from the caller's own Google Ads role, with break-glass on top.

    There is no `mode` any more. Tiers ALWAYS come from Google Ads - that is
    the operational requirement the whole design exists for: a lead adds
    someone in the Google Ads UI and they work here, with no developer and no
    file to edit.

    `OverridingTierResolver` wraps it with the break-glass file, which is
    normally absent. See auth/roles.py for why that stayed a file.

    The managed set is passed as a callable rather than a value so that
    linking a new account in Google Ads changes which accounts count towards
    a user's visible tier without a restart.
    """

    async def managed_customer_ids() -> frozenset[str]:
        return frozenset(
            account.customer_id for account in await managed_accounts.all()
        )

    primary = GoogleAdsTierResolver(
        reader=reader,
        login_customer_id=settings.login_customer_id,
        managed_customer_ids=managed_customer_ids,
        cache=TierCache(settings.tier_cache_seconds),
    )
    return OverridingTierResolver(overrides=role_store, primary=primary)


# ---------------------------------------------------------------------------
# Composition root. Any failure here kills the process before it serves.
# ---------------------------------------------------------------------------
try:
    SETTINGS: Settings = load_settings()
    _configure_logging(SETTINGS)

    ROLE_STORE = RoleStore(SETTINGS.roles_path)
    POLICY_STORE = PolicyStore(SETTINGS)

    # Holds no credential. `google_access_token` is called per request and
    # reads the current caller's token from the request context, so one
    # long-lived reader never carries one user's credential into another
    # user's request.
    READER = GoogleAdsReader(
        settings=SETTINGS, token_provider=google_access_token
    )

    # The single execution path. Nothing else in the process may mutate an
    # account; tests/test_architecture.py fails the build if anything tries.
    EXECUTOR = GoogleAdsExecutor(
        settings=SETTINGS, token_provider=google_access_token
    )

    # Which accounts this server may touch, derived from the MCC instead of
    # a hand-written list. Built before the tier resolver because
    # `visible_tier` intersects with it.
    MANAGED_ACCOUNTS = ManagedAccountStore(
        reader=READER, login_customer_id=SETTINGS.login_customer_id
    )

    TIER_RESOLVER: TierResolver = _build_tier_resolver(
        settings=SETTINGS,
        role_store=ROLE_STORE,
        managed_accounts=MANAGED_ACCOUNTS,
        reader=READER,
    )
    # Prunes itself: no cron, no logrotate, nothing to remember. See
    # safety/audit.py for why rotation is in-process rather than in ops.
    AUDIT_LOG = AuditLog(
        SETTINGS.audit_log_path, retention_days=SETTINGS.audit_retention_days
    )
    SPEND_LEDGER = DailySpendLedger(AUDIT_LOG)
    PLAN_STORE = PlanStore()

    GUARD = Guard(
        settings=SETTINGS,
        policy_store=POLICY_STORE,
        tier_resolver=TIER_RESOLVER,
        audit_log=AUDIT_LOG,
        spend_ledger=SPEND_LEDGER,
        managed_accounts=MANAGED_ACCOUNTS,
    )
except ConfigError as exc:
    print(f"\n{exc}\n", file=sys.stderr)
    raise SystemExit(1) from exc


mcp = FastMCP(name="gads-write-mcp", auth=_build_auth_provider(SETTINGS))

# Registered before any tool so it wraps every one of them.
mcp.add_middleware(
    TierMiddleware(tier_resolver=TIER_RESOLVER, settings=SETTINGS)
)

# Unauthenticated liveness. Returns 503 when a config edit was refused, so a
# monitor notices we are serving an older policy than the files show.
register_health_route(
    mcp,
    settings=SETTINGS,
    role_store=ROLE_STORE,
)

# Phase 3 reads. Registered here, in the composition root, with their
# dependencies passed in explicitly - tools/reads.py reaches for no globals,
# which is what lets tests build the same tools against fakes.
register_read_tools(
    mcp,
    guard=GUARD,
    reader=READER,
    policy_store=POLICY_STORE,
    managed_accounts=MANAGED_ACCOUNTS,
)

# Phase 4 writes. These draft plans and apply them; they are hidden and
# refused entirely while GADS_WRITE_ENABLED is false.
register_write_tools(
    mcp,
    guard=GUARD,
    reader=READER,
    policy_store=POLICY_STORE,
    plan_store=PLAN_STORE,
)
register_confirm_tool(
    mcp,
    guard=GUARD,
    executor=EXECUTOR,
    plan_store=PLAN_STORE,
    settings=SETTINGS,
    # Confirm re-reads the entity a plan targets, because budget and bid
    # limits are relative and a plan stores an absolute target. See the
    # module docstring in tools/confirm.py.
    reader=READER,
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

    # Derived from the MCC, so this is a live Google call. health_check must
    # still answer when Google is unreachable - reporting the failure IS the
    # useful answer - so it is caught rather than raised.
    try:
        managed_count: int | None = len(await MANAGED_ACCOUNTS.all())
        managed_error = None
    except Exception as exc:  # noqa: BLE001 - reported, never raised
        managed_count = None
        managed_error = str(exc)

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
            "phase": "6 - all write tools live; two-step draft then confirm",
            "environment": SETTINGS.env,
            "base_url": SETTINGS.base_url,
            "google_ads_api_version": API_VERSION,
        },
        "safety": {
            # The kill switch. False means every write is refused at the
            # gate, regardless of tier, regardless of policy.
            "write_enabled": SETTINGS.write_enabled,
            "managed_accounts": managed_count,
            "managed_accounts_error": managed_error,
            "manager_customer_id": SETTINGS.login_customer_id,
            "open_plans": PLAN_STORE.open_count(),
            # How long a demotion in the Google Ads UI can take to bite.
            "tier_cache_seconds": SETTINGS.tier_cache_seconds,
            # Surfaced so a bad config edit is visible here rather than only
            # in the logs. Both should be null.
            "roles_reload_error": ROLE_STORE.last_error,
        },
        "credentials": {
            # Boolean only. The token itself never leaves auth/identity.py.
            "google_token_available": has_google_access_token(),
        },
        "note": (
            "Tier 'none' means you are authenticated but have no access. "
            "Ask a lead to add you in Google Ads."
            if tier is Tier.NONE
            else (
                "Reads are available. Writes are disabled on this server "
                "(GADS_WRITE_ENABLED=false)."
                if not SETTINGS.write_enabled
                else "Reads and campaign pause/enable are available. Every "
                "write returns a plan_id and applies only via confirm_and_apply."
            )
        ),
    }


def main() -> None:
    # The managed-account count is deliberately NOT logged here: it would
    # mean a Google round trip during boot, and the process must start even
    # when Google is unreachable. health_check reports it instead.
    logger.info(
        "starting gads-write-mcp env=%s host=%s port=%s base_url=%s "
        "write_enabled=%s tiers=%s manager=%s",
        SETTINGS.env,
        SETTINGS.host,
        SETTINGS.port,
        SETTINGS.base_url,
        SETTINGS.write_enabled,
        TIER_RESOLVER.source,
        SETTINGS.login_customer_id,
    )
    if SETTINGS.write_enabled:
        logger.warning("GADS_WRITE_ENABLED is true - this server can mutate accounts.")

    mcp.run(transport="http", host=SETTINGS.host, port=SETTINGS.port)


if __name__ == "__main__":
    main()
