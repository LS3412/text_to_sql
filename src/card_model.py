# Open: C:\Users\ls3412\Desktop\A2A\src\card_model.py
from pydantic import BaseModel, Field, model_validator
from typing import Any, Literal
import re

class Card(BaseModel):
    title: str = Field(..., description="Card heading, e.g. 'Store 118 Execution Summary'")
    body: str  = Field(..., description="Plain-English summary of the answer")
    data_payload: list[dict[str, Any]] = Field(default_factory=list,
        description="The raw metrics rows used to draw charts/tables/lists")
    suggested_actions: list[str] = Field(default_factory=list,
        description="Follow-up questions, rendered as tappable buttons")
    card_kind: Literal[
        "summary", "metric", "list", "ranking",
        "comparison", "trend", "alert", "confirmation", "text", "auto"
    ] = "auto"

    @model_validator(mode='after')
    def validate_payload_shape(self) -> 'Card':
        kind = self.card_kind
        payload = self.data_payload
        
        if kind == "text" and len(payload) > 0:
            raise ValueError("text kind must have an empty data_payload")
            
        for i, item in enumerate(payload):
            if kind == "summary":
                if not isinstance(item.get("label"), str) or not isinstance(item.get("value"), (int, float)) or not isinstance(item.get("unit"), str):
                    raise ValueError(f"summary item at index {i} must have 'label' (str), 'value' (number), 'unit' (str)")
            elif kind == "metric":
                if not isinstance(item.get("label"), str) or not isinstance(item.get("value"), (int, float)) or not isinstance(item.get("unit"), str):
                    raise ValueError(f"metric item at index {i} must have 'label' (str), 'value' (number), 'unit' (str)")
            elif kind == "list":
                if not isinstance(item.get("id"), str) or not isinstance(item.get("title"), str) or not isinstance(item.get("subtitle"), str):
                    raise ValueError(f"list item at index {i} must have 'id' (str), 'title' (str), 'subtitle' (str)")
            elif kind == "ranking":
                if not isinstance(item.get("name"), str) or not isinstance(item.get("metric"), (int, float)) or not isinstance(item.get("rank"), int):
                    raise ValueError(f"ranking item at index {i} must have 'name' (str), 'metric' (number), 'rank' (int)")
            elif kind == "comparison":
                if not isinstance(item.get("entity"), str) or not isinstance(item.get("metric"), str) or not isinstance(item.get("value"), (int, float)):
                    raise ValueError(f"comparison item at index {i} must have 'entity' (str), 'metric' (str), 'value' (number)")
            elif kind == "trend":
                date_val = item.get("date")
                if not isinstance(date_val, str) or not re.match(r"^\d{4}-\d{2}-\d{2}$", date_val) or not isinstance(item.get("value"), (int, float)):
                    raise ValueError(f"trend item at index {i} must have 'date' (YYYY-MM-DD) and 'value' (number)")
            elif kind == "alert":
                if not isinstance(item.get("id"), str) or not isinstance(item.get("title"), str) or item.get("severity") not in ["low", "med", "high"]:
                    raise ValueError(f"alert item at index {i} must have 'id' (str), 'title' (str), and 'severity' in ['low', 'med', 'high']")
            elif kind == "confirmation":
                if not isinstance(item.get("field"), str) or not isinstance(item.get("value"), str):
                    raise ValueError(f"confirmation item at index {i} must have 'field' (str) and 'value' (str)")
        return self
