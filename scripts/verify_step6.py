"""Verification script for Step 6: MinIO service and local storage configuration."""

from pathlib import Path
import sys
import yaml

COMPOSE_FILE = Path(__file__).resolve().parent.parent / "docker-compose.yml"


def test_minio_service():
    assert COMPOSE_FILE.exists(), f"Missing docker-compose.yml at {COMPOSE_FILE}"
    with open(COMPOSE_FILE, "r", encoding="utf-8") as f:
        compose = yaml.safe_load(f)

    # 1. Check services map and minio service
    assert "services" in compose, "services missing from compose"
    assert "minio" in compose["services"], "minio service missing from compose services"

    minio_svc = compose["services"]["minio"]

    # 2. Check image
    image = minio_svc.get("image", "")
    assert "minio/minio" in image, f"Expected minio/minio image, got: {image}"

    # 3. Check ports: 9000 (API) and 9001 (Console)
    ports = minio_svc.get("ports", [])
    assert any("9000:9000" in str(p) for p in ports), f"Port 9000 missing in minio: {ports}"
    assert any("9001:9001" in str(p) for p in ports), f"Port 9001 missing in minio: {ports}"

    # 4. Check volumes: minio_data mounted to /data
    volumes = minio_svc.get("volumes", [])
    assert any("minio_data:/data" in str(v) for v in volumes), f"minio_data volume mount missing: {volumes}"

    # 5. Check environment variables
    env = minio_svc.get("environment", {})
    assert "MINIO_ROOT_USER" in env, "MINIO_ROOT_USER missing in minio environment"
    assert "MINIO_ROOT_PASSWORD" in env, "MINIO_ROOT_PASSWORD missing in minio environment"

    # 6. Check healthcheck
    healthcheck = minio_svc.get("healthcheck", {})
    assert "test" in healthcheck, "Healthcheck test missing in minio service"

    # 7. Check network attachment
    networks = minio_svc.get("networks", [])
    assert "cadastre_net" in networks, f"cadastre_net missing from minio networks: {networks}"

    print("Step 6 verification passed: MinIO service and local storage configuration verified.")


if __name__ == "__main__":
    try:
        test_minio_service()
    except Exception as exc:
        print(f"Verification failed: {exc}", file=sys.stderr)
        sys.exit(1)
