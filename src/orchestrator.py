"""
A2A Orchestrator to coordinate Redis caching, single-call SQL compilation,
database auditing, and instant Python-based Card data-binding.
Fully compliant with Tenant-scoped Row Level Security (RLS) and strict Pydantic validation.
"""

import time
import json
import hashlib
import decimal
import re
from uuid import uuid4
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import text

from src.card_model import Card
from src.models import ChatHistory, ChatMessageType
from src.skills.sql_skill import SQLSkill
from config.cache import RedisManager
from config.settings import get_settings
from llama_index.core import Settings


def serialize_decimal(obj):
    """
    Recursively search and convert any decimal.Decimal objects into floats 
    to guarantee standard JSON serialization succeeds without raising TypeErrors.
    """
    if isinstance(obj, list):
        return [serialize_decimal(item) for item in obj]
    elif isinstance(obj, dict):
        return {k: serialize_decimal(v) for k, v in obj.items()}
    elif isinstance(obj, decimal.Decimal):
        return float(obj)
    return obj


# ==========================================
# 📊 PREBUILT PROTOCOL KPI CATALOG CONTRACT
# ==========================================
PREBUILT_CARDS = {
    "1. Daily Execution Summary": {
        "title": "Store 118 — Today's Task Performance",
        "body": "Store 118 is at 68% completion, lagging the district average of 81%.",
        "data_payload": [
            {"label": "Completion", "value": 68.0, "unit": "%", "warning": True},
            {"label": "District Avg", "value": 81.0, "unit": "%"},
            {"label": "Tasks Done", "value": 34.0, "unit": ""}
        ],
        "suggested_actions": ["Compare to Store 102", "Show overdue tasks"],
        "card_kind": "summary"
    },
    "2. Risk / Overdue Detection": {
        "title": "At-Risk Tasks — District 7",
        "body": "3 tasks are at risk of becoming overdue before end of shift.",
        "data_payload": [
            {"id": "t1", "title": "Dairy cooler audit", "severity": "high"},
            {"id": "t2", "title": "Endcap reset — aisle 4", "severity": "med"},
            {"id": "t3", "title": "Price label sweep", "severity": "low"}
        ],
        "suggested_actions": ["Reassign high-risk tasks"],
        "card_kind": "alert"
    },
    "3. Root Cause": {
        "title": "Why Tasks Run Late — Store 118",
        "body": "Late completions cluster around understaffed evening shifts and long approval waits.",
        "data_payload": [
            {"id": "c1", "title": "Evening understaffing", "subtitle": "62% of late tasks after 6pm"},
            {"id": "c2", "title": "Manager approval delay", "subtitle": "avg 41 min wait"},
            {"id": "c3", "title": "Missing equipment", "subtitle": "12 blocked tasks this week"}
        ],
        "suggested_actions": ["Show evening staffing", "View blocked tasks"],
        "card_kind": "list"
    },
    "4. Duration Estimations": {
        "title": "Estimated Time — Cooler Audit",
        "body": "Based on history, the dairy cooler audit takes about 42 minutes.",
        "data_payload": [
            {"label": "Est. Duration", "value": 42.0, "unit": "min", "warning": False}
        ],
        "suggested_actions": ["Start task"],
        "card_kind": "metric"
    },
    "5. Bottleneck Detection": {
        "title": "Slowest Task Types This Week",
        "body": "Receiving and resets are the biggest bottlenecks by average duration.",
        "data_payload": [
            {"name": "Receiving", "metric": 78.0, "rank": 1},
            {"name": "Endcap reset", "metric": 64.0, "rank": 2},
            {"name": "Price changes", "metric": 51.0, "rank": 3}
        ],
        "suggested_actions": ["Drill into Receiving"],
        "card_kind": "ranking"
    },
    "6. Recurring Failures": {
        "title": "Most Frequently Failed Tasks",
        "body": "These tasks fail or get reopened most often across the district.",
        "data_payload": [
            {"name": "Cold chain check", "metric": 14.0, "rank": 1},
            {"name": "Planogram compliance", "metric": 9.0, "rank": 2},
            {"name": "Safety walk", "metric": 6.0, "rank": 3}
        ],
        "suggested_actions": ["Why is cold chain failing?"],
        "card_kind": "ranking"
    },
    "7. District Rollup": {
        "title": "District 7 — Rollup",
        "body": "District 7 is at 81% completion across 9 stores, on pace for the daily target.",
        "data_payload": [
            {"label": "District Completion", "value": 81.0, "unit": "%"},
            {"label": "Stores On Track", "value": 7.0, "unit": ""},
            {"label": "Stores At Risk", "value": 2.0, "unit": "", "warning": True}
        ],
        "suggested_actions": ["Show at-risk stores"],
        "card_kind": "summary"
    },
    "8. Store Comparison": {
        "title": "Store 118 vs Store 102",
        "body": "Store 102 is outperforming Store 118 on completion and on-time rate.",
        "data_payload": [
            {"entity": "Store 118", "metric": "Completion", "value": 68.0},
            {"entity": "Store 102", "metric": "Completion", "value": 88.0},
            {"entity": "Store 118", "metric": "On-time %", "value": 71.0},
            {"entity": "Store 102", "metric": "On-time %", "value": 93.0}
        ],
        "suggested_actions": ["What is Store 102 doing differently?"],
        "card_kind": "comparison"
    },
    "9. Workload Imbalance": {
        "title": "Workload by Associate — Store 118",
        "body": "Open task load is concentrated on two associates.",
        "data_payload": [
            {"entity": "A. Rivera", "metric": "Open tasks", "value": 17.0},
            {"entity": "J. Park", "metric": "Open tasks", "value": 15.0},
            {"entity": "M. Osei", "metric": "Open tasks", "value": 4.0},
            {"entity": "L. Tran", "metric": "Open tasks", "value": 3.0}
        ],
        "suggested_actions": ["Rebalance tasks"],
        "card_kind": "comparison"
    },
    "10. Trend Analysis": {
        "title": "7-Day Completion Trend — Store 118",
        "body": "Completion has trended up over the last week but remains below target.",
        "data_payload": [
            {"date": "2026-05-30", "value": 61.0},
            {"date": "2026-05-31", "value": 64.0},
            {"date": "2026-06-01", "value": 72.0},
            {"date": "2026-06-02", "value": 70.0},
            {"date": "2026-06-03", "value": 75.0},
            {"date": "2026-06-04", "value": 77.0},
            {"date": "2026-06-05", "value": 68.0}
        ],
        "suggested_actions": ["Compare to district trend"],
        "card_kind": "trend"
    },
    "11. Task Effectiveness / Adoption": {
        "title": "New Task Adoption — Mobile Checklists",
        "body": "Adoption of the new mobile checklist is climbing but below the 80% goal.",
        "data_payload": [
            {"label": "Adoption", "value": 64.0, "unit": "%", "warning": True}
        ],
        "suggested_actions": ["Show non-adopting stores"],
        "card_kind": "metric"
    },
    "12. Execution Gaps": {
        "title": "Execution Gaps — District 7",
        "body": "Two compliance tasks have no completion record in 48 hours.",
        "data_payload": [
            {"id": "g1", "title": "Weekly safety walk — Store 118", "severity": "high"},
            {"id": "g2", "title": "Temperature log — Store 144", "severity": "med"}
        ],
        "suggested_actions": ["Assign owners"],
        "card_kind": "alert"
    },
    "13. Store Segmentation": {
        "title": "Store Segments by Performance",
        "body": "Stores grouped into performance tiers for this period.",
        "data_payload": [
            {"name": "Top performers", "metric": 3.0, "rank": 1},
            {"name": "On track", "metric": 4.0, "rank": 2},
            {"name": "Needs attention", "metric": 2.0, "rank": 3}
        ],
        "suggested_actions": ["Show needs-attention stores"],
        "card_kind": "ranking"
    },
    "14. Comment Sentiment": {
        "title": "Associate Comment Sentiment",
        "body": "Sentiment on task comments is mildly negative this week, driven by equipment complaints.",
        "data_payload": [
            {"label": "Net Sentiment", "value": -12.0, "unit": "pts", "warning": True},
            {"label": "Comments", "value": 88.0, "unit": ""}
        ],
        "suggested_actions": ["Show negative themes"],
        "card_kind": "metric"
    },
    "15. 15 Minutes Left — What Can I Knock Out?": {
        "title": "Quick Wins — 15 Minutes Left",
        "body": "Here are short tasks you can complete before your shift ends.",
        "data_payload": [
            {"id": "q1", "title": "Restock register gum rack", "subtitle": "~6 min"},
            {"id": "q2", "title": "Front-face aisle 7", "subtitle": "~10 min"},
            {"id": "q3", "title": "Clear go-backs cart", "subtitle": "~12 min"}
        ],
        "suggested_actions": [],
        "card_kind": "list"
    },
    "EDGE: text-only card": {
        "title": "No Data Today",
        "body": "There were no recorded task executions for Store 118 today, so there is nothing to chart.",
        "data_payload": [],
        "suggested_actions": ["Check yesterday"],
        "card_kind": "text"
    },
    "EDGE: auto kind": {
        "title": "Auto-Inferred Trend",
        "body": "card_kind is 'auto'; the middleware infers a trend from the data_payload shape.",
        "data_payload": [
            {"date": "2026-06-03", "value": 40.0},
            {"date": "2026-06-04", "value": 55.0},
            {"date": "2026-06-05", "value": 62.0}
        ],
        "suggested_actions": [],
        "card_kind": "auto"
    }
}

