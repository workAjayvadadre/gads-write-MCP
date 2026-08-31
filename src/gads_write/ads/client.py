"""Builds a Google Ads client bound to the CALLING USER's own credential.

This is the module that makes "per-user OAuth" true rather than aspirational.
There is no refresh token on disk anywhere in this repo. Every client is
constructed from the access token that Google issued to the human making
this request, so Google's own permission model is the outermost guard: a
person removed from the MCC stops working here immediately, with no redeploy,
no config edit, and nothing for us to remember to revoke.

Two consequences worth understanding:

  A client is per-request, not per-process. The credential belongs to one
  user and expires, so it cannot be built once at startup and shared. That
  costs a gRPC channel setup per call, which is real but small next to a
  Google Ads round trip. Caching channels keyed by token would be a genuine
  footgun: tokens rotate, and a stale channel would silently act as the
  previous user.

  There is no refresh token on the credential. FastMCP's OAuth proxy owns
  refreshing and hands us a currently-valid access token. `Credentials`
  reports `valid=True` with no refresh token as long as no expiry is set,
  which is exactly the behaviour we want: use it now, never try to renew it
  ourselves.

Python notes for a TypeScript reader:
  - `google.oauth2.credentials.Credentials` is a value object wrapping a
    bearer token. Passing `token=` and nothing else is the "I already have a
    valid access token" case.
  - Keyword-only arguments after `*` mean callers must name every argument.
    That is deliberate here: `login_customer_id` and `developer_token` are
    both opaque digit strings and swapping them would be easy and silent.
"""

from __future__ import annotations

from google.ads.googleads.client import GoogleAdsClient
from google.oauth2.credentials import Credentials

from ..settings import Settings
from .api_version import API_VERSION


def build_client(
    *,
    settings: Settings,
    access_token: str,
    login_customer_id: str | None = None,
) -> GoogleAdsClient:
    """Construct a Google Ads client for one request, as one user.

    `login_customer_id` is the manager account the request is made "through",
    the API equivalent of picking an account from the switcher in the Google
    Ads UI. It defaults to the configured MCC. Google resolves the caller's
    effective role against this account, which is why it is not optional in
    practice.
    """
    if not access_token:
        # Defensive: an empty token would produce an opaque UNAUTHENTICATED
        # from Google that reads like a permissions problem.
        raise ValueError(
            "access_token is empty. The caller's Google credential could not "
            "be read; see auth/identity.py:google_access_token()."
        )

    credentials = Credentials(token=access_token)

    return GoogleAdsClient(
        credentials=credentials,
        developer_token=settings.developer_token,
        login_customer_id=login_customer_id or settings.login_customer_id,
        # Pinning the version on the client makes it stick for every service
        # and type lookup made through it: the library starts each one with
        # `version = self.version if self.version else version`, so an
        # explicit value here overrides the library's own moving default.
        version=API_VERSION,
        # Return proto-plus messages, which have ordinary Python attribute
        # access and real enums, rather than raw protobuf.
        use_proto_plus=True,
    )
