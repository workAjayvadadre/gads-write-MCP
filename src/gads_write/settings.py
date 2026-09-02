"""Boot configuration, validated once at startup.

Design rule: this module either returns a fully valid Settings object or
raises and kills the process. There is no "mostly configured" state. A
server that boots with a missing developer token and only discovers it
when someone changes a budget is worse than a server that refuses to start.

Python notes for a TypeScript reader:
  - `@dataclass(frozen=True)` is roughly `readonly` fields on a class. It
    generates __init__ and __eq__ for you.
  - `field(repr=False)` removes a field from the auto-generated __repr__.
    We use it on every secret so that printing or logging a Settings object
    can never leak a credential.
  - `str | None` is the union type, same idea as `string | null`.
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass, field
from pathlib import Path

from dotenv import load_dotenv

# Repo root = three levels up from this file (src/gads_write/settings.py).
REPO_ROOT = Path(__file__).resolve().parents[2]


class ConfigError(RuntimeError):
    """Raised at boot when the environment or config files are not usable."""


@dataclass(frozen=True)
class Settings:
    # --- deployment ---
    env: str
    host: str
    port: int
    base_url: str

    # --- google oauth ---
    oauth_client_id: str
    oauth_client_secret: str = field(repr=False)
    jwt_signing_key: str | None = field(repr=False)

    # --- google ads (unused until phase 3, validated now) ---
    developer_token: str = field(repr=False)
    login_customer_id: str

    # --- safety ---
    write_enabled: bool

    # --- paths ---
    policy_path: Path
    roles_path: Path
    audit_log_path: Path

    # --- phase 3 ---
    # How long a resolved Google Ads tier may be reused before it is looked
    # up again. See auth/google_ads_roles.py:TierCache for the trade-off;
    # 0 disables caching and pays a Google round trip on every call.
    tier_cache_seconds: int = 60

    # How many days of audit files the log keeps before deleting them itself.
    # Rotation is done in-process on purpose: the daily spend ceiling is
    # derived from these files, so an external logrotate rule would silently
    # reset everyone's allowance. See safety/audit.py.
    audit_retention_days: int = 400

    # When true, confirm_and_apply stops and asks the connected client to
    # show the preview to a person, and applies nothing unless they accept.
    # This is what makes human approval a property of the SERVER rather than
    # a habit of the client: two tool calls in one model turn are no longer
    # enough to change anything. Defaults to true; turning it off is a
    # deliberate act, like the kill switch.
    require_human_confirmation: bool = True

    @property
    def is_production(self) -> bool:
        return self.env == "production"


# --------------------------------------------------------------------------
# small helpers
# --------------------------------------------------------------------------

def _env(name: str, default: str | None = None) -> str | None:
    value = os.environ.get(name, default)
    if value is None:
        return None
    value = value.strip()
    return value or None


def _env_bool(name: str, default: bool) -> bool:
    raw = _env(name)
    if raw is None:
        return default
    lowered = raw.lower()
    if lowered in ("true", "1", "yes", "on"):
        return True
    if lowered in ("false", "0", "no", "off"):
        return False
    raise ConfigError(
        f"{name} must be true or false, got {raw!r}. "
        "Ambiguous values are rejected on purpose: a typo here could enable writes."
    )


def _resolve(path_str: str) -> Path:
    """Relative paths are resolved against the repo root, not the process CWD.

    PM2 does not always start the process in the directory you think it does.
    """
    path = Path(path_str)
    return path if path.is_absolute() else (REPO_ROOT / path)


# --------------------------------------------------------------------------
# validation
# --------------------------------------------------------------------------

def _validate_config_files(policy_path: Path, roles_path: Path) -> list[str]:
    """Confirm both YAML files exist and are fully valid.

    Validation is delegated to the same parsers the running server uses, so
    boot-time checks and runtime checks can never drift apart. Imports are
    local to this function to keep module import order simple.
    """
    from .auth.roles import RoleConfigError, RoleTable
    from .safety.policy import PolicyError, load_policy_file

    problems: list[str] = []

    if not policy_path.is_file():
        problems.append(f"policy file not found at {policy_path}")
    else:
        try:
            load_policy_file(policy_path)
        except PolicyError as exc:
            problems.append(str(exc))

    if not roles_path.is_file():
        problems.append(f"roles file not found at {roles_path}")
    else:
        try:
            RoleTable.load(roles_path)
        except RoleConfigError as exc:
            problems.append(str(exc))

    return problems


def load_settings(*, env_file: Path | None = None) -> Settings:
    """Read the environment, validate everything, and return Settings.

    Every problem is collected and reported together. Fixing config one
    error per redeploy is miserable, so we fail loudly but completely.
    """
    load_dotenv(env_file or (REPO_ROOT / ".env"), override=False)

    problems: list[str] = []

    env_name = _env("GADS_ENV", "development") or "development"
    host = _env("GADS_HOST", "127.0.0.1") or "127.0.0.1"

    raw_port = _env("GADS_PORT", "8081") or "8081"
    port = 0
    try:
        port = int(raw_port)
        if not (1 <= port <= 65535):
            raise ValueError
    except ValueError:
        problems.append(f"GADS_PORT must be a port number, got {raw_port!r}")

    base_url = _env("GADS_BASE_URL")
    if not base_url:
        problems.append("GADS_BASE_URL is required (e.g. https://adswrite.indiraivf.in)")
    else:
        base_url = base_url.rstrip("/")
        if env_name == "production" and not base_url.startswith("https://"):
            problems.append(
                f"GADS_BASE_URL must be https in production, got {base_url!r}. "
                "OAuth over plain http would expose the authorization code."
            )

    client_id = _env("GADS_OAUTH_CLIENT_ID")
    if not client_id:
        problems.append("GADS_OAUTH_CLIENT_ID is required")
    elif not client_id.endswith(".apps.googleusercontent.com"):
        problems.append(
            f"GADS_OAUTH_CLIENT_ID does not look like a Google client ID "
            f"(expected it to end in .apps.googleusercontent.com)"
        )

    client_secret = _env("GADS_OAUTH_CLIENT_SECRET")
    if not client_secret:
        problems.append("GADS_OAUTH_CLIENT_SECRET is required")

    jwt_signing_key = _env("GADS_JWT_SIGNING_KEY")
    if env_name == "production" and not jwt_signing_key:
        problems.append(
            "GADS_JWT_SIGNING_KEY is required in production. Without a stable key, "
            "every restart invalidates every session and all users must re-authorise."
        )

    developer_token = _env("GADS_DEVELOPER_TOKEN")
    if not developer_token:
        problems.append(
            "GADS_DEVELOPER_TOKEN is required. Every Google Ads call is made with "
            "it alongside the calling user's own OAuth token."
        )

    raw_cache = _env("GADS_TIER_CACHE_SECONDS", "60") or "60"
    tier_cache_seconds = 60
    try:
        tier_cache_seconds = int(raw_cache)
        if tier_cache_seconds < 0:
            raise ValueError
        if tier_cache_seconds > 900:
            # A long TTL turns a demotion in the Google Ads UI into something
            # that takes effect "eventually", which is the exact failure the
            # resolve-per-call requirement exists to prevent.
            problems.append(
                f"GADS_TIER_CACHE_SECONDS is {tier_cache_seconds}, which is longer "
                "than 15 minutes. A removed user would keep their access for that "
                "long. Lower it, or set 0 to disable caching entirely."
            )
    except ValueError:
        problems.append(
            f"GADS_TIER_CACHE_SECONDS must be a non-negative whole number of "
            f"seconds, got {raw_cache!r}"
        )

    login_customer_id = _env("GADS_LOGIN_CUSTOMER_ID")
    if not login_customer_id:
        problems.append("GADS_LOGIN_CUSTOMER_ID is required (your MCC, digits only)")
    elif not re.fullmatch(r"\d{10}", login_customer_id):
        problems.append(
            f"GADS_LOGIN_CUSTOMER_ID must be 10 digits with no dashes, "
            f"got {login_customer_id!r}"
        )

    try:
        write_enabled = _env_bool("GADS_WRITE_ENABLED", False)
    except ConfigError as exc:
        problems.append(str(exc))
        write_enabled = False

    try:
        # Defaults to true. Turning it off means a model can apply a drafted
        # change with no person in the loop, so it has to be typed out.
        require_human_confirmation = _env_bool("GADS_REQUIRE_HUMAN_CONFIRMATION", True)
    except ConfigError as exc:
        problems.append(str(exc))
        require_human_confirmation = True

    raw_retention = _env("GADS_AUDIT_RETENTION_DAYS", "400") or "400"
    audit_retention_days = 400
    try:
        audit_retention_days = int(raw_retention)
        if audit_retention_days < 1:
            raise ValueError
    except ValueError:
        problems.append(
            f"GADS_AUDIT_RETENTION_DAYS must be a whole number of days, at least 1, "
            f"got {raw_retention!r}. The audit log is the daily spend ceiling's only "
            "evidence, so it cannot be set to keep nothing."
        )

    policy_path = _resolve(_env("GADS_POLICY_PATH", "config/policy.yaml") or "config/policy.yaml")
    roles_path = _resolve(_env("GADS_ROLES_PATH", "config/roles.yaml") or "config/roles.yaml")
    audit_log_path = _resolve(_env("GADS_AUDIT_LOG_PATH", "logs/audit.jsonl") or "logs/audit.jsonl")

    problems.extend(_validate_config_files(policy_path, roles_path))

    if problems:
        bullets = "\n".join(f"  - {p}" for p in problems)
        raise ConfigError(
            f"Refusing to start: {len(problems)} configuration problem(s).\n{bullets}\n"
            "Fix these in .env or config/, then restart."
        )

    return Settings(
        env=env_name,
        host=host,
        port=port,
        base_url=base_url,  # type: ignore[arg-type]  # proven non-None above
        oauth_client_id=client_id,  # type: ignore[arg-type]
        oauth_client_secret=client_secret,  # type: ignore[arg-type]
        jwt_signing_key=jwt_signing_key,
        developer_token=developer_token,  # type: ignore[arg-type]
        login_customer_id=login_customer_id,  # type: ignore[arg-type]
        write_enabled=write_enabled,
        policy_path=policy_path,
        roles_path=roles_path,
        audit_log_path=audit_log_path,
        tier_cache_seconds=tier_cache_seconds,
        audit_retention_days=audit_retention_days,
        require_human_confirmation=require_human_confirmation,
    )
