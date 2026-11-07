"""FastAPI 앱 진입점. 실행: `uv run uvicorn app.main:app --reload` (backend 폴더에서)."""

import time
from collections.abc import Awaitable, Callable

from fastapi import FastAPI, Request, Response

from app.api.router import api_router
from app.core.config import get_settings
from app.core.logging import configure_logging, get_logger


def create_app() -> FastAPI:
    settings = get_settings()
    configure_logging(settings.log_level, json=settings.log_json, file=settings.log_file)
    log = get_logger("app.http")

    app = FastAPI(title="chanjin-rag", version="0.1.0")
    app.include_router(api_router)

    @app.middleware("http")
    async def access_log(
        request: Request, call_next: Callable[[Request], Awaitable[Response]]
    ) -> Response:
        started = time.perf_counter()
        response = await call_next(request)
        log.info(
            "http.request",
            method=request.method,
            path=request.url.path,
            status=response.status_code,
            ms=round((time.perf_counter() - started) * 1000, 1),
        )
        return response

    get_logger("app").info("app.started", env=settings.app_env)
    return app


app = create_app()
