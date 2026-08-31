"""Who is calling, according to Google — not according to the client.

Everything here reads from the OAuth token that FastMCP validated. Nothing
reads from tool arguments or HTTP headers the caller controls. That
distinction is the whole point: a caller can pass any email they like as a
tool argument, but they cannot forge a Google-signed token claim.

Python notes for a TypeScript reader:
  - `get_access_token()` is a context-local lookup, similar to AsyncLocalStorage
    in Node. It works inside a tool call without being passed down explicitly.
  - `getattr(obj, "name", None)` is a safe property read, like `obj?.name`.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from fastmcp.server.dependencies import get_access_token


class AuthError(RuntimeError):
    """Raised when the caller's identity cannot be established."""


@dataclass(frozen=True)
class Caller:
    """The authenticated human behind this request."""

    email: str
    subject: str | None = None
    name: str | None = None
    # Never included in repr. Nothing should ever print this.
    _raw_claims: dict = field(default_factory=dict, repr=False)


def current_caller() -> Caller:
    """Return the authenticated caller, or raise.

    There is no anonymous path. If we cannot prove who this is, we refuse.
    """
    token = get_access_token()
    if token is None:
        raise AuthError(
            "No authenticated user on this request. The server requires Google "
            "OAuth; this usually means the client connected without completing "
            "the authorization flow."
        )

    claims = getattr(token, "claims", None) or {}
    email = claims.get("email")

    if not email:
        raise AuthError(
            "Authenticated, but the token carries no email claim. Confirm the "
            "OAuth client requests the 'openid' and "
            "'https://www.googleapis.com/auth/userinfo.email' scopes."
        )

    return Caller(
        email=str(email).strip().lower(),
        subject=claims.get("sub"),
        name=claims.get("name"),
        _raw_claims=claims,
    )


# ---------------------------------------------------------------------------
# Upstream Google credential
# ---------------------------------------------------------------------------
# `AccessToken.token` holds the real Google access token, NOT the FastMCP JWT.
# That is not obvious and is worth stating precisely, because using the wrong
# one produces a 401 from Google Ads that looks like a permissions problem.
#
# Verified against fastmcp 3.4.7 source:
#   - The MCP client holds a FastMCP-issued JWT. OAuthProxy.load_access_token
#     maps its JTI to an UpstreamTokenSet held server-side.
#   - _get_verification_token() returns upstream_token_set.access_token
#     (oauth_proxy/proxy.py:1692) — the Google token — and hands it to the
#     token verifier.
#   - GoogleTokenVerifier builds AccessToken(token=token, ...) with that same
#     string (providers/google.py:174), so `.token` is Google's.
#   - The one path that could substitute a different token is
#     _uses_alternate_verification(); it returns False for OAuthProxy, and
#     where it is True (OIDCProxy) the result is patched back to
#     upstream_token_set.access_token anyway (proxy.py:1937). Either way,
#     `.token` is the upstream Google credential.
#
# Re-check this if fastmcp is upgraded across a major version.
_UPSTREAM_TOKEN_ATTR = "token"


def _extract_upstream_token(token: object) -> str | None:
    value = getattr(token, _UPSTREAM_TOKEN_ATTR, None)
    if isinstance(value, str) and value:
        return value
    return None


def google_access_token() -> str:
    """Return the caller's Google OAuth access token for the Ads API.

    Used from Phase 3 onward by ads/client.py. Every Google Ads call is made
    with the calling user's own credential, so Google's own permission model
    is the outermost guard: someone removed from the MCC loses access
    immediately, with no redeploy and no config change here.
    """
    token = get_access_token()
    if token is None:
        raise AuthError("No authenticated user on this request.")

    upstream = _extract_upstream_token(token)
    if upstream is None:
        # Report the SHAPE of the object, never its contents.
        available = sorted(
            name for name in dir(token)
            if not name.startswith("_") and not callable(getattr(token, name, None))
        )
        raise AuthError(
            "Could not locate the upstream Google access token on the FastMCP "
            f"token object. Attributes present: {available}. "
            "Update _UPSTREAM_TOKEN_ATTR in auth/identity.py to match — this "
            "usually means fastmcp was upgraded across a major version."
        )
    return upstream


def has_google_access_token() -> bool:
    """True if an upstream Google credential is reachable for this caller.

    Returns a boolean and nothing else — the token itself never leaves this
    module. Used by health_check to prove the Phase 3 credential path exists
    before any code depends on it.
    """
    try:
        token = get_access_token()
    except Exception:
        return False
    if token is None:
        return False
    return _extract_upstream_token(token) is not None
