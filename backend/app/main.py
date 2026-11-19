"""FastAPI 앱 진입점. 실행: `uv run uvicorn app.main:app --reload` (backend 폴더에서)."""

import time
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request, Response

from app.api.deps import apply_session_cookie
from app.api.errors import install_error_handlers
from app.api.router import api_router
from app.core.config import get_settings
from app.core.logging import configure_logging, get_logger
from app.db.engine import dispose_engine, get_sessionmaker
from app.providers.monitor import configure_recorder


@asynccontextmanager
async def lifespan(_: FastAPI) -> AsyncIterator[None]:
    # 채팅의 모델 호출 결과를 provider_status에 남긴다(`/health`, TRD 12.4)
    configure_recorder(get_sessionmaker())
    yield
    configure_recorder(None)
    await dispose_engine()


def create_app() -> FastAPI:
    settings = get_settings()
    configure_logging(settings.log_level, json=settings.log_json, file=settings.log_file)
    log = get_logger("app.http")

    app = FastAPI(title="chanjin-rag", version="0.1.0", lifespan=lifespan)
    install_error_handlers(app)
    app.include_router(api_router)

    @app.middleware("http")
    async def access_log(
        request: Request, call_next: Callable[[Request], Awaitable[Response]]
    ) -> Response:
        started = time.perf_counter()
        response = await call_next(request)
        # 세션을 슬라이딩 갱신한 요청이면 쿠키 만료도 늘린다(app.api.deps 참고)
        apply_session_cookie(request, response)
        log.info(
            "http.request",
            method=request.method,
            path=request.url.path,
            status=response.status_code,
            ms=round((time.perf_counter() - started) * 1000, 1),
        )
        return response

    get_logger("app").info(
        "app.started",
        env=settings.app_env,
        provider=settings.provider,
        log_query_text=settings.log_query_text,
    )
    return app


app = create_app()
