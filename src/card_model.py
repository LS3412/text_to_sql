from pydantic import BaseModel, Field

class Card(BaseModel):
    title: str
    body: str
    data_payload: list = Field(default_factory=list)
