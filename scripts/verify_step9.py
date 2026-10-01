"""Verification script for Step 9: Non-root cadastre_user and Python virtual environment."""

from pathlib import Path
import sys
import yaml

COMPOSE_FILE = Path(__file__).resolve().parent.parent / "docker-compose.yml"
DOCKERFILE = Path(__file__).resolve().parent.parent / "docker" / "backend" / "Dockerfile"


def test_non_root_user_and_venv():
    # 1. Verify Dockerfile non-root configuration
    assert DOCKERFILE.exists(), f"Missing Dockerfile at {DOCKERFILE}"
    with open(DOCKERFILE, "r", encoding="utf-8") as f:
        content = f.read()

    assert "groupadd -g 1000 cadastre_user" in content or "cadastre_user" in content, (
        "cadastre_user group creation missing in Dockerfile"
    )
    assert "useradd -u 1000" in content, "cadastre_user UID 1000 missing in Dockerfile"
    assert "USER cadastre_user" in content, "USER cadastre_user switch missing in Dockerfile"
    assert "chown -R cadastre_user:cadastre_user" in content, "Ownership change for /opt/venv /app missing"
    assert "VIRTUAL_ENV=/opt/venv" in content, "VIRTUAL_ENV path missing in Dockerfile"
    assert '/opt/venv/bin:$PATH' in content, "Virtual environment PATH missing in Dockerfile"

    # 2. Verify docker-compose.yml backend service user
    assert COMPOSE_FILE.exists(), f"Missing docker-compose.yml at {COMPOSE_FILE}"
    with open(COMPOSE_FILE, "r", encoding="utf-8") as f:
        compose = yaml.safe_load(f)

    backend = compose.get("services", {}).get("backend", {})
    user = str(backend.get("user", ""))
    assert user in ["1000:1000", "cadastre_user"], f"Expected non-root user for backend, got: {user}"

    print("Step 9 verification passed: Non-root cadastre_user and virtual environment configured.")


if __name__ == "__main__":
    try:
        test_non_root_user_and_venv()
    except Exception as exc:
        print(f"Verification failed: {exc}", file=sys.stderr)
        sys.exit(1)
