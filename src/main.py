"""
Main FastAPI server for the Text-to-SQL A2A application.
Implements the A2A JSON-RPC 2.0 interface alongside standard REST endpoints.
Uses pure-Python pg8000 dialect for secure, zero-binary schema reflection.
"""

from uuid import uuid4
from fastapi import FastAPI, Depends, HTTPException, Header, Request
from fastapi.responses import JSONResponse
from sqlalchemy import create_engine
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.pool import NullPool

from llama_index.core import Settings, SQLDatabase
from llama_index.core.embeddings import MockEmbedding

from config.database import DatabaseManager, get_db_session
from config.logging_config import setup_logging
from config.cache import RedisManager
from config.llm import build_llm, build_sql_llm
from config.settings import get_settings
from src.card_model import Card  # IMPORT your Card model
from src.orchestrator import A2AOrchestrator
from src.a2a_protocol import (
    Artifact,
    DataPart,
    Message,
    Task,
    TaskStatus,
    TextPart,
    build_agent_card,
    jsonrpc_error,
    jsonrpc_result,
    INTERNAL_ERROR,
    INVALID_PARAMS,
    INVALID_REQUEST,
    METHOD_NOT_FOUND,
    PARSE_ERROR,
)
from pydantic import BaseModel

settings = get_settings()
setup_logging()

# Build the LlamaIndex LLM from LLM_PROVIDER (default: local Ollama). Set at import
# time, before the orchestrator/skill read Settings.llm.
Settings.llm = build_llm(settings)

# SET the embedding model globally to a local mock embedding model (avoids OpenAI
# dependency inside LlamaIndex internals — NLSQLTableQueryEngine/program paths).
Settings.embed_model = MockEmbedding(embed_dim=1536)

orchestrator = A2AOrchestrator()

# Inject the dedicated low-temperature SQL LLM (stage 5) into the pipeline. The RAG embed
# model is built separately inside the pipeline (it must NOT replace the global MockEmbedding,
# which the LlamaIndex NLSQL internals rely on).
orchestrator.pipeline.set_sql_llm(build_sql_llm(settings))

app = FastAPI(title="Workcloud A2A Text-to-SQL API", version="1.0.0")

# Built once — the Agent Card is static for the process lifetime.
AGENT_CARD = build_agent_card(settings)

# Tables the LlamaIndex SQLDatabase reflects (chat_history is intentionally excluded).
# Restricted strictly to existing tables to prevent startup exceptions
_REFLECT_TABLES = ["stores", "active_tasks"]


class AskRequest(BaseModel):
    text: str
    tenant_id: str
    session_id: str
    user_id: str
    agent_id: str


@app.on_event("startup")
async def startup_event():
    await RedisManager.init()

    # Eagerly initialise the read-only engine so the stage-9 executor's first request does
    # not pay lazy-init cost and connection failures surface predictably at startup.
    DatabaseManager.init_readonly()

    # Build the shared SQLDatabase ONCE here (DB must be live). NLSQLTableQueryEngine
    # only generates SQL (sql_only) so it needs schema reflection, not tenant data —
    # reflection reads the catalog and is unaffected by RLS. Use a SYNC engine
    # (SQLDatabase/SQLAlchemy reflection is sync) on the least-privilege readonly role.
    # Swap out binary psycopg2 with pure-Python pg8000 for Windows-safe runtime
    sync_url = settings.database.readonly_url.replace("+asyncpg", "+pg8000")
    
    sync_engine = create_engine(sync_url, poolclass=NullPool)
    sql_database = SQLDatabase(sync_engine, include_tables=_REFLECT_TABLES)
    orchestrator.sql_skill.sql_database = sql_database


@app.on_event("shutdown")
async def shutdown_event():
    await RedisManager.close()


# UPDATE response_model to Card!
@app.post("/api/v1/ask", response_model=Card)
async def ask_endpoint(
    request: AskRequest,
    x_trace_id: str = Header(None),
    db: AsyncSession = Depends(get_db_session)
):
    try:
        # Returns the highly structured Card JSON matching your coworker's format
        card_payload = await orchestrator.ask(
            db_session=db,
            text_query=request.text,
            tenant_id=request.tenant_id,
            session_id=request.session_id,
            user_id=request.user_id,
            agent_id=request.agent_id,
            trace_id=x_trace_id
        )
        return card_payload
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