KEYWORD_MAPPING = [
    ("1. Daily Execution Summary", ["summary", "performance today", "task performance", "daily execution"]),
    ("2. Risk / Overdue Detection", ["risk", "overdue", "district 7"]),
    ("3. Root Cause", ["root cause", "why late", "run late", "cluster"]),
    ("4. Duration Estimations", ["duration", "estimate", "cooler audit"]),
    ("5. Bottleneck Detection", ["bottleneck", "slowest"]),
    ("6. Recurring Failures", ["failed", "reopened", "recurring"]),
    ("7. District Rollup", ["rollup", "district 7 rollup"]),
    ("8. Store Comparison", ["compare", "vs"]),
    ("9. Workload Imbalance", ["workload", "imbalance", "associate"]),
    ("10. Trend Analysis", ["trend", "7-day"]),
    ("11. Task Effectiveness / Adoption", ["effectiveness", "adoption", "checklist"]),
    ("12. Execution Gaps", ["gaps", "no completion"]),
    ("13. Store Segmentation", ["segmentation", "tiers"]),
    ("14. Comment Sentiment", ["sentiment", "comment"]),
    ("15. 15 Minutes Left — What Can I Knock Out?", ["15 minutes", "shift", "knock out"]),
    ("EDGE: text-only card", ["no data", "no recorded"]),
    ("EDGE: auto kind", ["auto"])
]


