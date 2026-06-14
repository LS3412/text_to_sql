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
    provider: str = "ollama"
    base_url: str = "http://localhost:11434"
    api_key: str = ""
    model: str = "qwen2:7b"
    temperature: float = 0.7
    # Deterministic temperature for the dedicated SQL-generation LLM (stage 5).
    sql_temperature: float = 0.0
    max_tokens: int = 2048
    timeout: int = 30

    class Config:
        env_file = ".env"
        env_prefix = "LLM_"
        case_sensitive = False


class RouterSettings(BaseSettings):
    """Stage 1 — Query Classifier & Database Router configuration."""
    use_llm_fallback: bool = False
    default_profile: str = "postgres"
    ambiguity_min_overlap: int = 1
    out_of_scope_message: str = (
        "I can only answer questions about your operational data (stores, tasks, "
        "performance, and the like). Try asking about completion rates, task status, "
        "or store comparisons."
    )

    class Config:
        env_file = ".env"
        env_prefix = "ROUTER_"
        case_sensitive = False


class SemanticSettings(BaseSettings):
    """Stage 2 — Semantic Layer (data dictionary) configuration."""
    dictionary_path: str = "config/semantic/dictionary.yaml"

    class Config:
        env_file = ".env"
        env_prefix = "SEMANTIC_"
        case_sensitive = False


class EmbeddingSettings(BaseSettings):
    """RAG embedding model (stage 3) — separate from the global MockEmbedding."""
    provider: str = "ollama"
    model: str = "nomic-embed-text"
    base_url: str = "http://localhost:11434"
    dim: int = 768
    timeout: int = 30
    enabled: bool = True

    class Config:
        env_file = ".env"
        env_prefix = "EMBED_"
        case_sensitive = False


class RetrievalSettings(BaseSettings):
    """Stage 3 — hybrid schema retrieval configuration."""
    keyword_prefilter_k: int = 12
    top_k_tables: int = 5
    max_compact_tables: int = 60
    vector_enabled: bool = True
    fuzzy_cutoff: float = 0.8

    class Config:
        env_file = ".env"
        env_prefix = "RETRIEVAL_"
        case_sensitive = False


class PipelineSettings(BaseSettings):
    """Pipeline-wide knobs (stages 8/10) and the schema-embedding pgvector DSN."""
    max_correction_retries: int = 2
    enable_correction_loop: bool = True
    correction_budget_seconds: int = 0  # 0 = no wall-clock budget
    source_dialect: str = "postgres"
    target_dialect: str = "postgres"
    # Connection for the schema_embeddings index. Empty -> use the app DB url.
    pgvector_dsn: str = ""

    class Config:
        env_file = ".env"
        env_prefix = "PIPELINE_"
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
    
    # Conversation memory turns
    memory_turns: int = 5
    a2a_agent_url: str = "http://localhost:8000"
    
    class Config:
        env_file = ".env"
        env_prefix = "APP_"
        case_sensitive = False


class EvalSettings(BaseSettings):
    """Evaluation configuration"""
    tenant_id: str = "tenant_a"
    agent_id: str = "field_user_agent"
    threshold: float = 0.7
    
    # FIXED: Allow optional None types cleanly to bypass strict string validation errors
    provider: str | None = None
    api_key: str | None = None
    model: str | None = None
    base_url: str | None = None
    timeout: int | None = None
    
    class Config:
        env_file = ".env"
        env_prefix = "EVAL_"
        case_sensitive = False


class Settings(BaseSettings):
    """Main settings class combining all configs"""
    database: DatabaseSettings = DatabaseSettings()
    redis: RedisSettings = RedisSettings()
    clickhouse: ClickHouseSettings = ClickHouseSettings()
    llm: LLMSettings = LLMSettings()
    app: ApplicationSettings = ApplicationSettings()
    eval: EvalSettings = EvalSettings()
    router: RouterSettings = RouterSettings()
    semantic: SemanticSettings = SemanticSettings()
    embedding: EmbeddingSettings = EmbeddingSettings()
    retrieval: RetrievalSettings = RetrievalSettings()
    pipeline: PipelineSettings = PipelineSettings()
    
    class Config:
        env_file = ".env"
        case_sensitive = False


@lru_cache()
def get_settings() -> Settings:
    """
    Get cached settings instance.
    """
    return Settings()
