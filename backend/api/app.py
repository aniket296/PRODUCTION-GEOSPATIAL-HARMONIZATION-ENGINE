"""FastAPI application factory and lifecycle management."""

from __future__ import annotations

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from backend.api.v1.router import api_v1_router
from backend.core.config import get_settings


def create_app() -> FastAPI:
    """Construct and configure the production Cadastral Harmonization Engine FastAPI app."""
    settings = get_settings()

    app = FastAPI(
        title="Cadastral Harmonization Engine API",
        description="SIH26013: Multi-Source Geospatial Data Harmonization Engine for Urban Land Records",
        version="0.1.0",
        docs_url="/docs" if settings.debug or settings.app_env != "production" else "/docs",
        redoc_url="/redoc" if settings.debug or settings.app_env != "production" else "/redoc",
    )

    # CORS configuration
    app.add_middleware(
        CORSMiddleware,
        allow_origins=settings.cors_origins,
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )

    # Include master v1 router
    app.include_router(api_v1_router)

    @app.get("/health", tags=["Health"])
    @app.get("/api/v1/health", tags=["Health"])
    async def health_check():
        return {
            "status": "healthy",
            "service": "cadastral_harmonization_engine",
            "version": "0.1.0",
        }

    return app


app = create_app()

