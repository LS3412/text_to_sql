# A2UI Side — Technical Specification

> **Owner:** A2UI Engineer (You — Pranav)
> **Project:** Dynamic A2UI Generation Framework for Workcloud Task Companion
> **Role of this side:** Take the structured **Card** produced by the A2A side, turn it into valid **A2UI v0.8 JSON**, **validate** it, **auto-inject the corporate CSS/brand styling**, and **push it to a Flutter client** that renders it natively — with **zero UX-team dependency**.
> **Last updated:** 2026-06-05

---

## 0. Golden Rule (read this first)

> **The A2UI side NEVER touches the database and NEVER writes SQL/MQL. Its only input is the `Card` JSON (from `chat_history` rows where `message_type = 'A2UI_DISPLAY'`). Its only output is styled A2UI v0.8 JSON pushed to Flutter. The `Card` model + the `chat_history` table are the ONLY two contracts with the A2A side. Freeze them on day one.**

---

## 1. Scope & Ownership

### In scope (you own this)
- **Card → A2UI v0.8 JSON** mapping (Dynamic Generation).
- **A2UI Linter / Validator** (reject malformed cards before they reach Flutter).
- **🎨 Style Auto-Mapper** — inject corporate CSS classes/styles automatically. *(Headline deliverable.)*
- **WebSocket push** to the Flutter client.
- **Session reconstruction** (replay past `A2UI_DISPLAY` rows when a user reopens chat).
- **Zero-UX workflow** — a brand-new task type renders correctly with no designer/hard-coding.
- **Flutter renderer** integration (A2UI v0.8).
- **Locust** load/concurrency testing of the generation + push pipeline.

### Out of scope (the A2A side owns this)
- Text-to-SQL / Text-to-MQL.
- Database access, query validation, Redis, ClickHouse.
- Producing the `Card` content (you only consume it).

---

## 2. Technology Stack

| Layer | Technology | Why |
|-------|-----------|-----|
| Language | **Python 3.11+** or **Node.js 20+** | Middleware; Python pairs well with the LLM, Node with WebSockets. Pick one and stay. |
| UI generation | **LLM** prompted to emit A2UI v0.8 (a "Macaron-A2UI"-style model, or any capable model) | Dynamic Generation. The framework matters more than the exact model. |
| Structured output | **Pydantic v2** (Python) / **zod** (Node) | Enforce the `Card` and the A2UI schema |
| UI protocol | **A2UI v0.8** (see a2ui.org) | Server-Driven UI JSON the Flutter app renders |
| Style injection | Custom **Style Auto-Mapper** + brand **CSS/style token config** | Pixel-perfect, on-brand, automatic |
| Real-time transport | **WebSocket** (FastAPI WebSocket / `ws` / Socket.IO) | Push cards to the device instantly |
| Client | **Flutter** + A2UI v0.8 renderer | Renders JSON as native widgets |
| Read access to history | **PostgreSQL** read-only (the shared `chat_history`) | Replay `A2UI_DISPLAY` rows for session rebuild |
| Load testing | **Locust** | Concurrency & throughput validation |
| Observability | **OpenTelemetry** (`trace_id` passthrough) | Trace a request across both halves |

---

## 3. Components & Responsibilities

```
   chat_history (A2UI_DISPLAY rows)            ← input from A2A
            │  Card JSON
            ▼
   ┌─────────────────────────────────────────────────┐
   │  A2UI MIDDLEWARE (you build this)                │
   │                                                  │
   │  ① Card Ingestor    – read/subscribe to rows     │
   │  ② Component Mapper  – Card.card_kind → A2UI tree │
   │  ③ A2UI Generator    – build valid v0.8 JSON      │
   │  ④ Validator/Linter  – schema + safety checks     │
   │  ⑤ 🎨 Style Auto-Mapper – inject brand CSS        │
   │  ⑥ Push Service      – WebSocket → Flutter        │
   └───────────────────────────────┬─────────────────┘
                                    │ styled A2UI v0.8 JSON
                                    ▼
                          ┌────────────────────┐
                          │   Flutter Client    │
                          │  (A2UI v0.8 render) │
                          └────────┬───────────┘
                                   │ user taps action
                                   ▼
                          back to A2A Handler (new question)
```

### 3.1 Card Ingestor
- Two supported handoff modes (pick one with A2A — see §9):
  - **(Recommended) Pull/subscribe:** poll/`LISTEN` for new `A2UI_DISPLAY` rows by `tenant_id`+`session_id`. Decoupled; gives free session rebuild.
  - **Direct call:** A2A Handler calls your `/render` endpoint with the `Card`.
