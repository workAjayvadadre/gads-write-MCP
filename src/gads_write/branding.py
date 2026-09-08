"""The server's own identity: its icon and its home page.

Why the icon is embedded rather than served from a URL:

    Every route on this server except /healthz sits behind Google OAuth, so
    an icon hosted here would be fetched by a client that is not yet logged
    in and would come back 401. Hosting it publicly instead means an Nginx
    rule, a file on the box, and one more thing that can 404 after a
    hostname change. A data URI travels inside the `initialize` response
    itself, so there is nothing to serve, nothing to keep alive, and nothing
    that breaks when the domain moves.

The PNGs live in `assets/` beside this module rather than at the repo root
so that they ship with the package, not just with a checkout.

`assets/logo-120.png` is not used here. It is the size Google wants for the
OAuth consent screen, which is uploaded by hand in the Cloud Console and is
the logo the team will actually see when they connect.

Python note for a TypeScript reader:
  `importlib.resources` is the supported way to read a file that ships
  inside a package. Building a path from `__file__` happens to work for an
  editable install and breaks for a zipped one.
"""

from __future__ import annotations

import base64
import logging
from importlib import resources

from mcp.types import Icon

logger = logging.getLogger(__name__)

# Small enough to embed comfortably (~4 KB, ~5.5 KB once base64-encoded) and
# large enough that a client showing it at 48px still has pixels to spare.
_ICON_FILE = "logo-96.png"
_ICON_SIZES = ["96x96", "48x48"]


def _icon_data_uri() -> str | None:
    """The icon as a data URI, or None if it cannot be read.

    A missing icon must never stop the server starting. It is decoration;
    everything else here is not.
    """
    try:
        data = (resources.files(__package__) / "assets" / _ICON_FILE).read_bytes()
    except (OSError, ModuleNotFoundError) as exc:  # noqa: BLE001 - logged, not fatal
        logger.warning("could not read the server icon %s: %s", _ICON_FILE, exc)
        return None
    return "data:image/png;base64," + base64.b64encode(data).decode("ascii")


def server_icons() -> list[Icon] | None:
    """Icons to advertise in the MCP handshake, or None if unavailable."""
    src = _icon_data_uri()
    if src is None:
        return None
    return [Icon(src=src, mimeType="image/png", sizes=list(_ICON_SIZES))]


__all__ = ["server_icons"]
