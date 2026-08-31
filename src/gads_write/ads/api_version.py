"""The one place the Google Ads API version is decided.

Pinned on purpose rather than taking the client library's default. The
library ships several API versions side by side (31.4.0 ships v21 through
v25) and moves its own default forward on upgrade. If we relied on that
default, a routine `pip install --upgrade` would silently re-point every
query and every future mutation at an API version nobody reviewed, where a
field may have been renamed, removed, or changed units.

So: this constant moves only when a human moves it, and moving it means
re-running the field checks in tests/test_ads_reads.py.

Python note for a TypeScript reader:
  `find_spec` asks "is this module importable?" without importing it. That
  matters because importing a whole API version package pulls in thousands
  of generated proto classes and takes seconds; we only want the existence
  check at startup.
"""

from __future__ import annotations

from importlib.util import find_spec

# Verified against google-ads 31.4.0. Every resource and field used in
# ads/reads.py was checked against this version's generated protos.
API_VERSION = "v25"


def _assert_available() -> None:
    """Fail at import time, not at the first query.

    A version mismatch discovered when someone asks for a campaign report is
    a confusing runtime error. Discovered at boot it is an obvious deploy
    problem, which is the whole fail-fast posture of this server.
    """
    if find_spec(f"google.ads.googleads.{API_VERSION}") is None:
        raise RuntimeError(
            f"The installed google-ads library does not ship Google Ads API "
            f"{API_VERSION}, which this code was written against. Either "
            f"install a library version that includes it, or update "
            f"API_VERSION in ads/api_version.py and re-verify every field "
            f"name in ads/reads.py against the new version first."
        )


_assert_available()
