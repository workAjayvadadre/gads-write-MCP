"""An unauthenticated liveness and readiness endpoint.

Every other route on this server requires OAuth, which meant the only way to
learn something was wrong was for a person to complain. A crash is the easy
case - PM2 restarts it and the logs say so. The case this exists for is
quieter:

    Someone edits roles.yaml and makes a typo. RoleStore does the right
    thing and keeps the last good version rather than crashing a live
    request. The server carries on serving perfectly, on the OLD table. The
    team believes the new role is in force. Nothing errors, nothing alerts,
    and nobody finds out.

roles.yaml is now the ONLY file that can fail this way. The spending rules
used to live in policy.yaml and were the original reason for this endpoint;
they are relative, fixed in code, or read from the environment now, so a bad
edit cannot silently leave old limits running - there is no edit to make.

`/healthz` is what makes that visible without a login.

Why a refused reload returns 503 rather than 200: this runs as a single PM2
process behind Nginx, not behind a load balancer that would evict it, so a
503 costs no availability and is the one status code every monitoring tool
alerts on by default. The alternative - 200 with a status field - relies on
whoever configures the monitor reading the body, which is exactly the kind of
step that gets skipped.

What it deliberately does NOT say: no customer IDs, no email addresses, no
token or secret material, no account names. It is unauthenticated, so it is
written on the assumption that anyone on the internet can read it.
"""

from __future__ import annotations

import logging
from typing import Any

from starlette.requests import Request
from starlette.responses import JSONResponse

logger = logging.getLogger(__name__)


def register_health_route(
    mcp: Any,
    *,
    settings: Any,
    role_store: Any,
    path: str = "/healthz",
) -> None:
    """Add the health endpoint to `mcp`.

    Takes its dependencies explicitly, like every other registration in this
    codebase, so a test can build one against real stores without booting the
    server or standing up OAuth.
    """

    @mcp.custom_route(path, methods=["GET"])
    async def healthz(request: Request) -> JSONResponse:  # noqa: ARG001
        roles_error = getattr(role_store, "last_error", None)
        degraded = bool(roles_error)

        body = {
            # "ok" means: alive, and serving the roles table on disk.
            # "degraded" means: alive, but a roles.yaml edit was REFUSED and
            # we are running on an older version than the file shows.
            "status": "degraded" if degraded else "ok",
            "write_enabled": bool(getattr(settings, "write_enabled", False)),
            "roles_reload_error": roles_error,
        }
        if degraded:
            logger.warning("/healthz reporting degraded: roles=%r", roles_error)
        return JSONResponse(body, status_code=503 if degraded else 200)


__all__ = ["register_health_route"]
