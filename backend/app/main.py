"""FastAPI entrypoint; business routers are added as their ports land."""

from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from app.api.analysis import router as analysis_router
from app.api.analysis_runtime import install_analysis_runtime
from app.api.analysis_status import router as analysis_status_router
from app.api.errors import install_exception_handlers
from app.api.evaluation import router as evaluation_router
from app.api.failed_tasks import router as failed_tasks_router
from app.api.health import router as health_router
from app.api.media import router as media_router
from app.api.processing import router as processing_router
from app.api.user import router as user_router
from app.config import Settings


@asynccontextmanager
async def lifespan(app: FastAPI):
    install_analysis_runtime(app)
    try:
        yield
    finally:
        if hasattr(app.state, "transcription_executor"):
            app.state.transcription_executor.shutdown()
        if hasattr(app.state, "transcription_http_client"):
            app.state.transcription_http_client.close()
        app.state.analysis_runtime.close()
        if hasattr(app.state, "task_event_service"):
            await app.state.task_event_service.close()
        if hasattr(app.state, "redis_client"):
            app.state.redis_client.close()
        if hasattr(app.state, "db_engine"):
            app.state.db_engine.dispose()


def create_app(settings: Settings | None = None) -> FastAPI:
    settings = settings or Settings()
    app = FastAPI(title="DOVideo-AI Python Backend", lifespan=lifespan)
    app.state.settings = settings
    app.add_middleware(
        CORSMiddleware,
        allow_origins=settings.cors_origins,
        allow_credentials=True,
        allow_methods=["GET", "POST", "DELETE", "OPTIONS"],
        allow_headers=["*"],
    )
    install_exception_handlers(app)
    app.include_router(health_router)
    app.include_router(user_router)
    app.include_router(media_router)
    app.include_router(analysis_router)
    app.include_router(analysis_status_router)
    app.include_router(evaluation_router)
    app.include_router(processing_router)
    app.include_router(failed_tasks_router)
    return app


app = create_app()
