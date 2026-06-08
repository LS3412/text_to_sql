"""
A2A Orchestrator to coordinate Redis caching, Text-to-SQL execution,
database auditing, and structured UI Card synthesis via Qwen2:7b.
Enforces real-time database facts mapped to strict UI contracts.
"""

import time
import json
import hashlib
import decimal
from uuid import uuid4
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import text

from src.card_model import Card
from src.models import ChatHistory, ChatMessageType
from src.skills.sql_skill import SQLSkill
from config.cache import RedisManager
from config.settings import get_settings
from llama_index.core import Settings
from llama_index.core.program import LLMTextCompletionProgram
from llama_index.core.output_parsers import PydanticOutputParser


# Card synthesis prompt. PydanticOutputParser appends JSON-schema/format
# instructions for the Card model automatically, so we only describe the
# semantics + the per-card_kind data_payload shapes the A2UI side expects.
_CARD_PROMPT_TMPL = """\
Translate the user question and the raw database rows into a structured UI Card.

You MUST map and reshape raw database column names (like 'store_name',
'completion_rate') into the standardized keys shown below — never use raw DB
column names as keys in data_payload.
{history_block}
User Question: {question}
Database Results: {results}

STRICT LAYOUT RULES:
1. For a single numeric value use card_kind = "metric". Prefer a concrete kind
   over "auto" whenever the data allows it.
2. Always generate 2 to 3 relevant 'suggested_actions' as follow-up question
   strings (e.g. ["Compare to Store 202", "Show active tasks"]).

Data layout rules per card_kind (FOLLOW STRICTLY):
- "summary" or "metric": data_payload is a list of {{"label": str, "value": number, "unit": str}}
  e.g. [{{"label": "Completion rate", "value": 95.5, "unit": "%"}}]
- "list": list of {{"id": str, "title": str, "subtitle": str}}
- "ranking": list of {{"name": str, "metric": number, "rank": int}}
- "comparison": list of {{"entity": str, "metric": str, "value": number}}
- "trend": list of {{"date": "YYYY-MM-DD", "value": number}}
- "alert": list of {{"id": str, "title": str, "severity": "low"|"med"|"high"}}
- "confirmation": list of {{"field": str, "value": str}}
- "text": data_payload MUST be empty: []
"""


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


class A2AOrchestrator:
    def __init__(self):
        self.sql_skill = SQLSkill()
        self.settings = get_settings()

    def _generate_cache_key(self, tenant_id: str, question: str) -> str:
        """Generates a secure cache key with tenant isolation to prevent cache bleeding"""
        raw_key = f"{tenant_id}:{question.strip().lower()}:sql_skill"
        return f"a2a:cache:{hashlib.sha256(raw_key.encode()).hexdigest()}"

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

        # 1. STANDBY PATH: REDIS CACHING
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

        # 2. FALLBACK PATH: LIVE DYNAMIC TEXT-TO-SQL
        try:
            # Load recent conversation memory for this session so follow-ups
            # ("what about yesterday?") have context (§3.1). Best-effort: never
            # let a memory miss break the request.
            history = await self._load_memory(db_session, agent_id, tenant_id, session_id)

            sql_query, tables_used = await self.sql_skill.generate_and_validate_sql(
                text_query, history=history
            )

            # Log Tool Call intent
            await self._log_to_history(
                db_session=db_session, 
                message_type=ChatMessageType.TOOL_CALL,
                agent_id=agent_id, 
                tenant_id=tenant_id, 
                session_id=session_id, 
                user_id=user_id,
                request_payload={"question": text_query}, 
                response_payload={"generated_sql": sql_query, "tables_filtered": tables_used},
                trace_id=trace_id
            )

            # Execute query against the read-only, tenant-scoped connection
            raw_query_results = await self.sql_skill.execute_query(
                sql_query=sql_query,
                agent_id=agent_id,
                tenant_id=tenant_id
            )
            
            # Convert any database decimal.Decimal values to standard floats
            query_results = serialize_decimal(raw_query_results)
            
            # Log raw database output rows safely
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
            
            # Card Synthesis via Qwen
            card = await self._synthesize_card(text_query, query_results, history=history)

        except Exception as e:
            # Fallback Card on exception
            card = Card(
                title="System Apology",
                body=f"Failed to fetch data: {str(e)}",
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
            request_payload={"text": text_query, "routing": "live_text_to_sql"}, 
            response_payload=card.model_dump(),
            trace_id=trace_id, 
            latency_ms=int((time.time() - start_time) * 1000)
        )

        return card

    async def _synthesize_card(self, question: str, results: list[dict], history: str = "", retries: int = 2) -> Card:
        """
        Synthesizes raw database results into a structured UI Card using a LlamaIndex
        structured-output program (LLMTextCompletionProgram + PydanticOutputParser),
        which enforces the frozen `Card` Pydantic contract — including the per-
        card_kind data_payload shapes via Card's model_validator. `history` is recent
        conversation context (may be empty) used to phrase a coherent follow-up.

        On repeated parse/validation failure (a small local model sometimes can't
        satisfy the strict shapes), fall back to a code-built text Card — never raise.
        """
        program = LLMTextCompletionProgram.from_defaults(
            output_parser=PydanticOutputParser(output_cls=Card),
            prompt_template_str=_CARD_PROMPT_TMPL,
            llm=Settings.llm,
            verbose=False,
        )
        history_block = (
            f"\nRecent conversation (for context only):\n{history}\n" if history else ""
        )

        for attempt in range(retries + 1):
            try:
                card = await program.acall(
                    question=question,
                    results=json.dumps(results),
                    history_block=history_block,
                )
                # from_defaults returns the parsed Card; be defensive about type.
                return card if isinstance(card, Card) else Card.model_validate(card)
            except Exception:
                if attempt == retries:
                    # Final safe fallback if the model can't satisfy the Card schema.
                    return Card(
                        title="Query Summary Results",
                        body=f"Processed query rows successfully. Total records retrieved: {len(results)}",
                        card_kind="text",
                        data_payload=[],
                    )

    async def _load_memory(
        self, db_session: AsyncSession, agent_id: str, tenant_id: str, session_id: str
    ) -> str:
        """
        Loads the last N A2UI_DISPLAY turns for this session and returns them as a
        compact text block for the LLM. Filters tenant_id AND session_id (§7.3) and
        sets app.agent_id so the RLS policy on chat_history is satisfied (§5.2).
        Best-effort: returns "" on any error so memory never breaks a request.
        """
        try:
            turns = self.settings.app.memory_turns
            await db_session.execute(
                text("SELECT set_config('app.agent_id', :v, true)"), {"v": agent_id}
            )
            result = await db_session.execute(
                text(
                    """
                    SELECT request_payload, response_payload
                    FROM chat_history
                    WHERE tenant_id = :tenant_id
                      AND session_id = :session_id
                      AND message_type = 'A2UI_DISPLAY'
                    ORDER BY created_at DESC
                    LIMIT :lim
                    """
                ),
                {"tenant_id": tenant_id, "session_id": session_id, "lim": turns},
            )
            rows = result.fetchall()

            lines = []
            for request_payload, response_payload in reversed(rows):  # oldest first
                req = request_payload if isinstance(request_payload, dict) else {}
                resp = response_payload if isinstance(response_payload, dict) else {}
                question = req.get("text")
                answer = resp.get("body")
                if question and answer:
                    lines.append(f"User: {question}\nAssistant: {answer}")

            return "\n".join(lines)
        except Exception:
            return ""

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
