# A2A — Implementation Notes & Spec Deviations

Tracks where this prototype intentionally differs from `A2A_Specification.md` and
records the operational steps the spec assumes. Items marked **(sign-off)** change
a stated requirement and need the partner engineer's agreement.

---

## Spec deviations

### 1. LlamaIndex `NLSQLTableQueryEngine` — RESOLVED (now used) — §2 / §3.2
The SQL Skill ([src/skills/sql_skill.py](src/skills/sql_skill.py)) now uses
`NLSQLTableQueryEngine` in **`sql_only=True`** mode: the engine GENERATES the SQL
from reflected schema (it never executes or touches tenant rows), then we run the
existing literal-aware validator + mandatory `LIMIT` and execute the SQL ourselves
on the read-only, tenant-scoped (RLS) connection.

- **Latency:** the original concern (schema-scanning per request) is avoided —
  schema reflection happens **once at startup** in a shared `SQLDatabase`
  (built in [src/main.py](src/main.py) on a sync psycopg2 readonly engine). Per
  request we build only a lightweight engine over the ClickHouse-narrowed `tables`.
- **RLS preserved:** reflection reads the catalog only (tenant-agnostic), so no GUCs
  are needed for generation; tenant isolation stays entirely in `execute_query`.
- **Custom prompt:** a `text_to_sql_prompt` keeps our safety rules — notably "do NOT
  filter by tenant_id" (the default prompt lacks this) and "no semicolon".
- Card synthesis is also LlamaIndex-native now: `LLMTextCompletionProgram` +
  `PydanticOutputParser` enforce the frozen `Card` (with a code-built text fallback).

### 2. `chat_history` partitioning uses a DEFAULT partition — §5.1
The spec's model is one range partition per day, purged by `DROP TABLE`. We keep
that model **and** add a `DEFAULT` partition (`chat_history_default`) as a safety
net so inserts never fail if the daily job hasn't run.

- Daily partitions are created by `create_daily_partition(date)` (in
  [database/schema.sql](database/schema.sql)), invoked from
  [scripts/manage_partitions.py](scripts/manage_partitions.py) via cron.
- Creating a future-dated partition while DEFAULT exists is safe (no future rows
  live in DEFAULT yet). **For production**, run the daily job and consider dropping
  the DEFAULT partition so per-day `DROP TABLE` purge works cleanly.

### 3. MQL Skill / MongoDB not implemented yet — §1 / §3.3 / §8
Out of scope for this change set. Target questions Q2–Q4 (§8) require it. The
SQL path, contracts, and caching are unaffected.

---

## Security model (how the spec's §7 rules are enforced)

- **Read-only DB user (§7.2):** the app (FastAPI) connects as the owner `a2a_user`
  to write audit rows; the SQL Skill connects as `a2a_readonly` (SELECT-only on
  `stores`/`active_tasks`) via the read-only engine in
  [config/database.py](config/database.py).
- **Tenant isolation (§7.3):** `stores` and `active_tasks` carry `tenant_id` and
  have `FORCE ROW LEVEL SECURITY`. The skill sets `app.tenant_id` per connection,
  so the LLM never needs to (and is told not to) filter by tenant.
- **Agent isolation (§5.2):** `chat_history` has `FORCE ROW LEVEL SECURITY`; every
  connection sets `app.agent_id` before reading/writing. `FORCE` is required so the
  owner role cannot silently bypass the policy.
- **Validation + LIMIT (§7.1):** `validate_sql_query()` allows only `SELECT`/`WITH`,
  blocks DML/DDL and `;`-stacking; `_apply_limit()` appends/clamps a `LIMIT` to
  `APP_MAX_RESULT_ROWS`.
- **Cache key (§3.4):** `hash(tenant_id + normalized_question + skill)` — tenant is
  always in the key. Note: the key is intentionally session-independent so repeated
  questions hit cross-user (needed for the §9.4 cache-hit-ratio target). A
  context-dependent follow-up therefore shares the tenant-level cache by design;
  add `session_id` to the key if you need per-session disambiguation.

---

## A2A protocol surface (Agent2Agent)

The agent is exposed over a **minimal, hand-rolled, spec-compliant** slice of the
open A2A protocol ([src/a2a_protocol.py](src/a2a_protocol.py), mounted in
[src/main.py](src/main.py)) — alongside, not replacing, `/api/v1/ask`:

- **Discovery:** `GET /.well-known/agent-card.json` (+ legacy `/.well-known/agent.json`)
  returns the Agent Card advertising one `text_to_sql` skill.
- **Invocation:** `POST /a2a` is a JSON-RPC 2.0 endpoint supporting **`message/send`**.
  It concatenates the message's text parts, runs them through the same orchestrator,
  and returns the `Card` as a `DataPart` Artifact inside a `completed` `Task` (plus a
  text Message = `card.body`). Unknown methods → `-32601`, bad JSON → `-32700`.
- **Identity bridge:** A2A `message.contextId` → our `session_id`; `tenant_id` /
  `user_id` / `agent_id` come from `message.metadata`, falling back to the configured
  defaults. The `A2UI_DISPLAY` row is still written to `chat_history` either way, so
  the A2UI half is unaffected by which transport the request arrived on.
- **Out of scope (later / via `a2a-sdk`):** `message/stream` (SSE), `tasks/get` + task
  store, push notifications, signed Agent Cards.

---

## First-run setup

```bash
# 1. Start infra (Postgres seeds schema.sql automatically, incl. roles/RLS/indexes)
docker compose -f docker/docker-compose.yml up -d

# 2. Pull the model into Ollama
docker exec -it a2a_ollama ollama pull qwen2:7b

# 3. Create + seed the ClickHouse catalog (otherwise it falls back to core tables)
python scripts/init_clickhouse.py

# 4. Verify everything is wired up
python scripts/setup_verify.py

# 5. (daily, via cron) pre-create tomorrow's chat_history partition
python scripts/manage_partitions.py

# 6. Run the API
uvicorn src.main:app --host 0.0.0.0 --port 8000

# 7. Load test
locust -f locustfile.py --host=http://localhost:8000
```

Seed data spans two tenants (`tenant_a`: Stores 118/202/304; `tenant_b`: Stores
500/501) so tenant isolation is observable: a query for `tenant_c` returns no rows.
