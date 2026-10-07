"""API v1 master router."""

from __future__ import annotations

from fastapi import APIRouter

from backend.api.v1.endpoints.layers import router as layers_router

api_v1_router = APIRouter(prefix="/api/v1")
api_v1_router.include_router(layers_router)

