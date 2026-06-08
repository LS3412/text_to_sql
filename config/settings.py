"""
Configuration module for the Text-to-SQL A2A application.
Handles environment variables and application settings using Pydantic.
"""

from functools import lru_cache
from pydantic_settings import BaseSettings


class DatabaseSettings(BaseSettings):
    """Database configuration"""
    host: str = "localhost"
    port: int = 5435
    user: str = "a2a_user"
    password: str = "secure_password_change_me"
    name: str = "a2a_db"

    # Read-only role used by the SQL Skill to execute LLM-generated SELECTs (§7.2).
    # Created by database/schema.sql; subject to tenant RLS on the data tables.
    readonly_user: str = "a2a_readonly"
    readonly_password: str = "readonly_secure_password_change_me"

    @property
    def url(self) -> str:
        """Get the read/write database URL (used for audit writes to chat_history)."""
        return f"postgresql+asyncpg://{self.user}:{self.password}@{self.host}:{self.port}/{self.name}"

    @property
    def readonly_url(self) -> str:
        """Get the read-only database URL (used by skills to run generated SQL)."""
        return f"postgresql+asyncpg://{self.readonly_user}:{self.readonly_password}@{self.host}:{self.port}/{self.name}"

    class Config:
        env_prefix = "DB_"
        env_file = ".env"
        case_sensitive = False


class RedisSettings(BaseSettings):
    """Redis configuration"""
    host: str = "localhost"
    port: int = 6385
    password: str = "redis_secure_password_change_me"
    db: int = 0
    
    @property
    def url(self) -> str:
        """Get Redis URL"""
        if self.password:
            return f"redis://:{self.password}@{self.host}:{self.port}/{self.db}"
        return f"redis://{self.host}:{self.port}/{self.db}"
    
    class Config:
        env_prefix = "REDIS_"
        env_file = ".env"
        case_sensitive = False


# ADD ClickHouse configuration class
class ClickHouseSettings(BaseSettings):
    """ClickHouse configuration"""
    host: str = "localhost"
    port: int = 8125
    user: str = "default"
    password: str = "secure_password_change_me"
    
    class Config:
        env_prefix = "CLICKHOUSE_"
        env_file = ".env"
        case_sensitive = False


class LLMSettings(BaseSettings):
    """LLM (Language Model) configuration"""
    api_key: str = ""
    model: str = "qwen2:7b"
    temperature: float = 0.7
    max_tokens: int = 2048
    timeout: int = 30
    
    class Config:
        env_file = ".env"
        env_prefix = "LLM_"
        case_sensitive = False


class ApplicationSettings(BaseSettings):
    """Application configuration"""
    env: str = "development"
    log_level: str = "DEBUG"
    api_host: str = "0.0.0.0"
    api_port: int = 8000
    
    # Agent defaults
    default_agent_id: str = "field_user_agent"
    default_tenant_id: str = "default_tenant"
    
    # SQL Execution
    sql_query_timeout: int = 30  # seconds
    max_result_rows: int = 10000
    
    # Caching
    cache_ttl: int = 3600  # 1 hour in seconds
    schema_cache_ttl: int = 86400  # 24 hours

    # Conversation memory (§3.1) — number of recent A2UI_DISPLAY turns to load
    # per session so follow-ups like "what about yesterday?" have context.
    memory_turns: int = 5

    class Config:
        env_file = ".env"
        env_prefix = "APP_"
        case_sensitive = False


class Settings(BaseSettings):
    """Main settings class combining all configs"""
    database: DatabaseSettings = DatabaseSettings()
    redis: RedisSettings = RedisSettings()
    clickhouse: ClickHouseSettings = ClickHouseSettings()  # <-- registered here!
    llm: LLMSettings = LLMSettings()
    app: ApplicationSettings = ApplicationSettings()
    
    class Config:
        env_file = ".env"
        case_sensitive = False


@lru_cache()
def get_settings() -> Settings:
    """
    Get cached settings instance.
    Use this function to access settings throughout the application.
    """
    return Settings()