- Validates the incoming `Card` against the frozen Pydantic model. Rejects/asks-for-retry on mismatch.

### 3.2 Component Mapper
- Maps `Card.card_kind` (+ `data_payload` shape) → an A2UI component type. **No guessing** — the mapping is a fixed table (§6).
- Handles `card_kind = "text"` (body only, no widget) and `card_kind = "auto"` (infer from `data_payload` shape).

### 3.3 A2UI Generator
- Produces valid **A2UI v0.8 JSON**: a root `Card` with children (`SummaryCard`, `LineChartCard`, `SelectionList`, `ActionButton`, etc.).
- Binds `data_payload` rows into the component's data fields.
- Renders `suggested_actions` as `ActionButton`s that emit structured action payloads.

### 3.4 Validator / Linter
- Schema check (valid A2UI v0.8 structure).
- Safety check (no unknown components, no oversized payloads, all required fields present).
- **A malformed card NEVER reaches Flutter** — fail to a safe text card instead.

### 3.5 🎨 Style Auto-Mapper (headline deliverable)
- Walks the A2UI tree; for each component type, injects brand style tokens (colors, padding, typography, radius) from a central **style config**.
- Output is **100% on-brand, pixel-perfect, automatic** — no designer in the loop.
- Driven by a `style_tokens.json` so rebrands are one-file changes.

### 3.6 Push Service
- Maintains a WebSocket per `session_id`.
- Pushes the final styled JSON; handles reconnects, backpressure, and replay on reconnect.

---

## 4. Shared Contract #1 — `chat_history` (read-only for you)

You only ever run the **session-rebuild query** (you never write):

```sql
-- Set the agent so Row-Level Security lets you read
SET app.agent_id = 'field_user_agent';

SELECT response_payload          -- this is the Card JSON
FROM   chat_history
WHERE  tenant_id   = :tenant_id
  AND  session_id  = :session_id
  AND  message_type = 'A2UI_DISPLAY'        -- ONLY UI rows
  AND  created_at  >= now() - interval '24 hours'
ORDER BY created_at ASC;
```

> **Why this is fast:** the composite index `(agent_id, tenant_id, session_id, message_type)` is built exactly for this. You skip all tool-call/log rows.
>
> **Two must-dos or you get zero rows / leaks:**
> 1. `SET app.agent_id` on the connection (RLS), else you read nothing.
> 2. Always filter `tenant_id` (multi-tenant isolation).

---

## 5. Shared Contract #2 — The `Card` Model (FROZEN, your INPUT)

```python
from pydantic import BaseModel, Field
from typing import Any, Literal

class Card(BaseModel):
    title: str
    body: str
    data_payload: list[dict[str, Any]] = Field(default_factory=list)
    suggested_actions: list[str] = Field(default_factory=list)
    card_kind: Literal[
        "summary", "metric", "list", "ranking",
        "comparison", "trend", "alert", "confirmation", "text", "auto"
    ] = "auto"
```

### 5.1 Frozen `data_payload` shapes (your rendering depends on these)

| `card_kind` | `data_payload` shape | You render as |
|-------------|----------------------|---------------|
| `summary` | `[{"label","value","unit"}]` | `SummaryCard` |
| `metric` | `[{"label","value","unit","warning"}]` | `MetricCard` (warning style if true) |
| `list` | `[{"id","title","subtitle"}]` | `SelectionList` / `ListCard` |
| `ranking` | `[{"name","metric","rank"}]` | `StoreRankCard` |
| `comparison` | `[{"entity","metric","value"}]` | `ComparisonTableCard` |
| `trend` | `[{"date","value"}]` | `LineChartCard` |
| `alert` | `[{"id","title","severity"}]` | `WarningAlertCard` |
| `confirmation` | `[{"field","value"}]` | `ConfirmationCard` |
| `text` | `[]` | body text only |

> If A2A ever needs a new shape, **both engineers update this table together** — never one-sided.

---

## 6. Card → A2UI Component Mapping (fixed table)

