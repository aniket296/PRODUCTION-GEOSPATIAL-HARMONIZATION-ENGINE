"""Verification script for Step 2: Docker Compose network cadastre_net and base service configuration."""

from pathlib import Path
import sys
import yaml

COMPOSE_FILE = Path(__file__).resolve().parent.parent / "docker-compose.yml"


def test_compose_configuration():
    assert COMPOSE_FILE.exists(), f"Missing docker-compose.yml at {COMPOSE_FILE}"

    with open(COMPOSE_FILE, "r", encoding="utf-8") as f:
        compose = yaml.safe_load(f)

    assert isinstance(compose, dict), "docker-compose.yml must be a valid YAML mapping"

    # 1. Verify cadastre_net network
    assert "networks" in compose, "Networks section missing from docker-compose.yml"
    assert "cadastre_net" in compose["networks"], "cadastre_net network missing"

    cadastre_net = compose["networks"]["cadastre_net"]
    assert cadastre_net.get("name") == "cadastre_net", "Network name must be 'cadastre_net'"
    assert cadastre_net.get("driver") == "bridge", "Network driver must be 'bridge'"

    ipam = cadastre_net.get("ipam", {})
    configs = ipam.get("config", [])
    assert any(c.get("subnet") == "172.28.0.0/16" for c in configs), "cadastre_net subnet 172.28.0.0/16 not found"

    # 2. Verify volumes
    assert "volumes" in compose, "Volumes section missing from docker-compose.yml"
    for vol in ["postgres_data", "redis_data", "minio_data"]:
        assert vol in compose["volumes"], f"Required persistent volume {vol} missing"

    # 3. Verify base service defaults
    assert "x-service-defaults" in compose, "Base service defaults anchor x-service-defaults missing"
    assert "cadastre_net" in compose["x-service-defaults"].get("networks", []), "x-service-defaults must include cadastre_net"

    assert "x-backend-common-env" in compose, "Base environment anchor x-backend-common-env missing"

    print("Step 2 verification passed: docker-compose.yml network cadastre_net and base configurations are valid.")


if __name__ == "__main__":
    try:
        test_compose_configuration()
    except Exception as exc:
        print(f"Verification failed: {exc}", file=sys.stderr)
        sys.exit(1)
