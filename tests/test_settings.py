"""Boot validation: the server must refuse to start when misconfigured.

Since Phase 2, `_validate_config_files` delegates to the same parsers the
running server uses, so boot-time and runtime validity can never drift apart.
That means these tests build real, fully valid config documents rather than
minimal stubs.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from gads_write.settings import ConfigError, Settings, load_settings

ENV_KEYS = (
    "GADS_ENV",
    "GADS_BASE_URL",
    "GADS_OAUTH_CLIENT_ID",
    "GADS_OAUTH_CLIENT_SECRET",
    "GADS_JWT_SIGNING_KEY",
    "GADS_DEVELOPER_TOKEN",
    "GADS_LOGIN_CUSTOMER_ID",
    "GADS_WRITE_ENABLED",
    "GADS_ROLES_PATH",
    "GADS_AUDIT_LOG_PATH",
    "GADS_HOST",
    "GADS_PORT",
    "GADS_TIER_CACHE_SECONDS",
    "GADS_AUDIT_RETENTION_DAYS",
)


@pytest.fixture
def env(monkeypatch: pytest.MonkeyPatch, write_roles):
    """A valid environment. Tests mutate it to prove each check fires."""
    for key in ENV_KEYS:
        monkeypatch.delenv(key, raising=False)

    monkeypatch.setenv("GADS_ENV", "production")
    monkeypatch.setenv("GADS_BASE_URL", "https://adswrite.example.com")
    monkeypatch.setenv("GADS_OAUTH_CLIENT_ID", "123.apps.googleusercontent.com")
    monkeypatch.setenv("GADS_OAUTH_CLIENT_SECRET", "GOCSPX-secret")
    monkeypatch.setenv("GADS_JWT_SIGNING_KEY", "a-stable-signing-key")
    monkeypatch.setenv("GADS_DEVELOPER_TOKEN", "dev-token")
    monkeypatch.setenv("GADS_LOGIN_CUSTOMER_ID", "1234567890")
    monkeypatch.setenv("GADS_WRITE_ENABLED", "false")
    monkeypatch.setenv("GADS_ROLES_PATH", str(write_roles()))
    return monkeypatch


def _load(tmp_path: Path) -> Settings:
    # env_file points at a path that does not exist, so a developer's real
    # .env on disk can never influence a test result.
    return load_settings(env_file=tmp_path / "no-such.env")


# ---------------------------------------------------------------------------
# audit retention
# ---------------------------------------------------------------------------
# The audit log prunes itself so the server can run unattended. The window is
# configurable, but a nonsensical value must not silently become "keep
# nothing" - that would delete the daily spend ceiling's own evidence.


def test_audit_retention_defaults_to_a_sane_window(env, tmp_path: Path) -> None:
    assert _load(tmp_path).audit_retention_days >= 365


def test_audit_retention_can_be_configured(env, tmp_path: Path) -> None:
    env.setenv("GADS_AUDIT_RETENTION_DAYS", "90")
    assert _load(tmp_path).audit_retention_days == 90


@pytest.mark.parametrize("bad", ["0", "-5", "forever"])
def test_a_nonsensical_retention_window_refuses_to_start(
    env, tmp_path: Path, bad: str
) -> None:
    env.setenv("GADS_AUDIT_RETENTION_DAYS", bad)
    with pytest.raises(ConfigError) as caught:
        _load(tmp_path)
    assert "GADS_AUDIT_RETENTION_DAYS" in str(caught.value)


# ---------------------------------------------------------------------------
# the happy path
# ---------------------------------------------------------------------------

def test_valid_config_loads(env, tmp_path: Path) -> None:
    settings = _load(tmp_path)
    assert settings.write_enabled is False
    assert settings.is_production is True


def test_write_switch_defaults_to_off(env, tmp_path: Path) -> None:
    env.delenv("GADS_WRITE_ENABLED", raising=False)
    assert _load(tmp_path).write_enabled is False


# ---------------------------------------------------------------------------
# environment
# ---------------------------------------------------------------------------

def test_ambiguous_write_switch_is_rejected(env, tmp_path: Path) -> None:
    # "maybe" must not quietly become False, nor True.
    env.setenv("GADS_WRITE_ENABLED", "maybe")
    with pytest.raises(ConfigError):
        _load(tmp_path)


def test_http_base_url_rejected_in_production(env, tmp_path: Path) -> None:
    env.setenv("GADS_BASE_URL", "http://adswrite.example.com")
    with pytest.raises(ConfigError, match="https"):
        _load(tmp_path)


def test_missing_developer_token_fails_at_boot(env, tmp_path: Path) -> None:
    env.delenv("GADS_DEVELOPER_TOKEN", raising=False)
    with pytest.raises(ConfigError, match="GADS_DEVELOPER_TOKEN"):
        _load(tmp_path)


def test_customer_id_with_dashes_is_rejected(env, tmp_path: Path) -> None:
    env.setenv("GADS_LOGIN_CUSTOMER_ID", "123-456-7890")
    with pytest.raises(ConfigError, match="10 digits"):
        _load(tmp_path)


def test_all_problems_are_reported_together(env, tmp_path: Path) -> None:
    # Fixing config one error per redeploy is miserable.
    env.delenv("GADS_DEVELOPER_TOKEN", raising=False)
    env.delenv("GADS_OAUTH_CLIENT_SECRET", raising=False)
    env.setenv("GADS_LOGIN_CUSTOMER_ID", "nope")

    with pytest.raises(ConfigError) as caught:
        _load(tmp_path)

    message = str(caught.value)
    assert "GADS_DEVELOPER_TOKEN" in message
    assert "GADS_OAUTH_CLIENT_SECRET" in message
    assert "GADS_LOGIN_CUSTOMER_ID" in message


# ---------------------------------------------------------------------------
# config files, validated by the same parsers the server uses
# ---------------------------------------------------------------------------

# The three policy-file tests that stood here are gone with the file. There is
# no GADS_POLICY_PATH, no YAML to mistype and no shape to get wrong: the
# spending rules are relative, fixed in code, and read from the environment.
# What remains configurable is covered by the two tests below.


def test_the_increase_percent_defaults_and_can_be_overridden(env, tmp_path: Path) -> None:
    from decimal import Decimal

    assert _load(tmp_path).max_increase_percent == Decimal(100)
    env.setenv("GADS_MAX_INCREASE_PERCENT", "50")
    assert _load(tmp_path).max_increase_percent == Decimal(50)


@pytest.mark.parametrize("bad", ["-1", "abc"])
def test_a_nonsensical_increase_percent_refuses_to_start(
    env, tmp_path: Path, bad: str
) -> None:
    env.setenv("GADS_MAX_INCREASE_PERCENT", bad)
    with pytest.raises(ConfigError, match="GADS_MAX_INCREASE_PERCENT"):
        _load(tmp_path)


def test_an_empty_increase_percent_means_the_default(env, tmp_path: Path) -> None:
    """Empty reads as unset, the same as every other variable here. Refusing
    to boot on a blank line somebody left in .env would be unhelpful."""
    from decimal import Decimal

    env.setenv("GADS_MAX_INCREASE_PERCENT", "")
    assert _load(tmp_path).max_increase_percent == Decimal(100)


def test_url_domains_are_split_and_normalised(env, tmp_path: Path) -> None:
    env.setenv("GADS_ALLOWED_URL_DOMAINS", " IndiraIVF.com , www.indiraivf.com ,")
    assert _load(tmp_path).allowed_url_domains == frozenset(
        {"indiraivf.com", "www.indiraivf.com"}
    )


def test_no_url_domains_is_allowed_and_means_no_new_ads(env, tmp_path: Path) -> None:
    """The right default for a server that has not been told which domains
    are its own: creating an ad is refused rather than pointed anywhere."""
    assert _load(tmp_path).allowed_url_domains == frozenset()


def test_roles_in_file_mode_without_a_lead_is_rejected(
    env, tmp_path: Path, write_roles
) -> None:
    env.setenv(
        "GADS_ROLES_PATH",
        str(write_roles({"users": {"analyst@example.com": "readonly"}})),
    )
    with pytest.raises(ConfigError, match="lead"):
        _load(tmp_path)


def test_roles_in_google_ads_mode_may_be_empty(env, tmp_path: Path, write_roles) -> None:
    # Phase 3: the file becomes a break-glass override, normally empty.
    env.setenv(
        "GADS_ROLES_PATH", str(write_roles({"mode": "google_ads", "users": {}}))
    )
    assert _load(tmp_path).is_production


# ---------------------------------------------------------------------------
# secrets
# ---------------------------------------------------------------------------

def test_secrets_are_not_in_repr(env, tmp_path: Path) -> None:
    # A Settings object gets printed in logs and tracebacks. It must not
    # carry credentials with it when that happens.
    rendered = repr(_load(tmp_path))
    assert "GOCSPX-secret" not in rendered
    assert "dev-token" not in rendered
    assert "a-stable-signing-key" not in rendered
