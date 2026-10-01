"""Asynchronous SQLAlchemy 2.x engine and session factory.

Provides connection pooling, session lifecycle management, and FastAPI dependency integration.
"""

from collections.abc import AsyncGenerator
from typing import Optional
from sqlalchemy import create_engine
from sqlalchemy.engine import Engine
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from backend.core.config import get_settings


def create_engine_instance(database_url: Optional[str] = None) -> AsyncEngine:
    """Instantiate an AsyncEngine with connection pool tuning and health pre-ping."""
    settings = get_settings()
    url = database_url or settings.database_url

    # In SQLite memory/file testing, pool_size is not supported
    if "sqlite" in url:
        return create_async_engine(
            url,
            echo=settings.debug,
            pool_pre_ping=True,
        )

    return create_async_engine(
        url,
        echo=settings.debug,
        pool_pre_ping=True,
        pool_size=10,
        max_overflow=20,
    )


# Global default engine and sessionmaker
engine: AsyncEngine = create_engine_instance()

async_session_factory: async_sessionmaker[AsyncSession] = async_sessionmaker(
    bind=engine,
    class_=AsyncSession,
    expire_on_commit=False,
    autoflush=False,
)

AsyncSessionLocal = async_session_factory


async def get_async_session() -> AsyncGenerator[AsyncSession, None]:
    """FastAPI dependency yielding an AsyncSession with managed commit/rollback lifecycle."""
    async with async_session_factory() as session:
        try:
            yield session
            await session.commit()
        except Exception:
            await session.rollback()
            raise
        finally:
            await session.close()


def get_sync_engine(database_sync_url: Optional[str] = None) -> Engine:
    """Return synchronous SQLAlchemy engine for Alembic migrations and utility scripts."""
    settings = get_settings()
    url = database_sync_url or settings.database_sync_url
    return create_engine(url, pool_pre_ping=True)
