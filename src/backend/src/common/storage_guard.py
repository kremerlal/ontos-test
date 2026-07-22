"""Block API writes only for capabilities marked unavailable."""

from __future__ import annotations

from typing import Awaitable, Callable

from fastapi import Request, Response
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.responses import JSONResponse

from src.common.config import get_settings
from src.common.logging import get_logger
from src.common.storage_mode import StorageMode, resolve_storage_mode

logger = get_logger(__name__)

WRITE_ALLOW_PREFIXES = (
    "/api/health",
    "/api/storage",
    "/api/uc-native",
    "/api/version",
    "/api/cache-version",
    "/api/data-catalog",
    "/api/catalog-commander",
    "/api/user",
    "/api/data-products",
    "/api/data-contracts",
    "/api/assets",
    "/api/data-domains",
    "/api/tags",
    "/api/comments",
    "/api/notifications",
    "/api/access-grants",
    "/api/workflows",
    "/api/semantic-models",
    "/api/settings",
    "/api/directory",
    "/static/",
)

WRITE_ALLOW_EXACT = ("/api/settings/ui-customization",)


class StorageWriteGuardMiddleware(BaseHTTPMiddleware):
    """Reject write operations when deployment has writes disabled."""

    async def dispatch(
        self,
        request: Request,
        call_next: Callable[[Request], Awaitable[Response]],
    ) -> Response:
        if request.method in ("GET", "HEAD", "OPTIONS"):
            return await call_next(request)

        path = request.url.path
        if path.startswith(WRITE_ALLOW_PREFIXES) or path in WRITE_ALLOW_EXACT:
            return await call_next(request)

        try:
            settings = get_settings()
            mode = resolve_storage_mode(settings)
        except Exception:
            return await call_next(request)

        if mode == StorageMode.UC_NATIVE:
            return await call_next(request)

        if mode != StorageMode.UC_READONLY:
            return await call_next(request)

        logger.warning("Blocked %s %s in uc_readonly mode", request.method, path)
        return JSONResponse(
            status_code=503,
            content={
                "error": "read_only_mode",
                "detail": (
                    "This deployment runs in uc_readonly mode. "
                    "Use STORAGE_MODE=uc_native for write-capable UC storage."
                ),
                "capability": "writes_enabled",
            },
        )