```
card_kind     →  A2UI v0.8 component
─────────────────────────────────────
summary       →  Card { SummaryCard }
metric        →  Card { MetricCard }
list          →  Card { SelectionList, ActionButton("Submit") }
ranking       →  Card { StoreRankCard }
comparison    →  Card { ComparisonTableCard }
trend         →  Card { LineChartCard }
alert         →  Card { WarningAlertCard }
confirmation  →  Card { ConfirmationCard }
text          →  Card { Text(body) }
auto          →  infer from data_payload shape, else fall back to text
suggested_actions → ActionButton[] appended to every card
```

### 6.1 Example: a `summary` Card becomes A2UI v0.8 JSON
**Input Card:**
```json
{
  "title": "Store 118 Execution Summary",
  "body": "Store 118 is at 68% completion, lagging the district average of 81%.",
  "data_payload": [
    {"label": "Completion", "value": 68, "unit": "%"},
    {"label": "District Avg", "value": 81, "unit": "%"}
  ],
  "suggested_actions": ["Compare to Store 102", "Show overdue tasks"],
  "card_kind": "summary"
}
```
**Generated + styled A2UI v0.8 (abridged):**
```json
{
  "type": "Card",
  "style": "wc-card",
  "children": [
    { "type": "Text", "value": "Store 118 Execution Summary", "style": "wc-title" },
    { "type": "Text", "value": "Store 118 is at 68% completion...", "style": "wc-body" },
    { "type": "SummaryCard", "style": "wc-summary",
      "metrics": [
        {"label": "Completion",   "value": "68%", "style": "wc-metric-warn"},
        {"label": "District Avg", "value": "81%", "style": "wc-metric"}
      ]},
    { "type": "ActionButton", "label": "Compare to Store 102",
      "style": "wc-btn-secondary", "action": {"type":"ask","text":"Compare to Store 102"} },
    { "type": "ActionButton", "label": "Show overdue tasks",
      "style": "wc-btn-secondary", "action": {"type":"ask","text":"Show overdue tasks"} }
  ]
}
```
The `style` values (`wc-*`) are injected automatically by the Style Auto-Mapper — never written by hand.

---

## 7. 🎨 Style Auto-Mapper (the core deliverable, in detail)

### 7.1 Style token config (`style_tokens.json`)
```json
{
  "wc-card":          {"background": "#FFFFFF", "radius": 12, "padding": 16, "shadow": "sm"},
  "wc-title":         {"font": "Zebra-Bold", "size": 18, "color": "#1A1A1A"},
  "wc-body":          {"font": "Zebra-Regular", "size": 14, "color": "#4A4A4A"},
  "wc-metric":        {"color": "#1A1A1A", "weight": "600"},
  "wc-metric-warn":   {"color": "#D32F2F", "weight": "700"},
  "wc-btn-secondary": {"background": "#F0F0F0", "color": "#0066CC", "radius": 8, "padding": 10}
}
```

### 7.2 Algorithm
1. Receive validated A2UI tree (raw, unstyled).
2. Walk every node depth-first.
3. For each node `type`, look up the brand style class and attach `style`.
4. Apply state-based styles (e.g. `warning: true` → `wc-metric-warn`).
5. Output a fully styled tree. **Zero hand-coding, zero designer.**

### 7.3 Why this proves "Zero-UX Dependency"
A brand-new `card_kind` from A2A still flows through the same mapper. As long as it maps to a known component (or `text`), it renders **on-brand automatically** — no ticket, no mockup, no template.

---

## 8. Concurrency & Load Testing — Locust

> Goal: prove the generation + style + WebSocket push pipeline holds up when many devices receive cards at once.

### 8.1 What to test
- `/render` (Card → styled A2UI) throughput & latency.
- Concurrent **WebSocket** connections (one per active session).
- Session-rebuild query under load.
- p95/p99 generation latency.

### 8.2 `locustfile.py` (A2UI — HTTP render path)
```python
from locust import HttpUser, task, between
import random, uuid

CARDS = [
    {"title": "Store 118 Summary", "body": "68% completion.",
     "data_payload": [{"label":"Completion","value":68,"unit":"%"}],
     "suggested_actions": ["Compare to Store 102"], "card_kind": "summary"},
    {"title": "At-Risk Tasks", "body": "3 tasks at risk.",
     "data_payload": [{"id":"t1","title":"Dairy audit","severity":"high"}],
     "suggested_actions": [], "card_kind": "alert"},
    {"title": "Completion Trend", "body": "7-day trend.",
     "data_payload": [{"date":"2026-06-01","value":72},{"date":"2026-06-02","value":75}],
     "suggested_actions": [], "card_kind": "trend"},
]

class A2UIRenderUser(HttpUser):
    wait_time = between(1, 3)

    def on_start(self):
        self.session_id = str(uuid.uuid4())
        self.tenant_id  = random.choice(["tenant_a", "tenant_b"])

    @task
    def render_card(self):
        card = random.choice(CARDS)
        self.client.post("/api/v1/render", json={
            "tenant_id": self.tenant_id,
            "session_id": self.session_id,
            "card": card,
        }, name=card["card_kind"])
```

