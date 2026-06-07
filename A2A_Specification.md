# A2A Side — Technical Specification

> **Owner:** A2A Engineer (Partner)
> **Project:** Workcloud Field Insights AI Agent
> **Role of this side:** Turn a user's natural-language question into the correct database query (SQL **or** MongoDB MQL), execute it, and synthesize the result into a strict, frozen **Card** JSON that the A2UI side renders.
> **Last updated:** 2026-06-05

---

## 0. Golden Rule (read this first)

> **The A2A side NEVER builds UI. It only produces a `Card` JSON and writes it to `chat_history` as an `A2UI_DISPLAY` row. The A2UI side never touches the database. The `Card` model + the `chat_history` table are the ONLY two contracts between the two halves. Freeze them on day one and do not change them without the other engineer's sign-off.**

---

## 1. Scope & Ownership

### In scope (you own this)
- Natural language → **SQL** (structured/current data) — Text-to-SQL.
- Natural language → **MQL** (event/historical data) — Text-to-MQL.
- Query **validation** and **safe execution** (read-only).
- **Handler/Orchestrator** with conversation memory and skill routing.
- **Synthesis** of raw rows into the frozen `Card` JSON.
- Writing every event into `chat_history` (the shared bus).
- **Redis** caching, **ClickHouse** metadata catalog.
- **Locust** load/concurrency testing of all A2A endpoints.

### Out of scope (the A2UI side owns this)
- A2UI v0.8 JSON generation.
- CSS / Style Auto-Mapper.
- WebSocket push to Flutter.
- Flutter rendering.

---

## 2. Technology Stack

| Layer | Technology | Why |
|-------|-----------|-----|
| Language | **Python 3.11+** | Best ecosystem for LLM agents |
| Agent framework | **LlamaIndex** (`NLSQLTableQueryEngine`, agentic routing) | Required choice; MindsDB rejected as over-complicated |
| LLM | Configurable (e.g. GPT/Claude/Llama via an LLM gateway) | Generates SQL/MQL + synthesizes Card text |
| Structured output | **Pydantic v2** | Enforces the strict `Card` schema |
| Structured / current DB | **PostgreSQL** (stand-in for AlloyDB — AlloyDB *is* Postgres) | Stores, Users, ActiveTasks, hierarchies, `chat_history` |
| Event / historical DB | **MongoDB** (read replica) | TaskExecutionLogs, Comments, durations, timestamps |
| Metadata catalog | **ClickHouse** | Table/column descriptions so the LLM picks the right tables when there are thousands |
| Cache | **Redis** | Question→answer caching; lower latency & cost |
| API | **FastAPI** | Serve the Handler over HTTP/WebSocket |
| Load testing | **Locust** | Concurrency & throughput validation |
| Archival (optional) | **GCP Bucket** | Cold storage of purged partitions for future RL |
| Observability | **OpenTelemetry** (`trace_id`) | Distributed tracing across services |

> **Database simplification for the prototype:** Run **PostgreSQL + MongoDB + Redis**. Add **ClickHouse only when** the real table count is large. Do not run five databases just for a demo.

---

## 3. Components & Responsibilities

```
                 ┌───────────────────────────────────────────────┐
   User text ──▶ │  HANDLER / ORCHESTRATOR (LlamaIndex)           │
   + context     │  - holds conversation memory (per session_id) │
                 │  - routes to the right skill(s)               │
                 │  - synthesizes final Card                     │
                 └───┬───────────────┬───────────────┬───────────┘
        1. check     │      2. lookup │       3. run  │
           cache     ▼        catalog ▼         skill ▼
              ┌──────────┐   ┌────────────┐   ┌──────────────────────┐
              │  REDIS   │   │ CLICKHOUSE │   │  SKILLS (Workers)     │
              │ (cache)  │   │ (catalog)  │   │  - SQL Skill          │
              └──────────┘   └────────────┘   │  - MQL Skill          │
                                              └──────┬───────┬───────┘
                                          PostgreSQL ▼       ▼ MongoDB
                                          ┌───────────┐  ┌───────────┐
                                          │ AlloyDB / │  │  Mongo    │
                                          │ Postgres  │  │  replica  │
                                          └───────────┘  └───────────┘
                                                  │
                              4. INSERT Card row  ▼
                                          ┌────────────────────┐
                                          │   chat_history      │ ──▶ A2UI side reads this
                                          │ (message_type=      │
                                          │  A2UI_DISPLAY)      │
                                          └────────────────────┘
```