# --------------------------------------------------------------------------- #
# A2A protocol surface (augments /api/v1/ask; wraps the same orchestrator)
# --------------------------------------------------------------------------- #

@app.get("/.well-known/agent-card.json")
async def agent_card():
    """A2A discovery: the Agent Card describing this agent's skills + endpoint."""
    return AGENT_CARD.model_dump(exclude_none=True)


@app.get("/.well-known/agent.json")
async def agent_card_legacy():
    """Legacy Agent Card path for older A2A clients."""
    return AGENT_CARD.model_dump(exclude_none=True)


@app.post("/a2a")
async def a2a_endpoint(request: Request, db: AsyncSession = Depends(get_db_session)):
    """
    A2A JSON-RPC 2.0 endpoint. Supports the synchronous `message/send` method: the
    user text is run through the same orchestrator and the resulting Card is returned
    as a DataPart Artifact inside a `completed` Task (plus a text Message for clients
    that only render text).
    """
    try:
        body = await request.json()
    except Exception:
        return JSONResponse(jsonrpc_error(None, PARSE_ERROR, "Parse error: invalid JSON"))
        
    req_id = body.get("id") if isinstance(body, dict) else None
    if not isinstance(body, dict) or body.get("jsonrpc") != "2.0" or "method" not in body:
        return JSONResponse(jsonrpc_error(req_id, INVALID_REQUEST, "Invalid JSON-RPC 2.0 request"))
        
    method = body.get("method")
    if method != "message/send":
        return JSONResponse(
            jsonrpc_error(req_id, METHOD_NOT_FOUND, f"Method not found: {method}")
        )
        
    params = body.get("params") or {}
    message = params.get("message") if isinstance(params, dict) else None
    if not isinstance(message, dict):
        return JSONResponse(jsonrpc_error(req_id, INVALID_PARAMS, "params.message is required"))
        
    # Concatenate the text of all text parts into the natural-language question.
    parts = message.get("parts") or []
    text_query = " ".join(
        p.get("text", "")
        for p in parts
        if isinstance(p, dict) and p.get("kind") == "text"
    ).strip()
    
    if not text_query:
        return JSONResponse(
            jsonrpc_error(req_id, INVALID_PARAMS, "message has no text part to answer")
        )
        
    # Identity bridge: A2A contextId -> our session_id; tenant/user/agent come from
    # message.metadata, falling back to configured defaults.
    meta = message.get("metadata") or {}
    context_id = message.get("contextId") or str(uuid4())
    tenant_id = meta.get("tenant_id", settings.app.default_tenant_id)
    user_id = meta.get("user_id", "a2a_user")
    agent_id = meta.get("agent_id", settings.app.default_agent_id)
    
    try:
        card = await orchestrator.ask(
            db_session=db,
            text_query=text_query,
            tenant_id=tenant_id,
            session_id=context_id,
            user_id=user_id,
            agent_id=agent_id,
            trace_id=request.headers.get("x-trace-id"),
        )
    except Exception as e:
        return JSONResponse(
            jsonrpc_error(req_id, INTERNAL_ERROR, f"Agent execution failed: {e}")
        )
        
    task_id = str(uuid4())
    task = Task(
        id=task_id,
        contextId=context_id,
        status=TaskStatus(state="completed"),
        artifacts=[
            Artifact(
                artifactId=str(uuid4()),
                name="card",
                parts=[DataPart(data=card.model_dump()).model_dump()],
            )
        ],
        history=[
            Message(
                role="agent",
                parts=[TextPart(text=card.body).model_dump()],
                messageId=str(uuid4()),
                taskId=task_id,
                contextId=context_id,
            )
        ],
    )
    return JSONResponse(jsonrpc_result(req_id, task.model_dump(exclude_none=True)))
