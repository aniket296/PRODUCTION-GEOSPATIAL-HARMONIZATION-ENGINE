"""Docker service smoke tests covering all five required services:
FastAPI, PostgreSQL/PostGIS, Redis, Celery worker, and MinIO.
"""

from pathlib import Path
import pytest
import yaml

PROJECT_ROOT = Path(__file__).resolve().parent.parent
COMPOSE_FILE = PROJECT_ROOT / "docker-compose.yml"
DOCKERFILE = PROJECT_ROOT / "docker" / "backend" / "Dockerfile"
POSTGRES_INIT_SQL = PROJECT_ROOT / "docker" / "postgres" / "01-init-postgis.sql"
REDIS_CONF = PROJECT_ROOT / "docker" / "redis" / "redis.conf"
MINIO_INIT_SH = PROJECT_ROOT / "docker" / "minio" / "init-buckets.sh"


@pytest.fixture(scope="module")
def compose_config():
    assert COMPOSE_FILE.exists(), f"docker-compose.yml not found at {COMPOSE_FILE}"
    with open(COMPOSE_FILE, "r", encoding="utf-8") as f:
        data = yaml.safe_load(f)
    assert isinstance(data, dict), "docker-compose.yml must be valid YAML dictionary"
    return data


class TestDockerComposeNetworkAndStorage:
    """Verifies the base cadastre_net bridge network and persistent volumes."""

    def test_cadastre_network_smoke(self, compose_config):
        networks = compose_config.get("networks", {})
        assert "cadastre_net" in networks, "cadastre_net missing from networks"
        cadastre_net = networks["cadastre_net"]
        assert cadastre_net.get("name") == "cadastre_net"
        assert cadastre_net.get("driver") == "bridge"
        ipam = cadastre_net.get("ipam", {})
        configs = ipam.get("config", [])
        assert any(c.get("subnet") == "172.28.0.0/16" for c in configs)

    def test_persistent_volumes_smoke(self, compose_config):
        volumes = compose_config.get("volumes", {})
        for vol_name in ["postgres_data", "redis_data", "minio_data"]:
            assert vol_name in volumes, f"Required persistent volume {vol_name} missing"


class TestFastAPIServiceSmoke:
    """Smoke test for FastAPI backend service."""

    def test_fastapi_backend_service(self, compose_config):
        services = compose_config.get("services", {})
        assert "backend" in services, "FastAPI backend service missing"
        backend = services["backend"]

        # Image build
        build_cfg = backend.get("build", {})
        assert "docker/backend/Dockerfile" in build_cfg.get("dockerfile", "")
        assert build_cfg.get("target") == "runtime"

        # Non-root user
        assert str(backend.get("user")) == "1000:1000"

        # Port mapping
        ports = backend.get("ports", [])
        assert any("8000:8000" in str(p) for p in ports)

        # Network attachment
        networks = backend.get("networks", [])
        assert "cadastre_net" in networks

        # Upstream service dependencies
        deps = backend.get("depends_on", {})
        for required_dep in ["postgres", "redis", "minio"]:
            assert required_dep in deps, f"FastAPI must depend on {required_dep}"
            assert deps[required_dep].get("condition") == "service_healthy"


class TestPostgreSQLPostGISServiceSmoke:
    """Smoke test for PostgreSQL 16 + PostGIS 3.4 database service."""

    def test_postgres_service(self, compose_config):
        services = compose_config.get("services", {})
        assert "postgres" in services, "PostgreSQL service missing"
        pg = services["postgres"]

        # Image specification
        image = pg.get("image", "")
        assert "postgis" in image and "16" in image

        # Port mapping
        ports = pg.get("ports", [])
        assert any("5432:5432" in str(p) for p in ports)

        # Persistent storage mount
        volumes = pg.get("volumes", [])
        assert any("postgres_data:/var/lib/postgresql/data" in str(v) for v in volumes)
        assert any("docker/postgres:/docker-entrypoint-initdb.d" in str(v) for v in volumes)

        # Healthcheck
        health = pg.get("healthcheck", {})
        assert "test" in health
        test_cmd = " ".join(health["test"]) if isinstance(health["test"], list) else str(health["test"])
        assert "pg_isready" in test_cmd

        # Network
        networks = pg.get("networks", [])
        assert "cadastre_net" in networks

    def test_postgis_init_sql(self):
        assert POSTGRES_INIT_SQL.exists()
        with open(POSTGRES_INIT_SQL, "r", encoding="utf-8") as f:
            sql = f.read()
        assert 'CREATE EXTENSION IF NOT EXISTS "postgis"' in sql
        assert 'CREATE EXTENSION IF NOT EXISTS "postgis_topology"' in sql
        assert 'CREATE EXTENSION IF NOT EXISTS "pgcrypto"' in sql


