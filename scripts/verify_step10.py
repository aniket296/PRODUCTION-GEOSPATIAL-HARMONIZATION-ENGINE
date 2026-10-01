"""Verification script for Step 10: Celery worker service."""

from pathlib import Path
import sys
import yaml

COMPOSE_FILE = Path(__file__).resolve().parent.parent / "docker-compose.yml"


def test_celery_worker_service():
    assert COMPOSE_FILE.exists(), f"Missing docker-compose.yml at {COMPOSE_FILE}"
    with open(COMPOSE_FILE, "r", encoding="utf-8") as f:
        compose = yaml.safe_load(f)

    assert "services" in compose, "services section missing in compose"
    assert "worker" in compose["services"], "worker service missing in compose services"

    worker = compose["services"]["worker"]

    # 1. Build context and Dockerfile
    build_cfg = worker.get("build", {})
    assert "docker/backend/Dockerfile" in build_cfg.get("dockerfile", ""), "worker Dockerfile must be docker/backend/Dockerfile"
    assert build_cfg.get("target") == "runtime", "worker build target must be runtime"

    # 2. User configuration
    assert str(worker.get("user")) == "1000:1000", "worker must run as non-root user 1000:1000"

    # 3. Command
    cmd = worker.get("command", "")
    assert "celery" in cmd and "worker" in cmd, f"worker command must be celery worker, got: {cmd}"

    # 4. Dependencies
    deps = worker.get("depends_on", {})
    assert "redis" in deps, "worker must depend on redis"
    assert "postgres" in deps, "worker must depend on postgres"
    assert "minio" in deps, "worker must depend on minio"

    # 5. Network
    networks = worker.get("networks", [])
    assert "cadastre_net" in networks, "worker must be attached to cadastre_net"

    print("Step 10 verification passed: Celery worker service configuration verified.")


if __name__ == "__main__":
    try:
        test_celery_worker_service()
    except Exception as exc:
        print(f"Verification failed: {exc}", file=sys.stderr)
        sys.exit(1)
