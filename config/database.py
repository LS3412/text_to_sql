"""
Database connection and session management.

Two connections are maintained:
  * the read/write engine (owner role) — used by the orchestrator to write
    audit rows into chat_history.
  * the read-only engine (a2a_readonly role) — used by the SQL Skill to execute
    LLM-generated SELECTs under tenant RLS (§7.2).
"""

from contextlib import asynccontextmanager
from typing import AsyncGenerator, AsyncIterator, Optional
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, create_async_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import NullPool

from config.settings import get_settings


def _connect_args(settings) -> dict:
    """Shared asyncpg connect args (timeouts + app name)."""
    return {
        "timeout": 10,
        "command_timeout": settings.app.sql_query_timeout,
        "server_settings": {
            "application_name": "a2a_text_to_sql",
        },
    }


class DatabaseManager:
    """Manages database connections and sessions"""

    _engine: Optional[AsyncEngine] = None
    _async_session_maker: Optional[sessionmaker] = None

    _readonly_engine: Optional[AsyncEngine] = None
    _readonly_session_maker: Optional[sessionmaker] = None

    @classmethod
    def init(cls) -> None:
        """Initialize the read/write database engine and session factory"""
        settings = get_settings()

        # Create async engine
        database_url = settings.database.url.replace("postgresql://", "postgresql+asyncpg://")

        cls._engine = create_async_engine(
            database_url,
            echo=settings.app.env == "development",
            poolclass=NullPool,  # Better for containerized apps
            connect_args=_connect_args(settings),
        )

        cls._async_session_maker = sessionmaker(
            cls._engine,
            class_=AsyncSession,
            expire_on_commit=False,
            autoflush=False,
        )

    @classmethod
    def init_readonly(cls) -> None:
        """Initialize the read-only database engine and session factory (§7.2)"""
        settings = get_settings()

        cls._readonly_engine = create_async_engine(
            settings.database.readonly_url,
            echo=settings.app.env == "development",
            poolclass=NullPool,
            connect_args=_connect_args(settings),
        )

        cls._readonly_session_maker = sessionmaker(
            cls._readonly_engine,
            class_=AsyncSession,
            expire_on_commit=False,
            autoflush=False,
        )

    @classmethod
    async def get_session(cls) -> AsyncGenerator[AsyncSession, None]:
        """Get a read/write async database session"""
        if cls._async_session_maker is None:
            cls.init()

        async with cls._async_session_maker() as session:  # type: ignore
            try:
                yield session
                await session.commit()
            except Exception:
                await session.rollback()
                raise
            finally:
                await session.close()

    @classmethod
    async def close(cls) -> None:
        """Close database connections"""
        if cls._engine:
            await cls._engine.dispose()
        if cls._readonly_engine:
            await cls._readonly_engine.dispose()


# Convenience functions
async def get_db_session() -> AsyncGenerator[AsyncSession, None]:
    """Dependency injection for FastAPI"""
    async for session in DatabaseManager.get_session():
        yield session


@asynccontextmanager
async def readonly_session() -> AsyncIterator[AsyncSession]:
    """
    Yield a short-lived read-only session for executing generated SQL.
    SELECT-only — the transaction is rolled back on exit (never committed).
    """
    if DatabaseManager._readonly_session_maker is None:
        DatabaseManager.init_readonly()

    async with DatabaseManager._readonly_session_maker() as session:  # type: ignore
        try:
            yield session
        finally:
            await session.rollback()
            await session.close()
