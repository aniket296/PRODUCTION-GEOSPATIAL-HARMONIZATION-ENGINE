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


def test_alembic_model_metadata_discovery():
    from backend.alembic.env import target_metadata

    assert target_metadata is not None
    discovered_tables = set(target_metadata.tables.keys())

    expected_tables = {
        "raw_cadastral_layers",
        "harmonized_parcels",
        "topological_errors",
        "transformation_configs",
    }
    for table_name in expected_tables:
        assert table_name in discovered_tables, f"Table {table_name} missing from Alembic target_metadata"

    # Verify GeoAlchemy2 spatial geometry columns are attached to target metadata
    parcels_table = target_metadata.tables["harmonized_parcels"]
    assert "geometry_wgs84" in parcels_table.columns
    assert "geometry_projected" in parcels_table.columns

    errors_table = target_metadata.tables["topological_errors"]
    assert "error_geometry_wgs84" in errors_table.columns
    assert "error_geometry_projected" in errors_table.columns


def test_alembic_include_object_filter():
    from backend.alembic.env import EXCLUDED_TABLES, include_object

    assert "spatial_ref_sys" in EXCLUDED_TABLES
    assert "topology" in EXCLUDED_TABLES
    assert "layer" in EXCLUDED_TABLES

    # PostGIS system tables must be excluded from autogeneration diffs
    assert include_object(None, "spatial_ref_sys", "table", False, None) is False
    assert include_object(None, "topology", "table", False, None) is False
    assert include_object(None, "layer", "table", False, None) is False
    assert include_object(None, "geometry_columns", "table", False, None) is False

    # Cadastral application domain tables must be included
    assert include_object(None, "raw_cadastral_layers", "table", False, None) is True
    assert include_object(None, "harmonized_parcels", "table", False, None) is True
    assert include_object(None, "topological_errors", "table", False, None) is True
    assert include_object(None, "transformation_configs", "table", False, None) is True

