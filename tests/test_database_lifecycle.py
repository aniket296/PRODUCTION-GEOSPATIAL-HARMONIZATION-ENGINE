"""Unit and lifecycle tests for asynchronous SQLAlchemy engine and session management.

Tests async engine connectivity, connection pooling, sessionmaker configuration,
transaction commit/rollback semantics, and FastAPI-style session lifecycle generators.
"""

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)


@pytest.fixture
def async_engine():
    engine = create_async_engine(
        "sqlite+aiosqlite:///:memory:",
        echo=False,
        pool_pre_ping=True,
    )
    yield engine
    engine.sync_engine.dispose()


@pytest.fixture
def session_factory(async_engine):
    return async_sessionmaker(
        bind=async_engine,
        class_=AsyncSession,
        expire_on_commit=False,
        autoflush=False,
    )


class TestAsyncEngineLifecycle:
    """Verifies asynchronous engine instantiation and execution semantics."""

    @pytest.mark.asyncio
    async def test_async_engine_instance(self, async_engine):
        assert isinstance(async_engine, AsyncEngine)
        assert async_engine.pool is not None

    @pytest.mark.asyncio
    async def test_async_engine_connect_and_query(self, async_engine):
        async with async_engine.connect() as conn:
            result = await conn.execute(text("SELECT 42 AS val"))
            row = result.mappings().one()
            assert row["val"] == 42


class TestAsyncSessionLifecycle:
    """Verifies session factory, transaction commit, rollback, and cleanup lifecycle."""

    @pytest.mark.asyncio
    async def test_sessionmaker_configuration(self, session_factory):
        session = session_factory()
        assert isinstance(session, AsyncSession)
        assert session.sync_session.expire_on_commit is False
        await session.close()

    @pytest.mark.asyncio
    async def test_session_commit_lifecycle(self, async_engine, session_factory):
        # Create a test table
        async with async_engine.begin() as conn:
            await conn.execute(text("CREATE TABLE test_items (id INTEGER PRIMARY KEY, name TEXT)"))

        # Session 1: insert and commit
        async with session_factory() as session:
            await session.execute(
                text("INSERT INTO test_items (id, name) VALUES (:id, :name)"),
                {"id": 1, "name": "cadastre_parcel_01"},
            )
            await session.commit()

        # Session 2: verify committed data
        async with session_factory() as session:
            result = await session.execute(
                text("SELECT name FROM test_items WHERE id = :id"),
                {"id": 1},
            )
            row = result.mappings().one()
            assert row["name"] == "cadastre_parcel_01"

    @pytest.mark.asyncio
    async def test_session_rollback_on_exception(self, async_engine, session_factory):
        # Create a test table
        async with async_engine.begin() as conn:
            await conn.execute(text("CREATE TABLE test_rollback (id INTEGER PRIMARY KEY, name TEXT)"))

        # Session: attempt insert then raise exception
        with pytest.raises(RuntimeError, match="Simulated transaction fault"):
            async with session_factory() as session:
                await session.execute(
                    text("INSERT INTO test_rollback (id, name) VALUES (:id, :name)"),
                    {"id": 100, "name": "should_be_rolled_back"},
                )
                raise RuntimeError("Simulated transaction fault")

        # Session: verify data was rolled back and table is empty
        async with session_factory() as session:
            result = await session.execute(text("SELECT COUNT(*) AS total FROM test_rollback"))
            count = result.scalar_one()
            assert count == 0


class TestAsyncSessionGeneratorDependency:
    """Verifies FastAPI-compatible dependency generator lifecycle."""

    @pytest.mark.asyncio
    async def test_session_generator_lifecycle_clean(self, session_factory):
        async def session_dependency():
            async with session_factory() as session:
                try:
                    yield session
                    await session.commit()
                except Exception:
                    await session.rollback()
                    raise
                finally:
                    await session.close()

        gen = session_dependency()
        session = await anext(gen)
        assert isinstance(session, AsyncSession)
        assert session.is_active

        # Clean exit from caller
        with pytest.raises(StopAsyncIteration):
            await anext(gen)

    @pytest.mark.asyncio
    async def test_session_generator_lifecycle_on_error(self, async_engine, session_factory):
        async with async_engine.begin() as conn:
            await conn.execute(text("CREATE TABLE test_gen_err (id INTEGER PRIMARY KEY, val TEXT)"))

        async def session_dependency():
            async with session_factory() as session:
                try:
                    yield session
                    await session.commit()
                except Exception:
                    await session.rollback()
                    raise
                finally:
                    await session.close()

        gen = session_dependency()
        session = await anext(gen)
        await session.execute(
            text("INSERT INTO test_gen_err (id, val) VALUES (1, 'temp')")
        )

        # Simulate exception raised in route handler
        with pytest.raises(ValueError, match="Route handler error"):
            await gen.athrow(ValueError("Route handler error"))

        # Verify transaction was rolled back
        async with session_factory() as check_session:
            result = await check_session.execute(text("SELECT COUNT(*) FROM test_gen_err"))
            assert result.scalar_one() == 0


class TestBackendDBSessionModule:
    """Verifies backend.db package exports, custom engine creation, and declarative base."""

    def test_declarative_base_inheritance(self):
        from backend.db.base import Base
        from sqlalchemy.orm import DeclarativeBase
        assert issubclass(Base, DeclarativeBase)

    def test_backend_db_exports(self):
        import backend.db as db
        assert hasattr(db, "Base")
        assert hasattr(db, "engine")
        assert hasattr(db, "async_session_factory")
        assert hasattr(db, "AsyncSessionLocal")
        assert hasattr(db, "get_async_session")
        assert hasattr(db, "create_engine_instance")
        assert hasattr(db, "get_sync_engine")

    @pytest.mark.asyncio
    async def test_custom_sqlite_engine_creation(self):
        from backend.db.session import create_engine_instance, async_sessionmaker
        test_engine = create_engine_instance("sqlite+aiosqlite:///:memory:")
        assert isinstance(test_engine, AsyncEngine)
        factory = async_sessionmaker(bind=test_engine, class_=AsyncSession, expire_on_commit=False)
        async with factory() as session:
            res = await session.execute(text("SELECT 'harmonized' AS status"))
            assert res.scalar_one() == "harmonized"
        test_engine.sync_engine.dispose()
