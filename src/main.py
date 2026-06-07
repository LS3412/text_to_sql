# Open: C:\Users\ls3412\Desktop\A2A\src\main.py
from fastapi import FastAPI, Depends, HTTPException, Header
from sqlalchemy.ext.asyncio import AsyncSession
from llama_index.core import Settings
from llama_index.llms.ollama import Ollama
from llama_index.core.embeddings import MockEmbedding 

from config.database import get_db_session
from config.logging_config import setup_logging
from config.cache import RedisManager
from config.settings import get_settings
from src.card_model import Card # IMPORT your Card model
from src.orchestrator import A2AOrchestrator
from pydantic import BaseModel

settings = get_settings()
setup_logging()

# Route LlamaIndex text queries globally to Ollama
Settings.llm = Ollama(
    model=settings.llm.model, 
    base_url="http://localhost:11434", 
    request_timeout=float(settings.llm.timeout)
)

# SET the embedding model globally to a local mock embedding model (avoids OpenAI dependency)
Settings.embed_model = MockEmbedding(embed_dim=1536)

orchestrator = A2AOrchestrator()
app = FastAPI(title="Workcloud A2A Text-to-SQL API", version="1.0.0")

class AskRequest(BaseModel):
    text: str
    tenant_id: str
    session_id: str
    user_id: str
    agent_id: str

@app.on_event("startup")
async def startup_event():
    await RedisManager.init()

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
