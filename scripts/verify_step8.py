"""Verification script for Step 8: Multi-stage backend Dockerfile with GDAL, GEOS, and PROJ."""

from pathlib import Path
import sys
import yaml

COMPOSE_FILE = Path(__file__).resolve().parent.parent / "docker-compose.yml"
DOCKERFILE = Path(__file__).resolve().parent.parent / "docker" / "backend" / "Dockerfile"


def test_dockerfile_and_compose():
    # 1. Verify Dockerfile exists
    assert DOCKERFILE.exists(), f"Missing Dockerfile at {DOCKERFILE}"
    with open(DOCKERFILE, "r", encoding="utf-8") as f:
        dockerfile_content = f.read()

    # 2. Verify multi-stage build structure
    assert "AS builder" in dockerfile_content, "Missing builder stage in Dockerfile"
    assert "AS runtime" in dockerfile_content, "Missing runtime stage in Dockerfile"
    assert "python:3.12-slim-bookworm" in dockerfile_content, "Base image must be python:3.12-slim-bookworm"

    # 3. Verify GDAL, GEOS, PROJ dependencies
    assert "libgdal-dev" in dockerfile_content or "libgdal" in dockerfile_content, "GDAL missing in Dockerfile"
    assert "libgeos-dev" in dockerfile_content or "libgeos" in dockerfile_content, "GEOS missing in Dockerfile"
    assert "libproj-dev" in dockerfile_content or "libproj" in dockerfile_content, "PROJ missing in Dockerfile"
    assert "PROJ_LIB" in dockerfile_content, "PROJ_LIB environment variable missing"
    assert "GDAL_DATA" in dockerfile_content, "GDAL_DATA environment variable missing"

    # 4. Verify virtual environment
    assert "/opt/venv" in dockerfile_content, "Virtualenv path /opt/venv missing"

    # 5. Verify docker-compose backend service
    assert COMPOSE_FILE.exists(), f"Missing docker-compose.yml at {COMPOSE_FILE}"
    with open(COMPOSE_FILE, "r", encoding="utf-8") as f:
        compose = yaml.safe_load(f)

    assert "services" in compose, "services missing from compose"
    assert "backend" in compose["services"], "backend service missing from compose services"

    backend = compose["services"]["backend"]
    build_cfg = backend.get("build", {})
    assert "docker/backend/Dockerfile" in build_cfg.get("dockerfile", ""), "backend must point to docker/backend/Dockerfile"
    assert any("8000:8000" in str(p) for p in backend.get("ports", [])), "Port 8000 mapping missing in backend service"

    print("Step 8 verification passed: Multi-stage backend Dockerfile with GDAL, GEOS, and PROJ verified.")


if __name__ == "__main__":
    try:
        test_dockerfile_and_compose()
    except Exception as exc:
        print(f"Verification failed: {exc}", file=sys.stderr)
        sys.exit(1)
