"""Application configuration management using pydantic-settings.

Centralizes all environment parameters, infrastructure connection strings,
and deterministic geospatial topology thresholds.
"""

from functools import lru_cache
from typing import Any, List, Union
from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """Production application settings with deterministic geospatial defaults."""

    # Application Core
    app_env: str = Field(default="production", alias="APP_ENV")
    app_host: str = Field(default="0.0.0.0", alias="APP_HOST")
    app_port: int = Field(default=8000, alias="APP_PORT")
    log_level: str = Field(default="INFO", alias="LOG_LEVEL")
    debug: bool = Field(default=False, alias="DEBUG")

    @field_validator("debug", mode="before")
    @classmethod
    def parse_debug(cls, v: Any) -> bool:
        """Parse boolean debug flag, handling strings like 'release', 'dev', etc."""
        if isinstance(v, str):
            clean = v.strip().lower()
            if clean in ("release", "prod", "production", "false", "0", "no", "off"):
                return False
            if clean in ("debug", "dev", "development", "true", "1", "yes", "on"):
                return True
        return bool(v)

    secret_key: str = Field(
        default="cadastre-secret-key-production-change-in-deploy-env",
        alias="SECRET_KEY",
    )
    cors_origins: List[str] = Field(
        default=[
            "http://localhost:3000",
            "http://localhost:5173",
            "http://127.0.0.1:3000",
            "http://127.0.0.1:5173",
        ],
        alias="CORS_ORIGINS",
    )
    max_upload_size_bytes: int = Field(default=104857600, alias="MAX_UPLOAD_SIZE_BYTES")

    # PostgreSQL / PostGIS Database
    postgres_user: str = Field(default="cadastre_user", alias="POSTGRES_USER")
    postgres_password: str = Field(default="cadastre_secure_password", alias="POSTGRES_PASSWORD")
    postgres_db: str = Field(default="cadastre_db", alias="POSTGRES_DB")
    postgres_host: str = Field(default="postgres", alias="POSTGRES_HOST")
    postgres_port: int = Field(default=5432, alias="POSTGRES_PORT")
    database_url: str = Field(
        default="postgresql+asyncpg://cadastre_user:cadastre_secure_password@postgres:5432/cadastre_db",
        alias="DATABASE_URL",
    )
    database_sync_url: str = Field(
        default="postgresql+psycopg2://cadastre_user:cadastre_secure_password@postgres:5432/cadastre_db",
        alias="DATABASE_SYNC_URL",
    )

    # Redis & Distributed Worker
    redis_host: str = Field(default="redis", alias="REDIS_HOST")
    redis_port: int = Field(default=6379, alias="REDIS_PORT")
    redis_url: str = Field(default="redis://redis:6379/0", alias="REDIS_URL")
    celery_broker_url: str = Field(default="redis://redis:6379/1", alias="CELERY_BROKER_URL")
    celery_result_backend: str = Field(default="redis://redis:6379/2", alias="CELERY_RESULT_BACKEND")

    # MinIO S3-Compatible Object Storage
    minio_endpoint: str = Field(default="minio:9000", alias="MINIO_ENDPOINT")
    minio_port: int = Field(default=9000, alias="MINIO_PORT")
    minio_console_port: int = Field(default=9001, alias="MINIO_CONSOLE_PORT")
    minio_secure: bool = Field(default=False, alias="MINIO_SECURE")
    minio_root_user: str = Field(default="minio_admin", alias="MINIO_ROOT_USER")
    minio_root_password: str = Field(default="minio_secure_password", alias="MINIO_ROOT_PASSWORD")
    minio_bucket_raw: str = Field(default="cadastral-raw", alias="MINIO_BUCKET_RAW")
    minio_bucket_harmonized: str = Field(default="cadastral-harmonized", alias="MINIO_BUCKET_HARMONIZED")

    # Local / Offline-First Tile Server
    local_tile_server_url: str = Field(
        default="http://localhost:8080/tiles/{z}/{x}/{y}.pbf",
        alias="LOCAL_TILE_SERVER_URL",
    )

    # Deterministic Cadastral Topology Tolerances (Sections 15, 16, 17, 18, 19)
    max_overlap_percent_threshold: float = Field(
        default=2.0,
        alias="MAX_OVERLAP_PERCENT_THRESHOLD",
        description="Percentage threshold below or equal to which overlaps are AUTO_RESOLVABLE; above is CRITICAL_DISPUTE",
    )
    sliver_max_area_sqm: float = Field(
        default=10.0,
        alias="SLIVER_MAX_AREA_SQM",
        description="Metric polygon area in square meters below which a polygon is classified as sliver candidate",
    )
    sliver_max_polsby_popper: float = Field(
        default=0.05,
        alias="SLIVER_MAX_POLSBY_POPPER",
        description="Polsby-Popper compactness (4πA/P²) below which a polygon is classified as sliver",
    )
    boundary_gap_tolerance_meters: float = Field(
        default=0.5,
        alias="BOUNDARY_GAP_TOLERANCE_METERS",
        description="Metric detection tolerance for gaps between adjacent parcels in meters",
    )
    snap_healing_tolerance_meters: float = Field(
        default=0.25,
        alias="SNAP_HEALING_TOLERANCE_METERS",
        description="Metric ST_Snap boundary healing tolerance in meters",
    )
    grid_tolerance_meters: float = Field(
        default=0.001,
        alias="GRID_TOLERANCE_METERS",
        description="ST_SnapToGrid normalization precision (1 millimeter = 0.001m)",
    )

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
        populate_by_name=True,
    )


@lru_cache
def get_settings() -> Settings:
    """Return cached application settings instance."""
    return Settings()
