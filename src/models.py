# Open: C:\Users\ls3412\Desktop\A2A\src\models.py
import uuid
import enum
from datetime import datetime
from sqlalchemy import Column, String, Numeric, Integer, DateTime, Enum as SQLEnum
from sqlalchemy.dialects.postgresql import UUID, JSONB
from sqlalchemy.orm import declarative_base

Base = declarative_base()

class ChatMessageType(str, enum.Enum):
    A2UI_DISPLAY = 'A2UI_DISPLAY'
    TOOL_CALL = 'TOOL_CALL'
    TOOL_RESULT = 'TOOL_RESULT'
    AGENT_INTERNAL = 'AGENT_INTERNAL'
    SYSTEM_LOG = 'SYSTEM_LOG'

class ChatHistory(Base):
    __tablename__ = 'chat_history'

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    created_at = Column(DateTime(timezone=True), nullable=False, default=datetime.utcnow)
    message_type = Column(SQLEnum(ChatMessageType, name='chat_message_type'), nullable=False)
    
    agent_id = Column(String(255), nullable=False)
    tenant_id = Column(String(255), nullable=False)
    session_id = Column(String(255), nullable=False)
    user_id = Column(String(255))
    parent_id = Column(UUID(as_uuid=True), nullable=True)
    
    request_payload = Column(JSONB, nullable=True)
    response_payload = Column(JSONB, nullable=False)
    
    trace_id = Column(String(255), nullable=True)
    model_name = Column(String(255), nullable=True)
    llm_usage_cost = Column(Numeric(10, 8), default=0.0)
    latency_ms = Column(Integer, nullable=True)
    
    feedback = Column(JSONB, nullable=True)
    
    # RENAME the python attribute to 'meta' but map it to 'metadata' in PostgreSQL
    meta = Column('metadata', JSONB, nullable=True) 