### 3.1 Handler / Orchestrator
- Receives `{ text, tenant_id, session_id, user_id, agent_id }`.
- Loads conversation memory for the `session_id` (so "What about yesterday?" works).
- **Routing logic:**
  - Structured/current data (assignments, hierarchies, roles, static metadata) → **SQL Skill**.
  - Event/historical data (execution logs, durations, comments, status timestamps) → **MQL Skill**.
  - Needs both → call both, then merge.
- Synthesizes the merged result + a natural-language `body` into the frozen `Card`.
- Writes every step into `chat_history` with the correct `message_type`.

### 3.2 SQL Skill (Text-to-SQL)
- Uses LlamaIndex `NLSQLTableQueryEngine` against PostgreSQL/AlloyDB.
- Pulls relevant table metadata from the **ClickHouse catalog** first (so the prompt only includes relevant tables).
- **Validates** generated SQL before running (see §7).
- Executes with a **read-only DB user**.
- Returns rows to the Handler.

### 3.3 MQL Skill (Text-to-MQL)
- Generates a **MongoDB aggregation pipeline** from natural language.
- Validates the pipeline (no `$out`, `$merge`, no writes).
- Executes against the **read replica only**.
- Returns documents to the Handler.

### 3.4 Redis Cache
- Key = `hash(tenant_id + normalized_question + skill)`. **`tenant_id` MUST be in the key** (else cross-tenant leak).
- On hit → return cached Card instantly (still log an `A2UI_DISPLAY` row).
- TTL configurable (e.g. 5–15 min for live metrics).

### 3.5 ClickHouse Catalog
- Stores per-table metadata: `table_name`, `column_name`, `description`, `db_source (sql|mongo)`, `example_values`.
- Queried before SQL/MQL generation so the LLM sees only relevant tables — solves the "thousands of tables" problem.

---

## 4. Data Sources Map

| Store | Holds | Queried by |
|-------|-------|-----------|
| **PostgreSQL / AlloyDB** | Stores, Users, ActiveTasks, hierarchies, static project metadata, **`chat_history`** | SQL Skill |
| **MongoDB** (read replica) | TaskExecutionLogs, Comments, durations, status-transition timestamps, historical averages | MQL Skill |
| **ClickHouse** | Metadata catalog of all tables/collections | Pre-generation lookup |
| **Redis** | Cached question→Card pairs | Pre-LLM lookup |

---

## 5. The Shared Contract #1 — `chat_history` Table

> This table is the **message bus + audit log + UI source of truth**. It lives in PostgreSQL. Both sides depend on it.

```sql
-- Message type enum (the fast-filter key)
CREATE TYPE chat_message_type AS ENUM (
    'A2UI_DISPLAY',   -- final answer for the UI  ← A2UI side reads ONLY these
    'TOOL_CALL',      -- agent calling a DB skill
    'TOOL_RESULT',    -- data returned from a tool
    'AGENT_INTERNAL', -- internal reasoning, not for display
    'SYSTEM_LOG'      -- system log / error
);

CREATE TABLE chat_history (
    id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    message_type    chat_message_type NOT NULL,

    -- Isolation
    agent_id        VARCHAR(255) NOT NULL,
    tenant_id       VARCHAR(255) NOT NULL,

    -- Threading
    session_id      VARCHAR(255) NOT NULL,
    user_id         VARCHAR(255),
    parent_id       UUID REFERENCES chat_history(id) ON DELETE SET NULL,

    -- Payloads
    request_payload  JSONB,
    response_payload JSONB NOT NULL,   -- the Card lives here for A2UI_DISPLAY rows

    -- Observability
    trace_id        VARCHAR(255),
    model_name      VARCHAR(255),
    llm_usage_cost  NUMERIC(10, 8),    -- NAME FROZEN: use llm_usage_cost everywhere
    latency_ms      INTEGER,

    -- Feedback & metadata
    feedback        JSONB,
    metadata        JSONB
) PARTITION BY RANGE (created_at);

-- Indexes
CREATE INDEX idx_chat_history_a2ui_filter
    ON chat_history (agent_id, tenant_id, session_id, message_type);
CREATE INDEX idx_chat_history_created_at ON chat_history (created_at);
CREATE INDEX idx_chat_history_user_id    ON chat_history (user_id);
CREATE INDEX idx_chat_history_trace_id   ON chat_history (trace_id);
CREATE INDEX idx_chat_history_feedback_gin ON chat_history USING GIN (feedback);

-- Row-Level Security (agent isolation)
ALTER TABLE chat_history ENABLE ROW LEVEL SECURITY;
CREATE POLICY agent_isolation_policy ON chat_history
    FOR ALL USING (agent_id = current_setting('app.agent_id'));
```

