-- ==========================================
-- 1. CLEAN REBUILD: DROP EXISTING OBJECTS
-- ==========================================
DROP TABLE IF EXISTS active_tasks CASCADE;
DROP TABLE IF EXISTS stores CASCADE;
DROP TABLE IF EXISTS chat_history CASCADE;
DROP TYPE IF EXISTS chat_message_type CASCADE;

-- ==========================================
-- 2. READ-ONLY ROLE  (§7.2 — skills execute generated SQL as a read-only user)
-- The application (FastAPI) connects as the owner (a2a_user) to write audit
-- rows; the SQL Skill connects as a2a_readonly to run LLM-generated SELECTs.
-- ==========================================
DO $$
BEGIN
  IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'a2a_readonly') THEN
    CREATE ROLE a2a_readonly WITH LOGIN PASSWORD 'readonly_secure_password_change_me';
  END IF;
END $$;

GRANT CONNECT ON DATABASE a2a_db TO a2a_readonly;
GRANT USAGE ON SCHEMA public TO a2a_readonly;

-- ==========================================
-- 3. CREATE SCHEMAS & ENUMS
-- ==========================================
CREATE TYPE chat_message_type AS ENUM (
    'A2UI_DISPLAY',   -- Final output JSON data for the UI
    'TOOL_CALL',      -- Agent calling a DB skill
    'TOOL_RESULT',    -- Data returned from database
    'AGENT_INTERNAL', -- Internal reasoning / chain of thought
    'SYSTEM_LOG'      -- Exception and error states
);

-- ==========================================
-- 4. CREATE PARTITIONED AUDIT TABLE
-- ==========================================
CREATE TABLE chat_history (
    id              UUID DEFAULT gen_random_uuid(),
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    message_type    chat_message_type NOT NULL,

    -- Tenant Isolation
    agent_id        VARCHAR(255) NOT NULL,
    tenant_id       VARCHAR(255) NOT NULL,

    -- Thread Tracking
    session_id      VARCHAR(255) NOT NULL,
    user_id         VARCHAR(255),
    parent_id       UUID, -- Simplified tracking column (decoupled from self-referencing FK constraints)

    -- Payloads
    request_payload  JSONB,
    response_payload JSONB NOT NULL,   -- Stores the final structured JSON database results

    -- Observability & Metrics
    trace_id        VARCHAR(255),
    model_name      VARCHAR(255),
    llm_usage_cost  NUMERIC(10, 8) DEFAULT 0.0,
    latency_ms      INTEGER,

    -- Metadata
    feedback        JSONB,
    metadata        JSONB,

    -- Composite primary key solves the range-partitioning constraint error
    PRIMARY KEY (id, created_at)
) PARTITION BY RANGE (created_at);

-- DEFAULT partition: dev safety net so inserts never fail at midnight even if
-- the daily partition job hasn't run yet. See IMPLEMENTATION_NOTES.md for the
-- trade-off vs. the spec's strict daily-partition + DROP-to-purge model.
CREATE TABLE chat_history_default PARTITION OF chat_history DEFAULT;

-- 4.1 INDEXES (§5) ------------------------------------------------------------
-- On a partitioned table these are propagated to every (current + future) partition.
CREATE INDEX idx_chat_history_a2ui_filter
    ON chat_history (agent_id, tenant_id, session_id, message_type);
CREATE INDEX idx_chat_history_created_at   ON chat_history (created_at);
CREATE INDEX idx_chat_history_user_id      ON chat_history (user_id);
CREATE INDEX idx_chat_history_trace_id     ON chat_history (trace_id);
CREATE INDEX idx_chat_history_feedback_gin ON chat_history USING GIN (feedback);

-- 4.2 DAILY PARTITION HELPER (§5.1) ------------------------------------------
-- Run scripts/manage_partitions.py from cron once a day to pre-create tomorrow's
-- partition. Creating a future-dated partition while the DEFAULT partition exists
-- is safe because no future rows live in DEFAULT yet.
CREATE OR REPLACE FUNCTION create_daily_partition(target_date DATE)
RETURNS void AS $$
DECLARE
    partition_name TEXT := 'chat_history_y' || to_char(target_date, 'YYYY')
                                            || 'm' || to_char(target_date, 'MM')
                                            || 'd' || to_char(target_date, 'DD');
    start_ts TEXT := to_char(target_date,            'YYYY-MM-DD') || ' 00:00:00+00';
    end_ts   TEXT := to_char(target_date + INTERVAL '1 day', 'YYYY-MM-DD') || ' 00:00:00+00';
BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_class WHERE relname = partition_name) THEN
        EXECUTE format(
            'CREATE TABLE %I PARTITION OF chat_history FOR VALUES FROM (%L) TO (%L)',
            partition_name, start_ts, end_ts
        );
    END IF;
END;
$$ LANGUAGE plpgsql;

-- ==========================================
-- 5. ROW-LEVEL SECURITY (RLS) — chat_history (agent isolation)
-- FORCE so the table owner (a2a_user) cannot silently bypass the policy.
-- ==========================================
ALTER TABLE chat_history ENABLE ROW LEVEL SECURITY;
ALTER TABLE chat_history FORCE ROW LEVEL SECURITY;

CREATE POLICY agent_isolation_policy ON chat_history
    FOR ALL USING (agent_id = current_setting('app.agent_id'));

-- ==========================================
-- 6. CREATE CORE DATA TABLES (tenant-scoped)
-- ==========================================
CREATE TABLE stores (
    store_id SERIAL PRIMARY KEY,
    store_name VARCHAR(100) NOT NULL,
    district_id INTEGER NOT NULL,
    completion_rate NUMERIC(5, 2) NOT NULL,
    tenant_id VARCHAR(255) NOT NULL   -- §7.3 tenant isolation enforced via RLS below
);

