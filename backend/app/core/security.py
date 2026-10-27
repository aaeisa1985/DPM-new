"""Local-only security middleware: Host allow-list, Origin check, optional token."""

from __future__ import annotations

from urllib.parse import urlparse

from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import JSONResponse

from app.core.config import settings

WRITE_METHODS = {"POST", "PUT", "PATCH", "DELETE"}


class LocalSecurityMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next):
        host = (request.headers.get("host") or "").split(":")[0].strip().lower()
        if host and host not in settings.hosts:
            return JSONResponse(
                {"error": {"code": "host_rejected", "detail": f"Host '{host}' is not allowed."}},
                status_code=403,
            )

        if request.method in WRITE_METHODS:
            origin = request.headers.get("origin")
            if origin:
                origin_host = (urlparse(origin).hostname or "").lower()
                if origin_host not in settings.hosts:
                    return JSONResponse(
                        {
                            "error": {
                                "code": "origin_rejected",
                                "detail": f"Origin '{origin}' is not allowed for write requests.",
                            }
                        },
                        status_code=403,
                    )
            if settings.local_token:
                if request.headers.get("x-dpm-token") != settings.local_token:
                    return JSONResponse(
                        {"error": {"code": "token_required", "detail": "X-DPM-Token missing or invalid."}},
                        status_code=401,
                    )

        return await call_next(request)
