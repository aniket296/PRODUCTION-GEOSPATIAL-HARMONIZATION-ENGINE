"""Tests for backend/pyproject.toml metadata and dependency specifications."""

from pathlib import Path
import tomllib
import pytest

PYPROJECT_PATH = Path(__file__).resolve().parent.parent / "backend" / "pyproject.toml"


@pytest.fixture(scope="module")
def pyproject_data():
    assert PYPROJECT_PATH.exists(), f"Missing pyproject.toml at {PYPROJECT_PATH}"
    with open(PYPROJECT_PATH, "rb") as f:
        return tomllib.load(f)


def test_project_metadata(pyproject_data):
    project = pyproject_data.get("project", {})
    assert project.get("name") == "cadastral-harmonization-engine"
    assert project.get("version") == "0.1.0"
    assert "SIH26013" in project.get("description", "")
    assert project.get("requires-python") == ">=3.12"


def test_approved_technology_stack_dependencies(pyproject_data):
    project = pyproject_data.get("project", {})
    deps = project.get("dependencies", [])
    dep_names = [dep.split(">=")[0].split("[")[0].strip().lower() for dep in deps]

    # Required backend stack
    required_backend = [
        "fastapi",
        "pydantic",
        "pydantic-settings",
        "sqlalchemy",
        "asyncpg",
        "alembic",
        "geoalchemy2",
    ]
    for req in required_backend:
        assert req in dep_names, f"Required backend dependency '{req}' missing from dependencies"

    # Required geospatial stack
    required_geospatial = [
        "shapely",
        "pyproj",
        "geopandas",
        "flatgeobuf",
        "ezdxf",
    ]
    for req in required_geospatial:
        assert req in dep_names, f"Required geospatial dependency '{req}' missing from dependencies"

    # Required distributed queue and object storage
    required_infra = [
        "celery",
        "redis",
        "minio",
    ]
    for req in required_infra:
        assert req in dep_names, f"Required infra dependency '{req}' missing from dependencies"


def test_optional_dev_dependencies(pyproject_data):
    opt_deps = pyproject_data.get("project", {}).get("optional-dependencies", {})
    assert "dev" in opt_deps, "dev dependencies missing in optional-dependencies"
    dev_deps = [d.split(">=")[0].strip().lower() for d in opt_deps["dev"]]

    for req_dev in ["pytest", "pytest-asyncio", "pytest-cov", "ruff", "mypy"]:
        assert req_dev in dev_deps, f"Development dependency '{req_dev}' missing from dev dependencies"


def test_build_system_configuration(pyproject_data):
    build_sys = pyproject_data.get("build-system", {})
    assert "requires" in build_sys
    assert "build-backend" in build_sys
