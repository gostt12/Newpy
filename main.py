"""
main.py
────────
FastAPI application factory and entry point.

Lifecycle
─────────
  startup  → initialise DB tables (dev only), start APScheduler
  shutdown → stop scheduler, dispose DB pool
"""

from contextlib import asynccontextmanager

import uvicorn
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.middleware.trustedhost import TrustedHostMiddleware

from api import api_router, webhook_router
from config.database import close_db, init_db
from config.settings import get_settings
from services.scheduler import start_scheduler, stop_scheduler
from utils.logger import get_logger

logger = get_logger("main")
settings = get_settings()


# ──────────────────────────────────────────────────────────────────────────────
# Lifespan
# ──────────────────────────────────────────────────────────────────────────────

@asynccontextmanager
async def lifespan(app: FastAPI):
    logger.info("starting_up", env=settings.app_env)

    # In production use Alembic migrations instead of auto-create
    if not settings.is_production:
        await init_db()

    start_scheduler()
    logger.info("ready")

    yield

    stop_scheduler()
    await close_db()
    logger.info("shut_down")


# ──────────────────────────────────────────────────────────────────────────────
# App factory
# ──────────────────────────────────────────────────────────────────────────────

def create_app() -> FastAPI:
    app = FastAPI(
        title="Bot Manager — Telegram Escrow & Marketplace",
        version="1.0.0",
        docs_url="/docs" if not settings.is_production else None,
        redoc_url="/redoc" if not settings.is_production else None,
        lifespan=lifespan,
    )

    # ── CORS ──────────────────────────────────────────────────────────────────
    app.add_middleware(
        CORSMiddleware,
        allow_origins=settings.get_allowed_origins(),
        allow_credentials=True,
        allow_methods=["GET", "POST", "PUT", "PATCH", "DELETE"],
        allow_headers=["*"],
    )

    # ── Trusted hosts (prod) ──────────────────────────────────────────────────
    if settings.is_production:
        app.add_middleware(
            TrustedHostMiddleware,
            allowed_hosts=["yourdomain.com", "*.yourdomain.com"],
        )

    # ── Routers ───────────────────────────────────────────────────────────────
    app.include_router(api_router,     prefix="/api/v1")
    app.include_router(webhook_router, prefix="/api/v1")

    # ── Health check ──────────────────────────────────────────────────────────
    @app.get("/health", include_in_schema=False)
    async def health():
        return {"status": "ok", "env": settings.app_env}

    return app


app = create_app()


# ──────────────────────────────────────────────────────────────────────────────
# Dev runner
# ──────────────────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    uvicorn.run(
        "main:app",
        host="0.0.0.0",
        port=8000,
        reload=not settings.is_production,
        log_level="debug" if settings.debug else "info",
    )
