-- ==========================================
-- 1. CLEAN REBUILD: DROP EXISTING TABLES
-- ==========================================
DROP TABLE IF EXISTS active_tasks CASCADE;
DROP TABLE IF EXISTS stores CASCADE;
DROP TABLE IF EXISTS chat_history CASCADE;
DROP TYPE IF EXISTS chat_message_type CASCADE;

-- ==========================================
-- 2. CREATE SCHEMAS & ENUMS
-- ==========================================
CREATE TYPE chat_message_type AS ENUM (
    'A2UI_DISPLAY',   -- Final output JSON data for the UI
    'TOOL_CALL',      -- Agent calling a DB skill
    'TOOL_RESULT',    -- Data returned from database
    'AGENT_INTERNAL', -- Internal reasoning / chain of thought
    'SYSTEM_LOG'      -- Exception and error states
);

-- ==========================================
-- 3. CREATE PARTITIONED AUDIT TABLE
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

-- Recreate default partition range for immediate testing
CREATE TABLE chat_history_default PARTITION OF chat_history DEFAULT;

-- ==========================================
-- 4. ROW-LEVEL SECURITY (RLS) POLICIES
-- ==========================================
ALTER TABLE chat_history ENABLE ROW LEVEL SECURITY;

CREATE POLICY agent_isolation_policy ON chat_history
    FOR ALL USING (agent_id = current_setting('app.agent_id'));

-- ==========================================
-- 5. CREATE CORE DATA TABLES
-- ==========================================
CREATE TABLE stores (
    store_id SERIAL PRIMARY KEY,
    store_name VARCHAR(100) NOT NULL,
    district_id INTEGER NOT NULL,
    completion_rate NUMERIC(5, 2) NOT NULL
);

-- Add column comment descriptors for ClickHouse dynamic metadata indexing
COMMENT ON COLUMN stores.store_id IS 'Unique identifier of the physical store';
COMMENT ON COLUMN stores.store_name IS 'The name of the retail store';
COMMENT ON COLUMN stores.district_id IS 'ID of the regional district';
COMMENT ON COLUMN stores.completion_rate IS 'The overall audit task completion percentage of the store';

CREATE TABLE active_tasks (
    task_id SERIAL PRIMARY KEY,
    store_id INTEGER REFERENCES stores(store_id) ON DELETE CASCADE,
    task_name VARCHAR(150) NOT NULL,
    status VARCHAR(50) NOT NULL
);

COMMENT ON COLUMN active_tasks.task_id IS 'Unique task identifier';
COMMENT ON COLUMN active_tasks.store_id IS 'ID of the store this audit task belongs to';
COMMENT ON COLUMN active_tasks.task_name IS 'The name or description of the audit task';
COMMENT ON COLUMN active_tasks.status IS 'Current completion status of the task (Pending, In Progress, Completed)';

-- ==========================================
-- 6. SEED MOCK DATA RECORDS
-- ==========================================
INSERT INTO stores (store_name, district_id, completion_rate) VALUES
('Store 118', 1, 95.50),
('Store 202', 1, 82.10),
('Store 304', 2, 88.00);

INSERT INTO active_tasks (store_id, task_name, status) VALUES
(1, 'Audit Inventory', 'In Progress'),
(1, 'Display Setup', 'Completed'),
(2, 'Restock Items', 'Pending');
