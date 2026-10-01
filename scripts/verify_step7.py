"""Verification script for Step 7: MinIO initialization script and required buckets."""

from pathlib import Path
import sys
import yaml

COMPOSE_FILE = Path(__file__).resolve().parent.parent / "docker-compose.yml"
INIT_SH_FILE = Path(__file__).resolve().parent.parent / "docker" / "minio" / "init-buckets.sh"


def test_minio_buckets_initialization():
    # 1. Verify init-buckets.sh exists
    assert INIT_SH_FILE.exists(), f"Missing init-buckets.sh at {INIT_SH_FILE}"
    with open(INIT_SH_FILE, "r", encoding="utf-8") as f:
        sh_content = f.read()

    assert "cadastral-raw" in sh_content, "cadastral-raw missing in init-buckets.sh"
    assert "cadastral-harmonized" in sh_content, "cadastral-harmonized missing in init-buckets.sh"
    assert "/usr/bin/mc mb" in sh_content, "mc mb command missing in init-buckets.sh"

    # 2. Verify docker-compose.yml minio_init service
    assert COMPOSE_FILE.exists(), f"Missing docker-compose.yml at {COMPOSE_FILE}"
    with open(COMPOSE_FILE, "r", encoding="utf-8") as f:
        compose = yaml.safe_load(f)

    assert "services" in compose, "services missing from compose"
    assert "minio_init" in compose["services"], "minio_init service missing from compose services"

    init_svc = compose["services"]["minio_init"]
    assert "minio/mc" in init_svc.get("image", ""), "minio_init must use minio/mc image"

    depends_on = init_svc.get("depends_on", {})
    assert "minio" in depends_on, "minio_init must depend on minio"

    entrypoint = init_svc.get("entrypoint", "")
    assert "mc mb" in entrypoint, "mc mb missing in minio_init entrypoint"
    assert "MINIO_BUCKET_RAW" in entrypoint, "MINIO_BUCKET_RAW missing in minio_init entrypoint"
    assert "MINIO_BUCKET_HARMONIZED" in entrypoint, "MINIO_BUCKET_HARMONIZED missing in minio_init entrypoint"

    networks = init_svc.get("networks", [])
    assert "cadastre_net" in networks, "cadastre_net missing in minio_init networks"

    print("Step 7 verification passed: MinIO initialization script and bucket provisioning verified.")


if __name__ == "__main__":
    try:
        test_minio_buckets_initialization()
    except Exception as exc:
        print(f"Verification failed: {exc}", file=sys.stderr)
        sys.exit(1)