class TestRedisServiceSmoke:
    """Smoke test for Redis 7 Alpine cache and message broker."""

    def test_redis_service(self, compose_config):
        services = compose_config.get("services", {})
        assert "redis" in services, "Redis service missing"
        redis_svc = services["redis"]

        # Image specification
        image = redis_svc.get("image", "")
        assert "redis:7-alpine" in image

        # Port mapping
        ports = redis_svc.get("ports", [])
        assert any("6379:6379" in str(p) for p in ports)

        # Persistence volume and config mount
        volumes = redis_svc.get("volumes", [])
        assert any("redis_data:/data" in str(v) for v in volumes)
        assert any("redis.conf" in str(v) for v in volumes)

        # Healthcheck
        health = redis_svc.get("healthcheck", {})
        assert "test" in health
        test_cmd = " ".join(health["test"]) if isinstance(health["test"], list) else str(health["test"])
        assert "redis-cli" in test_cmd and "ping" in test_cmd

        # Network
        networks = redis_svc.get("networks", [])
        assert "cadastre_net" in networks

    def test_redis_memory_config(self):
        assert REDIS_CONF.exists()
        with open(REDIS_CONF, "r", encoding="utf-8") as f:
            conf = f.read()
        assert "maxmemory 512mb" in conf
        assert "maxmemory-policy noeviction" in conf
        assert "appendonly yes" in conf


class TestCeleryWorkerServiceSmoke:
    """Smoke test for Celery distributed worker service."""

    def test_celery_worker_service(self, compose_config):
        services = compose_config.get("services", {})
        assert "worker" in services, "Celery worker service missing"
        worker = services["worker"]

        # Runtime target and non-root user
        build_cfg = worker.get("build", {})
        assert build_cfg.get("target") == "runtime"
        assert str(worker.get("user")) == "1000:1000"

        # Celery command
        cmd = worker.get("command", "")
        assert "celery" in cmd and "worker" in cmd

        # Upstream dependencies
        deps = worker.get("depends_on", {})
        for upstream in ["redis", "postgres", "minio"]:
            assert upstream in deps
            assert deps[upstream].get("condition") == "service_healthy"

        # Network
        networks = worker.get("networks", [])
        assert "cadastre_net" in networks


class TestMinIOServiceSmoke:
    """Smoke test for MinIO S3 object storage and bucket initialization."""

    def test_minio_service(self, compose_config):
        services = compose_config.get("services", {})
        assert "minio" in services, "MinIO service missing"
        minio_svc = services["minio"]

        # Image specification
        assert "minio/minio" in minio_svc.get("image", "")

        # Ports (API 9000, Web Console 9001)
        ports = minio_svc.get("ports", [])
        assert any("9000:9000" in str(p) for p in ports)
        assert any("9001:9001" in str(p) for p in ports)

        # Persistent volume
        volumes = minio_svc.get("volumes", [])
        assert any("minio_data:/data" in str(v) for v in volumes)

        # Healthcheck
        health = minio_svc.get("healthcheck", {})
        assert "test" in health

        # Network
        networks = minio_svc.get("networks", [])
        assert "cadastre_net" in networks

    def test_minio_bucket_provisioning(self, compose_config):
        services = compose_config.get("services", {})
        assert "minio_init" in services, "minio_init service missing"
        minio_init = services["minio_init"]

        assert "minio/mc" in minio_init.get("image", "")
        assert "minio" in minio_init.get("depends_on", {})

        entrypoint = minio_init.get("entrypoint", "")
        assert "cadastral-raw" in entrypoint
        assert "cadastral-harmonized" in entrypoint

        assert MINIO_INIT_SH.exists()
        with open(MINIO_INIT_SH, "r", encoding="utf-8") as f:
            sh = f.read()
        assert "cadastral-raw" in sh
        assert "cadastral-harmonized" in sh
