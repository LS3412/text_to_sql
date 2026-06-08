-- ==========================================
-- 1. CLEAN REBUILD: DROP EXISTING OBJECTS
-- ==========================================
DROP TABLE IF EXISTS active_tasks CASCADE;
DROP TABLE IF EXISTS users CASCADE;
DROP TABLE IF EXISTS stores CASCADE;
DROP TABLE IF EXISTS districts CASCADE;
DROP TABLE IF EXISTS chat_history CASCADE;
DROP TYPE IF EXISTS chat_message_type CASCADE;

-- ==========================================
-- 2. READ-ONLY ROLE  (§7.2 — skills execute generated SQL as a read-only user)
-- The application (FastAPI) connects as the owner (a2a_user) to write audit
-- rows; the SQL Skill connects as a2a_readonly to run LLM-generated SELECTs.
-- The same read-only role is also used (by config/database.py's sync engine) for
-- LlamaIndex SQLDatabase schema reflection at startup — least privilege, and it
-- already holds the SELECT grants below.
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
-- Hierarchy: districts 1—* stores 1—* active_tasks; users belong to a district
-- and are assigned tasks. Every table carries tenant_id and is isolated via RLS
-- (§7.3). The richer columns (due_date, completed_at, priority, project_type)
-- make the spec's target questions answerable purely in SQL.
-- ==========================================
CREATE TABLE districts (
    district_id   SERIAL PRIMARY KEY,
    district_name VARCHAR(100) NOT NULL,
    region        VARCHAR(100),
    tenant_id     VARCHAR(255) NOT NULL   -- §7.3 tenant isolation enforced via RLS below
);

COMMENT ON COLUMN districts.district_id IS 'Unique identifier of the regional district';
COMMENT ON COLUMN districts.district_name IS 'The name of the district, e.g. North District';
COMMENT ON COLUMN districts.region IS 'The geographic region or cluster the district belongs to';
COMMENT ON COLUMN districts.tenant_id IS 'Owning tenant; rows are isolated per tenant via RLS';

CREATE TABLE stores (
    store_id SERIAL PRIMARY KEY,
    store_name VARCHAR(100) NOT NULL,
    district_id INTEGER REFERENCES districts(district_id) ON DELETE SET NULL,
    completion_rate NUMERIC(5, 2) NOT NULL,
    tenant_id VARCHAR(255) NOT NULL   -- §7.3 tenant isolation enforced via RLS below
);

-- Add column comment descriptors for ClickHouse dynamic metadata indexing
COMMENT ON COLUMN stores.store_id IS 'Unique identifier of the physical store';
COMMENT ON COLUMN stores.store_name IS 'The name of the retail store';
COMMENT ON COLUMN stores.district_id IS 'ID of the regional district the store belongs to';
COMMENT ON COLUMN stores.completion_rate IS 'The overall audit task completion percentage of the store';
COMMENT ON COLUMN stores.tenant_id IS 'Owning tenant; rows are isolated per tenant via RLS';

CREATE TABLE users (
    user_id     SERIAL PRIMARY KEY,
    user_name   VARCHAR(100) NOT NULL,
    role        VARCHAR(50),
    district_id INTEGER REFERENCES districts(district_id) ON DELETE SET NULL,
    tenant_id   VARCHAR(255) NOT NULL   -- §7.3 tenant isolation enforced via RLS below
);

COMMENT ON COLUMN users.user_id IS 'Unique identifier of the field user / manager';
COMMENT ON COLUMN users.user_name IS 'The display name of the user';
COMMENT ON COLUMN users.role IS 'The user role, e.g. Store Manager, District Manager';
COMMENT ON COLUMN users.district_id IS 'ID of the district this user manages or belongs to';
COMMENT ON COLUMN users.tenant_id IS 'Owning tenant; rows are isolated per tenant via RLS';

CREATE TABLE active_tasks (
    task_id          SERIAL PRIMARY KEY,
    store_id         INTEGER REFERENCES stores(store_id) ON DELETE CASCADE,
    task_name        VARCHAR(150) NOT NULL,
    status           VARCHAR(50) NOT NULL,
    priority         VARCHAR(20),
    project_type     VARCHAR(50),
    assigned_user_id INTEGER REFERENCES users(user_id) ON DELETE SET NULL,
    created_at       TIMESTAMPTZ NOT NULL DEFAULT now(),
    due_date         TIMESTAMPTZ,
    completed_at     TIMESTAMPTZ,
    tenant_id        VARCHAR(255) NOT NULL   -- §7.3 tenant isolation enforced via RLS below
);

COMMENT ON COLUMN active_tasks.task_id IS 'Unique task identifier';
COMMENT ON COLUMN active_tasks.store_id IS 'ID of the store this audit task belongs to';
COMMENT ON COLUMN active_tasks.task_name IS 'The name or description of the audit task';
COMMENT ON COLUMN active_tasks.status IS 'Current completion status of the task (Pending, In Progress, Completed)';
COMMENT ON COLUMN active_tasks.priority IS 'Task priority, e.g. Low, Medium, High';
COMMENT ON COLUMN active_tasks.project_type IS 'The kind of project, e.g. Store Walk, Inventory, Reset';
COMMENT ON COLUMN active_tasks.assigned_user_id IS 'ID of the user the task is assigned to';
COMMENT ON COLUMN active_tasks.created_at IS 'When the task was created';
COMMENT ON COLUMN active_tasks.due_date IS 'Deadline by which the task must be completed; used for overdue and at-risk detection';
COMMENT ON COLUMN active_tasks.completed_at IS 'When the task was actually completed; null if still open. Used to detect late completion';
COMMENT ON COLUMN active_tasks.tenant_id IS 'Owning tenant; rows are isolated per tenant via RLS';

-- ==========================================
-- 7. SEED MOCK DATA RECORDS
-- Seed BEFORE enabling RLS so the owner's INSERTs are not blocked by the
-- tenant policy (which requires app.tenant_id to be set at query time).
-- IDs are assigned in insert order. Dates are relative to now() so the
-- "today" / "overdue" / "late" target questions stay meaningful over time.
-- tenant_a: districts 1-2, stores 1-3; tenant_b: district 3, stores 4-5.
-- ==========================================
INSERT INTO districts (district_name, region, tenant_id) VALUES
('North District', 'West Region', 'tenant_a'),   -- district_id 1
('South District', 'West Region', 'tenant_a'),   -- district_id 2
('East District',  'East Region', 'tenant_b');   -- district_id 3

INSERT INTO stores (store_name, district_id, completion_rate, tenant_id) VALUES
('Store 118', 1, 95.50, 'tenant_a'),   -- store_id 1
('Store 202', 1, 82.10, 'tenant_a'),   -- store_id 2
('Store 304', 2, 88.00, 'tenant_a'),   -- store_id 3
('Store 500', 3, 73.40, 'tenant_b'),   -- store_id 4
('Store 501', 3, 91.20, 'tenant_b');   -- store_id 5

INSERT INTO users (user_name, role, district_id, tenant_id) VALUES
('Alice Stone',   'Store Manager',    1, 'tenant_a'),   -- user_id 1
('Bob Rivera',    'Store Manager',    1, 'tenant_a'),   -- user_id 2
('Carol Diaz',    'District Manager', 2, 'tenant_a'),   -- user_id 3
('Dan Brooks',    'Store Manager',    3, 'tenant_b');   -- user_id 4

-- A mix of completed/in-progress/pending tasks with deadlines in the past and
-- future, and some completed AFTER their due_date (late), so root-cause and
-- overdue/at-risk questions return real rows.
INSERT INTO active_tasks
    (store_id, task_name, status, priority, project_type, assigned_user_id, created_at, due_date, completed_at, tenant_id)
VALUES
(1, 'Audit Inventory', 'In Progress', 'High',   'Inventory',  1, now() - INTERVAL '3 days', now() + INTERVAL '6 hours',  NULL,                        'tenant_a'),
(1, 'Display Setup',   'Completed',   'Medium', 'Reset',      1, now() - INTERVAL '5 days', now() - INTERVAL '2 days',   now() - INTERVAL '1 day',    'tenant_a'),  -- late (completed after due)
(1, 'Store Walk',      'Pending',     'Low',    'Store Walk', 2, now() - INTERVAL '1 day',  now() - INTERVAL '2 hours',  NULL,                        'tenant_a'),  -- overdue
(2, 'Restock Items',   'Pending',     'High',   'Inventory',  2, now() - INTERVAL '2 days', now() + INTERVAL '1 day',    NULL,                        'tenant_a'),
(2, 'Store Walk',      'In Progress', 'Medium', 'Store Walk', 1, now() - INTERVAL '4 days', now() - INTERVAL '1 day',    NULL,                        'tenant_a'),  -- overdue, in progress
(3, 'Audit Inventory', 'Completed',   'Medium', 'Inventory',  3, now() - INTERVAL '6 days', now() - INTERVAL '4 days',   now() - INTERVAL '4 days',   'tenant_a'),  -- on time
(4, 'Audit Inventory', 'Pending',     'High',   'Inventory',  4, now() - INTERVAL '1 day',  now() + INTERVAL '3 hours',  NULL,                        'tenant_b'),
(4, 'Store Walk',      'In Progress', 'Low',    'Store Walk', 4, now() - INTERVAL '3 days', now() - INTERVAL '5 hours',  NULL,                        'tenant_b');  -- overdue

-- ==========================================
-- 8. TENANT ISOLATION RLS ON DATA TABLES (§7.3)
-- The read-only role only ever sees rows for the tenant set on its connection
-- via SELECT set_config('app.tenant_id', <tenant>, true). missing_ok=true keeps
-- the policy from erroring when the GUC is unset (it simply returns no rows).
-- NOTE: schema reflection (information_schema / pg_catalog) is NOT affected by
-- RLS, so the startup SQLDatabase reflection works without setting app.tenant_id.
-- ==========================================
GRANT SELECT ON districts, stores, users, active_tasks TO a2a_readonly;

ALTER TABLE districts ENABLE ROW LEVEL SECURITY;
ALTER TABLE districts FORCE ROW LEVEL SECURITY;
CREATE POLICY tenant_isolation_districts ON districts
    FOR ALL USING (tenant_id = current_setting('app.tenant_id', true));

ALTER TABLE stores ENABLE ROW LEVEL SECURITY;
ALTER TABLE stores FORCE ROW LEVEL SECURITY;
CREATE POLICY tenant_isolation_stores ON stores
    FOR ALL USING (tenant_id = current_setting('app.tenant_id', true));

ALTER TABLE users ENABLE ROW LEVEL SECURITY;
ALTER TABLE users FORCE ROW LEVEL SECURITY;
CREATE POLICY tenant_isolation_users ON users
    FOR ALL USING (tenant_id = current_setting('app.tenant_id', true));

ALTER TABLE active_tasks ENABLE ROW LEVEL SECURITY;
ALTER TABLE active_tasks FORCE ROW LEVEL SECURITY;
CREATE POLICY tenant_isolation_active_tasks ON active_tasks
    FOR ALL USING (tenant_id = current_setting('app.tenant_id', true));
