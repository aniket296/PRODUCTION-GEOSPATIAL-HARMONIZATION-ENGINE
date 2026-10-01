"""Verification script for Step 3: Add PostgreSQL 16 service."""

from pathlib import Path
import sys
import yaml

COMPOSE_FILE = Path(__file__).resolve().parent.parent / "docker-compose.yml"


def test_postgres_service():
    assert COMPOSE_FILE.exists(), f"Missing docker-compose.yml at {COMPOSE_FILE}"

    with open(COMPOSE_FILE, "r", encoding="utf-8") as f:
        compose = yaml.safe_load(f)

    assert "services" in compose, "services missing from compose"
    assert "postgres" in compose["services"], "postgres service missing from services"

    pg = compose["services"]["postgres"]

    # 1. Verify image is Postgres 16 with PostGIS
    image = pg.get("image", "")
    assert "16" in image, f"Expected PostgreSQL 16 image, got {image}"

    # 2. Verify ports
    ports = pg.get("ports", [])
    assert any("5432:5432" in str(p) for p in ports), f"Port 5432 mapping missing: {ports}"

    # 3. Verify volumes
    volumes = pg.get("volumes", [])
    assert any("postgres_data:/var/lib/postgresql/data" in str(v) for v in volumes), f"postgres_data mount missing: {volumes}"

    # 4. Verify environment variables
    env = pg.get("environment", {})
    assert "POSTGRES_USER" in env, "POSTGRES_USER missing from environment"
    assert "POSTGRES_PASSWORD" in env, "POSTGRES_PASSWORD missing from environment"
    assert "POSTGRES_DB" in env, "POSTGRES_DB missing from environment"

    # 5. Verify healthcheck
    healthcheck = pg.get("healthcheck", {})
    assert "test" in healthcheck, "Healthcheck test command missing"
    test_cmd = " ".join(healthcheck["test"]) if isinstance(healthcheck["test"], list) else str(healthcheck["test"])
    assert "pg_isready" in test_cmd, f"Expected pg_isready in healthcheck, got: {test_cmd}"

    # 6. Verify network connectivity
    networks = pg.get("networks", [])
    assert "cadastre_net" in networks, f"cadastre_net missing from postgres networks: {networks}"

    print("Step 3 verification passed: PostgreSQL 16 service is valid and configured with persistent volume and healthcheck.")


if __name__ == "__main__":
    try:
        test_postgres_service()
    except Exception as exc:
        print(f"Verification failed: {exc}", file=sys.stderr)
        sys.exit(1)