### 8.3 WebSocket load (Locust + `websocket-client`)
```python
# pip install locust websocket-client
from locust import User, task, between, events
import websocket, time, uuid

class A2UIWebSocketUser(User):
    wait_time = between(2, 5)

    def on_start(self):
        self.ws = websocket.create_connection("ws://localhost:8000/ws")
        self.session_id = str(uuid.uuid4())
        self.ws.send(f'{{"type":"subscribe","session_id":"{self.session_id}"}}')

    @task
    def receive_card(self):
        start = time.time()
        try:
            self.ws.recv()
            events.request.fire(request_type="WS", name="card_push",
                                response_time=(time.time()-start)*1000, response_length=0)
        except Exception as e:
            events.request.fire(request_type="WS", name="card_push",
                                response_time=0, response_length=0, exception=e)

    def on_stop(self):
        self.ws.close()
```

### 8.4 Run it
```bash
pip install locust websocket-client
locust -f locustfile.py --host=http://localhost:8000
# headless:
locust -f locustfile.py --host=http://localhost:8000 \
       --users 500 --spawn-rate 50 --run-time 5m --headless
```

### 8.5 Pass/fail targets (tune to hardware)
| Metric | Target |
|--------|--------|
| p95 render latency (Card → styled JSON) | < 300 ms |
| p95 WebSocket push delivery | < 150 ms |
| Concurrent WebSocket connections | ≥ 500 stable |
| Error rate | < 1 % |
| 0 dropped/duplicate cards | required |

---

## 9. Coordination with the A2A Side (lock these 5 things)

1. ✅ **The `Card` Pydantic model** — fields + types frozen (§5).
2. ✅ **The `data_payload` shapes** per `card_kind` (§5.1) — frozen jointly.
3. ✅ **Handoff mode** — recommend: A2A writes `A2UI_DISPLAY` rows, you subscribe/read (decoupled + free session rebuild).
4. ✅ **Field naming** — `llm_usage_cost` (never `cost`); shared enum values.
5. ✅ **`trace_id` passthrough** — so one request can be traced across both halves.

---

## 10. Build Phases

| Phase | Deliverable |
|-------|-------------|
| **1. Foundations** | Middleware skeleton + WebSocket + minimal Flutter renderer. **Agree & freeze `Card` + shapes with A2A engineer.** |
| **2. Mapper + Styling** | Card→A2UI Component Mapper + the **Style Auto-Mapper** + `style_tokens.json`. |
| **3. Integration** | Read `A2UI_DISPLAY` rows → generate → validate → style → push to Flutter; one full KPI end-to-end. |
| **4. KPIs + Load + Demo** | Render the 15 KPI card types; Locust suite green; demo zero-UX workflow to eng + UX teams. |

---

## 11. Pitfalls Checklist (don't ship without these)

- [ ] `Card` model + `data_payload` shapes frozen and shared as an importable module.
- [ ] Component Mapper is a fixed table — no silent guessing; unknown → `text` fallback.
- [ ] Validator runs before every push; malformed card NEVER reaches Flutter.
- [ ] Style Auto-Mapper driven by `style_tokens.json` (rebrand = one file).
- [ ] `SET app.agent_id` on the read connection (RLS) + always filter `tenant_id`.
- [ ] WebSocket handles reconnect + replay (re-run session-rebuild query).
- [ ] `text`-only cards (no `data_payload`) render gracefully.
- [ ] `trace_id` carried through to Flutter for end-to-end tracing.
- [ ] Locust targets met before demo.

---

## 12. Config / Environment

```env
# Read-only access to the shared chat_history
POSTGRES_READ_URL=postgresql://a2ui_readonly:***@localhost:5432/workcloud
APP_AGENT_ID=field_user_agent

# UI generation LLM
LLM_MODEL=claude-sonnet-4-6
A2UI_VERSION=0.8

# Transport
WS_PORT=8000
WS_PATH=/ws

# Styling
STYLE_TOKENS_PATH=./config/style_tokens.json
```