> **NAMING FIX (frozen):** The original draft had both `cost` and `llm_usage_cost`. **We standardize on `llm_usage_cost`.** Do not reintroduce `cost`.

### 5.1 Partition automation (MANDATORY — or inserts fail at midnight)
A scheduled job must create **tomorrow's** partition **today**:
```sql
CREATE TABLE chat_history_y2026m06d06 PARTITION OF chat_history
    FOR VALUES FROM ('2026-06-06 00:00:00+00') TO ('2026-06-07 00:00:00+00');
```
Purge old data instantly (no DELETE scans):
```sql
DROP TABLE chat_history_y2026m05d31;   -- optional: archive to GCP first
```

### 5.2 Connection requirement (or every query returns nothing)
Every DB connection must set the agent before querying, or RLS hides all rows:
```sql
SET app.agent_id = 'field_user_agent';
```

---

## 6. The Shared Contract #2 — The `Card` Model (FROZEN)

> Every `A2UI_DISPLAY` row's `response_payload` MUST be exactly this shape. The A2UI side renders strictly from it.

```python
from pydantic import BaseModel, Field
from typing import Any, Literal

class Card(BaseModel):
    title: str = Field(..., description="Card heading, e.g. 'Store 118 Execution Summary'")
    body: str  = Field(..., description="Plain-English summary of the answer")
    # data_payload SHAPES are frozen per card_kind (see table below)
    data_payload: list[dict[str, Any]] = Field(default_factory=list,
        description="The raw metrics rows used to draw charts/tables/lists")
    suggested_actions: list[str] = Field(default_factory=list,
        description="Follow-up questions, rendered as tappable buttons")
    # Hint to the A2UI side which component to render. If unsure, send 'auto'.
    card_kind: Literal[
        "summary", "metric", "list", "ranking",
        "comparison", "trend", "alert", "confirmation", "text", "auto"
    ] = "auto"
```

### 6.1 Frozen `data_payload` shapes (so A2UI can auto-pick a component)

| `card_kind` | `data_payload` shape | Example |
|-------------|----------------------|---------|
| `summary` | `[{"label": str, "value": number, "unit": str}]` | `[{"label":"Completion","value":68,"unit":"%"}]` |
| `metric` | `[{"label": str, "value": number, "unit": str, "warning": bool}]` | single metric |
| `list` | `[{"id": str, "title": str, "subtitle": str}]` | task list |
| `ranking` | `[{"name": str, "metric": number, "rank": int}]` | bottom-3 stores |
| `comparison` | `[{"entity": str, "metric": str, "value": number}]` | Store A vs B |
| `trend` | `[{"date": "YYYY-MM-DD", "value": number}]` | line chart |
| `alert` | `[{"id": str, "title": str, "severity": "low\|med\|high"}]` | at-risk tasks |
| `confirmation` | `[{"field": str, "value": str}]` | created-task summary |
| `text` | `[]` (empty — body only) | plain answer |

> **Rule:** If you cannot fill a known shape, set `card_kind = "text"` and put the answer in `body`. Never invent a new shape without updating this table jointly.

---

## 7. Validation & Security (non-negotiable)

1. **Never execute raw LLM SQL/MQL blindly.**
   - SQL: allow `SELECT` only; reject `INSERT/UPDATE/DELETE/DROP/ALTER/;`-stacking; enforce `LIMIT`.
   - MQL: reject `$out`, `$merge`, `$function`; read replica only.
2. **Read-only DB users** for both SQL and Mongo skills.
3. **Always filter** `tenant_id` AND `session_id` in every query.
4. **Set `app.agent_id`** on every connection (RLS).
5. **Redis key includes `tenant_id`** (no cross-tenant cache bleed).
6. **Validate the Card** with Pydantic before insert — reject malformed output and retry the LLM.
7. **Timeouts** on every DB/LLM call; fail to a `card_kind="text"` apology card, never a crash.

---

## 8. The 4 Target Questions (must pass)

| # | Question | Route | Card |
|---|----------|-------|------|
| 1 | "Give me a summary of my store's task performance today." | SQL | `summary` |
| 2 | "What tasks are at risk of becoming overdue in my district?" | SQL + MQL | `alert` |
| 3 | "Why are tasks being completed late in Store 118?" | SQL + MQL | `list`/`text` |
| 4 | "I have 15 minutes left in my shift — what task can I knock out?" | MQL | `list` |

