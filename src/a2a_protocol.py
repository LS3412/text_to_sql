"""
Minimal, spec-compliant Agent2Agent (A2A) protocol surface.

This is a hand-rolled subset of the open A2A protocol (Linux Foundation, v1.0/2026)
— enough to make this Text-to-SQL agent discoverable and callable by other agents,
without pulling in the full `a2a-sdk`:

  * an Agent Card served at /.well-known/agent-card.json (discovery), and
  * a synchronous JSON-RPC 2.0 `message/send` method that returns the orchestrator's
    Card as a DataPart Artifact inside a `completed` Task.

The JSON wire format is camelCase per the spec; these Pydantic models use camelCase
field names directly. Parts use the `kind` discriminator ("text" | "data").

Out of scope here (documented in IMPLEMENTATION_NOTES.md): message/stream (SSE),
tasks/get + a task store, push notifications, and signed Agent Cards.
"""

from typing import Any, Literal, Optional
from pydantic import BaseModel, Field

# JSON-RPC 2.0 standard error codes used by the /a2a handler.
PARSE_ERROR = -32700
INVALID_REQUEST = -32600
METHOD_NOT_FOUND = -32601
INVALID_PARAMS = -32602
INTERNAL_ERROR = -32603


# --------------------------------------------------------------------------- #
# Agent Card (discovery)
# --------------------------------------------------------------------------- #
class AgentSkill(BaseModel):
    id: str
    name: str
    description: str
    tags: list[str] = Field(default_factory=list)
    examples: list[str] = Field(default_factory=list)
    inputModes: list[str] = Field(default_factory=lambda: ["text/plain"])
    outputModes: list[str] = Field(default_factory=lambda: ["application/json", "text/plain"])


class AgentCapabilities(BaseModel):
    streaming: bool = False
    pushNotifications: bool = False
    stateTransitionHistory: bool = False


class AgentProvider(BaseModel):
    organization: str
    url: Optional[str] = None


class AgentInterface(BaseModel):
    transport: str = "JSONRPC"
    url: str


class AgentCard(BaseModel):
    protocolVersion: str = "0.3.0"
    name: str
    description: str
    version: str
    url: str
    preferredTransport: str = "JSONRPC"
    additionalInterfaces: list[AgentInterface] = Field(default_factory=list)
    provider: Optional[AgentProvider] = None
    capabilities: AgentCapabilities = Field(default_factory=AgentCapabilities)
    defaultInputModes: list[str] = Field(default_factory=lambda: ["text/plain"])
    defaultOutputModes: list[str] = Field(default_factory=lambda: ["application/json", "text/plain"])
    skills: list[AgentSkill] = Field(default_factory=list)


# --------------------------------------------------------------------------- #
# Messages, Parts, Artifacts, Tasks
# --------------------------------------------------------------------------- #
class TextPart(BaseModel):
    kind: Literal["text"] = "text"
    text: str


class DataPart(BaseModel):
    kind: Literal["data"] = "data"
    data: dict[str, Any]


class Message(BaseModel):
    role: Literal["user", "agent"]
    parts: list[dict[str, Any]]
    messageId: str
    kind: Literal["message"] = "message"
    taskId: Optional[str] = None
    contextId: Optional[str] = None
    metadata: Optional[dict[str, Any]] = None


class Artifact(BaseModel):
    artifactId: str
    name: Optional[str] = None
    parts: list[dict[str, Any]] = Field(default_factory=list)


class TaskStatus(BaseModel):
    state: Literal[
        "submitted", "working", "input-required", "completed", "failed", "canceled"
    ]


class Task(BaseModel):
    id: str
    contextId: str
    kind: Literal["task"] = "task"
    status: TaskStatus
    artifacts: list[Artifact] = Field(default_factory=list)
    history: list[Message] = Field(default_factory=list)


# --------------------------------------------------------------------------- #
# JSON-RPC envelopes
# --------------------------------------------------------------------------- #
class JSONRPCError(BaseModel):
    code: int
    message: str
    data: Optional[Any] = None


class JSONRPCResponse(BaseModel):
    jsonrpc: Literal["2.0"] = "2.0"
    id: Optional[Any] = None
    result: Optional[Any] = None
    error: Optional[JSONRPCError] = None


def jsonrpc_error(request_id: Any, code: int, message: str, data: Any = None) -> dict:
    """Build a JSON-RPC 2.0 error response payload."""
    return JSONRPCResponse(
        id=request_id, error=JSONRPCError(code=code, message=message, data=data)
    ).model_dump(exclude_none=True)


def jsonrpc_result(request_id: Any, result: Any) -> dict:
    """Build a JSON-RPC 2.0 success response payload."""
    return JSONRPCResponse(id=request_id, result=result).model_dump(exclude_none=True)


# --------------------------------------------------------------------------- #
# Agent Card builder
# --------------------------------------------------------------------------- #
def build_agent_card(settings) -> AgentCard:
    """Build this agent's Agent Card from application settings."""
    base_url = settings.app.a2a_agent_url.rstrip("/")
    return AgentCard(
        name="Workcloud Field Insights A2A Agent",
        description=(
            "Converts natural-language questions about retail field task execution "
            "into safe, read-only PostgreSQL, runs them under tenant isolation, and "
            "returns a structured UI Card."
        ),
        version="1.0.0",
        url=f"{base_url}/a2a",
        provider=AgentProvider(organization="Workcloud"),
        capabilities=AgentCapabilities(streaming=False),
        skills=[
            AgentSkill(
                id="text_to_sql",
                name="Field Insights Text-to-SQL",
                description=(
                    "Answers current-state questions about store/district task "
                    "completion, overdue and at-risk tasks, and store comparisons."
                ),
                tags=["text-to-sql", "retail", "field-insights", "analytics"],
                examples=[
                    "Give me a summary of my store's task performance today.",
                    "What tasks are at risk of becoming overdue in my district?",
                    "Why are tasks being completed late in Store 118?",
                    "I have 15 minutes left in my shift — what task can I knock out?",
                ],
            )
        ],
    )
