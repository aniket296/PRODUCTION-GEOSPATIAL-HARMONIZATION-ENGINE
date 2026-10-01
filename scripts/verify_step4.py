"""Verification script for Step 4: PostGIS, postgis_topology, and pgcrypto initialization."""

from pathlib import Path
import sys
import yaml

COMPOSE_FILE = Path(__file__).resolve().parent.parent / "docker-compose.yml"
INIT_SQL_FILE = Path(__file__).resolve().parent.parent / "docker" / "postgres" / "01-init-postgis.sql"


def test_postgis_initialization():
    # 1. Verify init SQL script exists
    assert INIT_SQL_FILE.exists(), f"Missing init SQL script at {INIT_SQL_FILE}"

    with open(INIT_SQL_FILE, "r", encoding="utf-8") as f:
        sql_content = f.read()

    # 2. Verify required extensions are declared
    required_extensions = ["postgis", "postgis_topology", "pgcrypto"]
    for ext in required_extensions:
        assert f'CREATE EXTENSION IF NOT EXISTS "{ext}"' in sql_content, (
            f"Extension {ext} not found in {INIT_SQL_FILE}"
        )

    # 3. Verify topology schema permissions
    assert "SCHEMA topology" in sql_content, "Topology schema configuration missing"
    assert "cadastre_user" in sql_content, "cadastre_user grants missing from SQL script"

    # 4. Verify docker-compose mounts the initialization script
    assert COMPOSE_FILE.exists(), f"Missing docker-compose.yml at {COMPOSE_FILE}"
    with open(COMPOSE_FILE, "r", encoding="utf-8") as f:
        compose = yaml.safe_load(f)

    pg = compose.get("services", {}).get("postgres", {})
    volumes = pg.get("volumes", [])
    expected_mount = "./docker/postgres:/docker-entrypoint-initdb.d:ro"
    assert any(expected_mount in str(v) for v in volumes), (
        f"Initialization script mount {expected_mount} missing in postgres volumes: {volumes}"
    )

    print("Step 4 verification passed: PostGIS, postgis_topology, and pgcrypto initialization configured.")


if __name__ == "__main__":
    try:
        test_postgis_initialization()
    except Exception as exc:
        print(f"Verification failed: {exc}", file=sys.stderr)
        sys.exit(1)
