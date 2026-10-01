"""Unit tests for application configuration using pydantic-settings."""

import os
import pytest
from pydantic import ValidationError


def test_settings_load_defaults():
    from backend.core.config import Settings

    settings = Settings()
    assert settings.app_env in ["production", "development", "test"]
    assert settings.app_host == "0.0.0.0"
    assert settings.app_port == 8000
    assert settings.postgres_port == 5432
    assert settings.postgres_db == "cadastre_db"
    assert settings.redis_port == 6379
    assert settings.minio_port == 9000
    assert settings.minio_bucket_raw == "cadastral-raw"
    assert settings.minio_bucket_harmonized == "cadastral-harmonized"


def test_deterministic_spatial_tolerances():
    from backend.core.config import Settings

    settings = Settings()
    # Overlap <= 2% threshold
    assert settings.max_overlap_percent_threshold == 2.0
    # Polsby-Popper compactness < 0.05
    assert settings.sliver_max_polsby_popper == 0.05
    # Sliver max area < 10 sqm
    assert settings.sliver_max_area_sqm == 10.0
    # Boundary gap tolerance 0.5 meters
    assert settings.boundary_gap_tolerance_meters == 0.5
    # ST_Snap tolerance 0.25 meters
    assert settings.snap_healing_tolerance_meters == 0.25
    # ST_SnapToGrid 1 millimeter (0.001 m)
    assert settings.grid_tolerance_meters == 0.001


def test_settings_env_override(monkeypatch):
    from backend.core.config import Settings

    monkeypatch.setenv("APP_PORT", "9090")
    monkeypatch.setenv("MAX_OVERLAP_PERCENT_THRESHOLD", "2.5")
    monkeypatch.setenv("LOG_LEVEL", "DEBUG")

    settings = Settings()
    assert settings.app_port == 9090
    assert settings.max_overlap_percent_threshold == 2.5
    assert settings.log_level == "DEBUG"


def test_get_settings_caching():
    from backend.core.config import get_settings

    s1 = get_settings()
    s2 = get_settings()
    assert s1 is s2