-- Add column comment descriptors for ClickHouse dynamic metadata indexing
COMMENT ON COLUMN stores.store_id IS 'Unique identifier of the physical store';
COMMENT ON COLUMN stores.store_name IS 'The name of the retail store';
COMMENT ON COLUMN stores.district_id IS 'ID of the regional district';
COMMENT ON COLUMN stores.completion_rate IS 'The overall audit task completion percentage of the store';
COMMENT ON COLUMN stores.tenant_id IS 'Owning tenant; rows are isolated per tenant via RLS';

CREATE TABLE active_tasks (
    task_id SERIAL PRIMARY KEY,
    store_id INTEGER REFERENCES stores(store_id) ON DELETE CASCADE,
    task_name VARCHAR(150) NOT NULL,
    status VARCHAR(50) NOT NULL,
    tenant_id VARCHAR(255) NOT NULL   -- §7.3 tenant isolation enforced via RLS below
);

COMMENT ON COLUMN active_tasks.task_id IS 'Unique task identifier';
COMMENT ON COLUMN active_tasks.store_id IS 'ID of the store this audit task belongs to';
COMMENT ON COLUMN active_tasks.task_name IS 'The name or description of the audit task';
COMMENT ON COLUMN active_tasks.status IS 'Current completion status of the task (Pending, In Progress, Completed)';
COMMENT ON COLUMN active_tasks.tenant_id IS 'Owning tenant; rows are isolated per tenant via RLS';

-- ==========================================
-- 7. SEED MOCK DATA RECORDS
-- Seed BEFORE enabling RLS so the owner's INSERTs are not blocked by the
-- tenant policy (which requires app.tenant_id to be set at query time).
-- store_ids are assigned 1..5 in insert order.
-- ==========================================
INSERT INTO stores (store_name, district_id, completion_rate, tenant_id) VALUES
('Store 118', 1, 95.50, 'tenant_a'),   -- store_id 1
('Store 202', 1, 82.10, 'tenant_a'),   -- store_id 2
('Store 304', 2, 88.00, 'tenant_a'),   -- store_id 3
('Store 500', 3, 73.40, 'tenant_b'),   -- store_id 4
('Store 501', 3, 91.20, 'tenant_b');   -- store_id 5

INSERT INTO active_tasks (store_id, task_name, status, tenant_id) VALUES
(1, 'Audit Inventory', 'In Progress', 'tenant_a'),
(1, 'Display Setup',   'Completed',   'tenant_a'),
(2, 'Restock Items',   'Pending',     'tenant_a'),
(4, 'Audit Inventory', 'Pending',     'tenant_b');

-- ==========================================
-- 8. TENANT ISOLATION RLS ON DATA TABLES (§7.3)
-- The read-only role only ever sees rows for the tenant set on its connection
-- via SELECT set_config('app.tenant_id', <tenant>, true). missing_ok=true keeps
-- the policy from erroring when the GUC is unset (it simply returns no rows).
-- ==========================================
-- IMPORTANT: the stage-9 Execution Engine runs LLM-generated SELECTs as a2a_readonly.
-- Any NEW in-scope data table added to config/semantic/dictionary.yaml must also be
-- granted here, otherwise queries against it fail at execution (caught by the
-- correction/apology path, not a crash). e.g.:
--   GRANT SELECT ON <new_table> TO a2a_readonly;
GRANT SELECT ON stores, active_tasks TO a2a_readonly;

ALTER TABLE stores ENABLE ROW LEVEL SECURITY;
ALTER TABLE stores FORCE ROW LEVEL SECURITY;
CREATE POLICY tenant_isolation_stores ON stores
    FOR ALL USING (tenant_id = current_setting('app.tenant_id', true));

ALTER TABLE active_tasks ENABLE ROW LEVEL SECURITY;
ALTER TABLE active_tasks FORCE ROW LEVEL SECURITY;
CREATE POLICY tenant_isolation_active_tasks ON active_tasks
    FOR ALL USING (tenant_id = current_setting('app.tenant_id', true));

    -- ==========================================
-- 7. PARTITION AUTOMATION PL/pgSQL FUNCTION
-- ==========================================
CREATE OR REPLACE FUNCTION create_daily_partition(target_date DATE) 
RETURNS VOID AS $$
DECLARE
    partition_name TEXT;
    start_range TEXT;
    end_range TEXT;
    exists_check BOOLEAN;
BEGIN
    -- Format partition table name as chat_history_y2026m06d08
    partition_name := 'chat_history_y' || to_char(target_date, 'YYYY') || 'm' || to_char(target_date, 'MM') || 'd' || to_char(target_date, 'DD');
    
    -- Format date bounds (UTC timezone safe)
    start_range := to_char(target_date, 'YYYY-MM-DD') || ' 00:00:00+00';
    end_range := to_char(target_date + INTERVAL '1 day', 'YYYY-MM-DD') || ' 00:00:00+00';
    
    -- Check if partition already exists
    SELECT EXISTS (
        SELECT 1 
        FROM pg_tables 
        WHERE tablename = partition_name
    ) INTO exists_check;
    
    -- Execute dynamic SQL DDL to attach range partition if it does not exist
    IF NOT exists_check THEN
        EXECUTE 'CREATE TABLE ' || quote_ident(partition_name) || 
                ' PARTITION OF chat_history FOR VALUES FROM (' || quote_literal(start_range) || ') TO (' || quote_literal(end_range) || ')';
    END IF;
END;
$$ LANGUAGE plpgsql;