(Plus the broader insight set: Daily Execution Summary, Risk/Overdue Detection, Root Cause, Duration Estimations, Bottleneck Detection, Recurring Failures, District Rollup, Store Comparison, Workload Imbalance, Trend Analysis, Task Effectiveness/Adoption, Execution Gaps, Store Segmentation, Comment Sentiment.)

---

## 9. Concurrency & Load Testing — Locust

> Goal: prove the Handler, skills, cache, and DB pool survive many frontline workers querying at once.

### 9.1 What to test
- Handler endpoint under N concurrent users.
- Cache hit ratio under load (repeat questions).
- DB connection pool saturation (Postgres + Mongo).
- p95 / p99 latency vs the `latency_ms` budget.

### 9.2 `locustfile.py` (A2A)
```python
from locust import HttpUser, task, between
import random, uuid

TENANTS  = ["tenant_a", "tenant_b", "tenant_c"]
QUESTIONS = [
    "Give me a summary of my store's task performance today.",
    "What tasks are at risk of becoming overdue in my district?",
    "Why are tasks being completed late in Store 118?",
    "I have 15 minutes left in my shift — what task can I knock out?",
]

class FieldAgentUser(HttpUser):
    wait_time = between(1, 4)        # think-time between questions

    def on_start(self):
        self.tenant_id  = random.choice(TENANTS)
        self.session_id = str(uuid.uuid4())
        self.user_id    = str(uuid.uuid4())

    @task(3)
    def ask_cached(self):            # repeat question → should hit Redis
        self._ask("Give me a summary of my store's task performance today.")

    @task(1)
    def ask_random(self):            # varied → exercises LLM + DB
        self._ask(random.choice(QUESTIONS))

    def _ask(self, text):
        self.client.post("/api/v1/ask", json={
            "text": text,
            "tenant_id": self.tenant_id,
            "session_id": self.session_id,
            "user_id": self.user_id,
            "agent_id": "field_user_agent",
        }, name=text[:30])
```

### 9.3 Run it
```bash
pip install locust
locust -f locustfile.py --host=http://localhost:8000
# open http://localhost:8089 — set users (e.g. 200) and spawn rate (e.g. 20/s)
# headless example:
locust -f locustfile.py --host=http://localhost:8000 \
       --users 200 --spawn-rate 20 --run-time 5m --headless
```

### 9.4 Pass/fail targets (tune to hardware)
| Metric | Target |
|--------|--------|
| p95 latency (cache hit) | < 200 ms |
| p95 latency (full LLM+DB) | < 4 s |
| Error rate | < 1 % |
| Cache hit ratio (repeat load) | > 70 % |
| 0 DB pool exhaustion errors | required |

---

## 10. Build Phases

| Phase | Deliverable |
|-------|-------------|
| **1. Foundations** | Postgres + `chat_history` schema + partition job; Mongo mock; Redis up. **Agree & freeze `Card` + shapes with A2UI engineer.** |
| **2. Skills** | SQL Skill + MQL Skill (LlamaIndex), each validated & read-only, with ClickHouse catalog lookup + Redis cache. |
| **3. Handler** | Routing agent with session memory; writes valid `Card` rows to `chat_history`. |
| **4. KPIs + Load** | All target questions pass; Locust suite green; demo to platform team. |

---

## 11. Pitfalls Checklist (don't ship without these)

- [ ] `Card` model frozen and shared as an importable module.
- [ ] `llm_usage_cost` used everywhere (never `cost`).
- [ ] Tomorrow's partition auto-created daily.
- [ ] `SET app.agent_id` on every connection.
- [ ] Every query filters `tenant_id` + `session_id`.
- [ ] Redis key includes `tenant_id`.
- [ ] SQL/MQL validated; read-only users; LIMIT enforced.
- [ ] Card validated by Pydantic before insert; malformed → retry → text fallback.
- [ ] Timeouts + graceful `text` fallback card on any failure.
- [ ] Locust targets met before demo.

---

## 12. Config / Environment

```env
POSTGRES_URL=postgresql://readonly_user:***@localhost:5432/workcloud
MONGO_REPLICA_URL=mongodb://readonly_user:***@localhost:27017/?readPreference=secondary
CLICKHOUSE_URL=http://localhost:8123
REDIS_URL=redis://localhost:6379/0
LLM_MODEL=claude-sonnet-4-6
CACHE_TTL_SECONDS=600
QUERY_TIMEOUT_SECONDS=8
APP_AGENT_ID=field_user_agent
```