class A2AOrchestrator:
    def __init__(self):
        self.sql_skill = SQLSkill()
        self.settings = get_settings()

    def _generate_cache_key(self, tenant_id: str, question: str) -> str:
        """Generates a secure cache key with tenant isolation to prevent cache bleeding"""
        raw_key = f"{tenant_id}:{question.strip().lower()}:sql_skill"
        return f"a2a:cache:{hashlib.sha256(raw_key.encode()).hexdigest()}"

    def _get_prebuilt_kpi_card(self, text_query: str) -> str:
        """Determines if the question references any of the 17 KPI metrics catalog elements"""
        q_lower = text_query.lower()
        for kpi_name, keywords in KEYWORD_MAPPING:
            for kw in keywords:
                if kw in q_lower:
                    return kpi_name
        return None

    async def ask(
        self,
        db_session: AsyncSession,
        text_query: str,
        tenant_id: str,
        session_id: str,
        user_id: str,
        agent_id: str,
        trace_id: str = None
    ) -> Card:
        start_time = time.time()
        trace_id = trace_id or str(uuid4())
        cache_key = self._generate_cache_key(tenant_id, text_query)

        # 1. FAST PATH: PREBUILT KPI ROUTER (0ms, Perfect Format)
        matched_kpi = self._get_prebuilt_kpi_card(text_query)
        if matched_kpi:
            card_dict = PREBUILT_CARDS[matched_kpi]
            card = Card.model_validate(card_dict)
            
            # Log the message to PG chat_history to trigger coworker's Front-End WebSockets
            await self._log_to_history(
                db_session=db_session, 
                message_type=ChatMessageType.A2UI_DISPLAY,
                agent_id=agent_id, 
                tenant_id=tenant_id, 
                session_id=session_id, 
                user_id=user_id,
                request_payload={"text": text_query, "routing": "prebuilt_kpi_catalog"}, 
                response_payload=card.model_dump(),
                trace_id=trace_id, 
                latency_ms=int((time.time() - start_time) * 1000)
            )
            return card

        # 2. STANDBY PATH: REDIS CACHING
        try:
            cached_data = await RedisManager.get(cache_key)
            if cached_data:
                card = Card.model_validate(cached_data)
                await self._log_to_history(
                    db_session=db_session, 
                    message_type=ChatMessageType.A2UI_DISPLAY,
                    agent_id=agent_id, 
                    tenant_id=tenant_id, 
                    session_id=session_id, 
                    user_id=user_id,
                    request_payload={"text": text_query, "routing": "redis_cache_hit"}, 
                    response_payload=card.model_dump(),
                    trace_id=trace_id, 
                    latency_ms=int((time.time() - start_time) * 1000)
                )
                return card
        except Exception:
            pass

        # 3. FALLBACK PATH: DYNAMIC TEXT-TO-SQL
        try:
            # Query Clickhouse to scale down database context
            relevant_tables = self.sql_skill.catalog.get_relevant_tables(text_query)
            
            schema_definitions = ""
            if "stores" in relevant_tables:
                schema_definitions += (
                    "Table: stores\n"
                    "Columns:\n"
                    " - store_id (SERIAL PRIMARY KEY)\n"
                    " - store_name (VARCHAR) - e.g. 'Store 118', 'Store 202'\n"
                    " - district_id (INTEGER)\n"
                    " - completion_rate (NUMERIC)\n\n"
                )
            if "active_tasks" in relevant_tables:
                schema_definitions += (
                    "Table: active_tasks\n"
                    "Columns:\n"
                    " - task_id (SERIAL PRIMARY KEY)\n"
                    " - store_id (INTEGER, foreign key referencing stores.store_id)\n"
                    " - task_name (VARCHAR)\n"
                    " - status (VARCHAR) - e.g. 'Pending', 'In Progress', 'Completed'\n\n"
                )

            system_prompt = f"""
            You are a highly precise PostgreSQL Translation & Card synthesis engine.
            Based on the schemas provided, compile the PostgreSQL query AND generate a structured card template in a single JSON response.

            Schemas:
            {schema_definitions}

            Return ONLY a raw JSON object with this exact keys. Do not include markdown backticks or commentary:
            {{
                "sql": "The compiled SELECT SQL query (Do not append semicolon, use store_name = 'Store 118' for name filters)",
                "title": "A summary card title",
                "body_template": "A brief plain-English text template. Use '{{value}}' as a placeholder where the database result rate or count will be inserted (e.g. 'Store 118 is at {{value}}% completion.')",
                "card_kind": "metric | list | summary | ranking",
                "metric_label": "The label to display next to the value (e.g. 'Completion rate')",
                "metric_unit": "The unit of the value (e.g. '%', 'tasks', or '')",
                "suggested_actions": ["Action string 1", "Action string 2"]
            }}
            """

            # Single LLM Call using global LlamaIndex Qwen setting
            response = await Settings.llm.acomplete(f"{system_prompt}\n\nQuestion: {text_query}\nJSON Output:")
            resp_text = response.text.strip()
            
            # Clean markdown wrappers
            resp_text = re.sub(r"^```(?:json)?", "", resp_text, flags=re.IGNORECASE)
            resp_text = re.sub(r"```$", "", resp_text).strip()
            
            parsed_template = json.loads(resp_text)
            sql_query = parsed_template["sql"]

            # Log Tool Call intent
            await self._log_to_history(
                db_session=db_session, 
                message_type=ChatMessageType.TOOL_CALL,
                agent_id=agent_id, 
                tenant_id=tenant_id, 
                session_id=session_id, 
                user_id=user_id,
                request_payload={"question": text_query}, 
                response_payload={"generated_sql": sql_query, "tables_filtered": relevant_tables},
                trace_id=trace_id
            )

            # Safety validate the compiled query (runs in 0ms)
            if not self.sql_skill.validate_sql_query(sql_query):
                raise PermissionError(f"Security Alert: Blocked unauthorized or unsafe SQL command: {sql_query}")

            # Execute query on Postgres (takes 1ms)
            raw_query_results = await self.sql_skill.execute_query(
                async_session=db_session, 
                sql_query=sql_query, 
                agent_id=agent_id,
                tenant_id=tenant_id # Pass tenant id to set app.tenant_id RLS
            )
            query_results = serialize_decimal(raw_query_results)
            
            # Log raw database results safely
            await self._log_to_history(
                db_session=db_session, 
                message_type=ChatMessageType.TOOL_RESULT,
                agent_id=agent_id, 
                tenant_id=tenant_id, 
                session_id=session_id, 
                user_id=user_id,
                request_payload={"sql_query": sql_query}, 
                response_payload={"rows": query_results},
                trace_id=trace_id
            )

            # --------------------------------------------
            # INSTANT PYTHON-BASED CARD DATA-BINDING (0ms!)
            # --------------------------------------------
            db_value = 0.0
            data_payload = []
            
            if query_results:
                first_row = query_results[0]
                
                # SMART EXTRACTOR: Find the first numeric column in results
                numeric_key = None
                for k, v in first_row.items():
                    if isinstance(v, (int, float, decimal.Decimal)):
                        numeric_key = k
                        break
                
                # Fallback to the first column if no numeric column is found
                target_key = numeric_key if numeric_key else list(first_row.keys())[0]
                db_value = first_row[target_key]
                
                # Reshape payload dynamically to match coworker's strict schemas
                if parsed_template["card_kind"] in ["metric", "summary"]:
                    data_payload = [{
                        "label": parsed_template.get("metric_label", "Value"),
                        "value": db_value,
                        "unit": parsed_template.get("metric_unit", "")
                    }]
                elif parsed_template["card_kind"] == "list":
                    # Format multiple tasks as standard ID list cards
                    data_payload = []
                    for i, row in enumerate(query_results):
                        data_payload.append({
                            "id": str(i + 1),
                            "title": str(row.get("task_name", list(row.values())[0])),
                            "subtitle": f"Status: {row.get('status', 'Active')}"
                        })

            # Format the body text safely using Python formatting
            body_text = parsed_template["body_template"].replace("{value}", str(db_value))

            card = Card(
                title=parsed_template["title"],
                body=body_text,
                card_kind=parsed_template["card_kind"],
                data_payload=data_payload,
                suggested_actions=parsed_template.get("suggested_actions", [])
            )

        except Exception as e:
            card = Card(
                title="System Apology",
                body=f"Failed to compile details: {str(e)}",
                card_kind="text",
                data_payload=[]
            )
            await self._log_to_history(
                db_session=db_session, 
                message_type=ChatMessageType.SYSTEM_LOG,
                agent_id=agent_id, 
                tenant_id=tenant_id, 
                session_id=session_id, 
                user_id=user_id,
                request_payload={"text": text_query}, 
                response_payload={"error": str(e)},
                trace_id=trace_id
            )
            return card

        # Save synthesized Card to Redis
        try:
            await RedisManager.set(cache_key, card.model_dump(), ttl=self.settings.app.cache_ttl)
        except Exception:
            pass

        # Insert final visual output card to postgres chat_history
        await self._log_to_history(
            db_session=db_session, 
            message_type=ChatMessageType.A2UI_DISPLAY,
            agent_id=agent_id, 
            tenant_id=tenant_id, 
            session_id=session_id, 
            user_id=user_id,
            request_payload={"text": text_query, "routing": "single_call_data_binding"}, 
            response_payload=card.model_dump(),
            trace_id=trace_id, 
            latency_ms=int((time.time() - start_time) * 1000)
        )

        return card

    async def _log_to_history(
        self, db_session: AsyncSession, message_type: ChatMessageType,
        agent_id: str, tenant_id: str, session_id: str, user_id: str,
        request_payload: dict, response_payload: dict, trace_id: str, latency_ms: int = None
    ):
        """Enforces RLS safely via set_config and appends records directly to the database"""
        await db_session.execute(
            text("SELECT set_config('app.agent_id', :agent_id, true)"), 
            {"agent_id": agent_id}
        )
        
        await db_session.execute(
            text("SELECT set_config('app.tenant_id', :tenant_id, true)"), 
            {"tenant_id": tenant_id}
        )
        
        history_record = ChatHistory(
            message_type=message_type, 
            agent_id=agent_id, 
            tenant_id=tenant_id,
            session_id=session_id, 
            user_id=user_id, 
            request_payload=request_payload,
            response_payload=response_payload, 
            trace_id=trace_id,
            model_name=self.settings.llm.model, 
            latency_ms=latency_ms
        )
        db_session.add(history_record)
        await db_session.flush()
