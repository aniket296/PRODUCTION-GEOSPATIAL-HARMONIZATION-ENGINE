"""Verification script for Step 5: Redis 7 Alpine service with memory configuration."""

from pathlib import Path
import sys
import yaml

COMPOSE_FILE = Path(__file__).resolve().parent.parent / "docker-compose.yml"
REDIS_CONF_FILE = Path(__file__).resolve().parent.parent / "docker" / "redis" / "redis.conf"


def test_redis_service():
    # 1. Verify redis.conf configuration
    assert REDIS_CONF_FILE.exists(), f"Missing redis.conf at {REDIS_CONF_FILE}"
    with open(REDIS_CONF_FILE, "r", encoding="utf-8") as f:
        conf_text = f.read()

    assert "maxmemory 512mb" in conf_text, "maxmemory 512mb missing from redis.conf"
    assert "maxmemory-policy noeviction" in conf_text, "maxmemory-policy noeviction missing from redis.conf"
    assert "appendonly yes" in conf_text, "appendonly yes missing from redis.conf"

    # 2. Verify docker-compose.yml service definition
    assert COMPOSE_FILE.exists(), f"Missing docker-compose.yml at {COMPOSE_FILE}"
    with open(COMPOSE_FILE, "r", encoding="utf-8") as f:
        compose = yaml.safe_load(f)

    assert "redis" in compose.get("services", {}), "redis service missing from compose services"
    redis_svc = compose["services"]["redis"]

    # Image check
    image = redis_svc.get("image", "")
    assert "redis:7-alpine" in image or ("redis:7" in image and "alpine" in image), (
        f"Expected Redis 7 Alpine image, got {image}"
    )

    # Port check
    ports = redis_svc.get("ports", [])
    assert any("6379:6379" in str(p) for p in ports), f"Port 6379 mapping missing: {ports}"

    # Volumes check
    volumes = redis_svc.get("volumes", [])
    assert any("redis_data:/data" in str(v) for v in volumes), f"redis_data volume missing: {volumes}"
    assert any("redis.conf" in str(v) for v in volumes), f"redis.conf mount missing: {volumes}"

    # Healthcheck check
    healthcheck = redis_svc.get("healthcheck", {})
    assert "test" in healthcheck, "Healthcheck test missing in redis service"
    test_cmd = " ".join(healthcheck["test"]) if isinstance(healthcheck["test"], list) else str(healthcheck["test"])
    assert "redis-cli" in test_cmd and "ping" in test_cmd, f"Expected redis-cli ping in healthcheck, got: {test_cmd}"

    # Network check
    networks = redis_svc.get("networks", [])
    assert "cadastre_net" in networks, f"cadastre_net missing from redis networks: {networks}"

    print("Step 5 verification passed: Redis 7 Alpine service with memory configuration verified.")


if __name__ == "__main__":
    try:
        test_redis_service()
    except Exception as exc:
        print(f"Verification failed: {exc}", file=sys.stderr)
        sys.exit(1)
