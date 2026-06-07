"""
Database connection and session management.
"""

from typing import AsyncGenerator, Optional
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, create_async_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import NullPool

from config.settings import get_settings


class DatabaseManager:
    """Manages database connections and sessions"""
    
    _engine: Optional[AsyncEngine] = None
    _async_session_maker: Optional[sessionmaker] = None
    
    @classmethod
    def init(cls) -> None:
        """Initialize database engine and session factory"""
        settings = get_settings()
        
        # Create async engine
        database_url = settings.database.url.replace("postgresql://", "postgresql+asyncpg://")
        
        cls._engine = create_async_engine(
            database_url,
            echo=settings.app.env == "development",
            poolclass=NullPool,  # Better for containerized apps
            connect_args={
                "timeout": 10,
                "command_timeout": settings.app.sql_query_timeout,
                "server_settings": {
                    "application_name": "a2a_text_to_sql",
                },
            },
        )
        
        cls._async_session_maker = sessionmaker(
            cls._engine,
            class_=AsyncSession,
            expire_on_commit=False,
            autoflush=False,
        )
    
    @classmethod
    async def get_session(cls) -> AsyncGenerator[AsyncSession, None]:
        """Get async database session"""
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


# Convenience functions
async def get_db_session() -> AsyncGenerator[AsyncSession, None]:
    """Dependency injection for FastAPI"""
    async for session in DatabaseManager.get_session():
        yield session
