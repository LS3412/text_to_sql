-- ====================================================================
-- INITIALIZATION SCRIPT - Database and Extensions Setup
-- ====================================================================
-- This script initializes the database with necessary extensions and settings.

-- Enable required PostgreSQL extensions
CREATE EXTENSION IF NOT EXISTS "uuid-ossp";
CREATE EXTENSION IF NOT EXISTS "pg_trgm";  -- For text search optimization
CREATE EXTENSION IF NOT EXISTS "btree_gin";
CREATE EXTENSION IF NOT EXISTS "btree_gist";

-- Set default session settings
ALTER DATABASE a2a_db SET timezone = 'UTC';
ALTER DATABASE a2a_db SET log_statement = 'all';

-- Create application user if not exists (if running in non-Docker environment)
-- Note: In Docker, this is handled by POSTGRES_USER environment variable
DO $$ 
BEGIN
  IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'a2a_app_user') THEN
    CREATE ROLE a2a_app_user WITH LOGIN PASSWORD 'app_secure_password';
  END IF;
END $$;

-- Grant permissions to app user
GRANT CONNECT ON DATABASE a2a_db TO a2a_app_user;
GRANT USAGE ON SCHEMA public TO a2a_app_user;
GRANT CREATE ON SCHEMA public TO a2a_app_user;

-- Create audit schema for logging
CREATE SCHEMA IF NOT EXISTS audit AUTHORIZATION a2a_user;
GRANT USAGE ON SCHEMA audit TO a2a_app_user;
GRANT CREATE ON SCHEMA audit TO a2a_app_user;

-- Function to track table changes
CREATE OR REPLACE FUNCTION audit.audit_trigger()
RETURNS TRIGGER AS $$
BEGIN
    IF TG_OP = 'DELETE' THEN
        INSERT INTO audit.audit_log (schema_name, table_name, user_name, action, record)
        VALUES (TG_TABLE_SCHEMA, TG_TABLE_NAME, current_user, TG_OP, to_jsonb(OLD));
        RETURN OLD;
    ELSIF TG_OP = 'UPDATE' THEN
        INSERT INTO audit.audit_log (schema_name, table_name, user_name, action, record_before, record_after)
        VALUES (TG_TABLE_SCHEMA, TG_TABLE_NAME, current_user, TG_OP, to_jsonb(OLD), to_jsonb(NEW));
        RETURN NEW;
    ELSIF TG_OP = 'INSERT' THEN
        INSERT INTO audit.audit_log (schema_name, table_name, user_name, action, record)
        VALUES (TG_TABLE_SCHEMA, TG_TABLE_NAME, current_user, TG_OP, to_jsonb(NEW));
        RETURN NEW;
    END IF;
    RETURN NULL;
END;
$$ LANGUAGE plpgsql;

-- Create audit log table
CREATE TABLE IF NOT EXISTS audit.audit_log (
    id BIGSERIAL PRIMARY KEY,
    schema_name TEXT NOT NULL,
    table_name TEXT NOT NULL,
    user_name TEXT NOT NULL,
    action TEXT NOT NULL,
    action_timestamp TIMESTAMPTZ DEFAULT now(),
    record JSONB,
    record_before JSONB,
    record_after JSONB
);

CREATE INDEX idx_audit_log_timestamp ON audit.audit_log (action_timestamp DESC);
CREATE INDEX idx_audit_log_table ON audit.audit_log (schema_name, table_name);
CREATE INDEX idx_audit_log_user ON audit.audit_log (user_name);

GRANT SELECT ON audit.audit_log TO a2a_app_user;

-- Logging
\echo 'Database initialization completed successfully!'
\echo 'Extensions loaded: uuid-ossp, pg_trgm, btree_gin, btree_gist'
\echo 'Audit schema created with audit logging capability'
