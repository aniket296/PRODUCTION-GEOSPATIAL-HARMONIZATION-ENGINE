"""Tests for Alembic configuration and asynchronous migration environment."""

from pathlib import Path
from alembic.config import Config
from alembic.script import ScriptDirectory

REPO_ROOT = Path(__file__).resolve().parent.parent


def test_alembic_ini_exists_and_valid():
    ini_path = REPO_ROOT / "alembic.ini"
    assert ini_path.exists(), "alembic.ini must exist at repository root"

    config = Config(str(ini_path))
    script_loc = config.get_main_option("script_location")
    assert script_loc is not None
    assert "backend/alembic" in script_loc


def test_alembic_script_directory_structure():
    backend_alembic = REPO_ROOT / "backend" / "alembic"
    assert backend_alembic.exists() and backend_alembic.is_dir()

    versions_dir = backend_alembic / "versions"
    assert versions_dir.exists() and versions_dir.is_dir()

    mako_template = backend_alembic / "script.py.mako"
    assert mako_template.exists() and mako_template.is_file()

    env_py = backend_alembic / "env.py"
    assert env_py.exists() and env_py.is_file()


def test_alembic_script_directory_loads():
    ini_path = REPO_ROOT / "alembic.ini"
    config = Config(str(ini_path))
    script = ScriptDirectory.from_config(config)
    assert script is not None
    assert script.dir == str((REPO_ROOT / "backend" / "alembic").resolve())


def test_alembic_env_py_contains_async_routines():
    env_py_path = REPO_ROOT / "backend" / "alembic" / "env.py"
    content = env_py_path.read_text(encoding="utf-8")

    assert "run_async_migrations" in content
    assert "do_run_migrations" in content
    assert "run_migrations_online" in content
    assert "run_migrations_offline" in content
    assert "async_engine_from_config" in content
