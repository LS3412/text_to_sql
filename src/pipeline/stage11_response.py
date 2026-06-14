"""
Stage 11 — Natural Language Response Generator.

Turns the execution rows + the CardSpec from generation into the frozen ``Card``. The
numeric-key extractor and per-kind payload reshaping that used to live inline in
A2AOrchestrator.ask now live here as reusable, testable functions. ``serialize_decimal`` is
relocated here (and re-exported from the orchestrator for the eval harness).

Guarantees a renderable Card: empty results become a text Card, and if a reshaped payload
violates the strict per-kind Card validator (e.g. the LLM picked card_kind='ranking' but the
rows lack name/metric/rank) it is caught and degraded to a valid text Card — never a 500.
"""

from __future__ import annotations

import decimal
import logging

from pydantic import ValidationError

from src.card_model import Card
from src.pipeline.contracts import CardSpec, ExecutionResult

logger = logging.getLogger(__name__)


def serialize_decimal(obj):
    """Recursively convert decimal.Decimal -> float so JSON serialization always succeeds."""
    if isinstance(obj, list):
        return [serialize_decimal(item) for item in obj]
    if isinstance(obj, dict):
        return {k: serialize_decimal(v) for k, v in obj.items()}
    if isinstance(obj, decimal.Decimal):
        return float(obj)
    return obj


def _first_numeric(row: dict):
    for k, v in row.items():
        if isinstance(v, (int, float)) and not isinstance(v, bool):
            return k, v
    return None, None


def _fill_body(template: str, value) -> str:
    return (template or "{value}").replace("{{value}}", str(value)).replace("{value}", str(value))


def build_card(card_spec: CardSpec, result: ExecutionResult) -> Card:
    rows = result.rows or []

    if not rows:
        return Card(
            title=card_spec.title or "No data",
            body=_fill_body(card_spec.body_template, "no data"),
            card_kind="text",
            data_payload=[],
            suggested_actions=card_spec.suggested_actions,
        )

    first = rows[0]
    num_key, num_val = _first_numeric(first)
    primary_value = num_val if num_key is not None else list(first.values())[0]
    body_text = _fill_body(card_spec.body_template, primary_value)

    kind = card_spec.card_kind
    payload = _reshape_payload(kind, rows, card_spec, num_key, num_val)

    try:
        return Card(
            title=card_spec.title or "Result",
            body=body_text,
            card_kind=kind,
            data_payload=payload,
            suggested_actions=card_spec.suggested_actions,
        )
    except ValidationError as exc:
        logger.info("Card payload failed strict validation for kind=%s (%s); using text card", kind, exc)
        return Card(
            title=card_spec.title or "Result",
            body=body_text,
            card_kind="text",
            data_payload=[],
            suggested_actions=card_spec.suggested_actions,
        )


def _reshape_payload(kind, rows, card_spec: CardSpec, num_key, num_val) -> list[dict]:
    try:
        if kind in ("metric", "summary", "auto"):
            if num_key is None:
                return []  # no numeric value -> let validation degrade to text for metric/summary
            return [{
                "label": card_spec.metric_label or "Value",
                "value": num_val,
                "unit": card_spec.metric_unit or "",
            }]

        if kind == "list":
            out = []
            for i, row in enumerate(rows):
                title = row.get("task_name") or row.get("name") or list(row.values())[0]
                subtitle = f"Status: {row['status']}" if "status" in row else \
                    "; ".join(f"{k}: {v}" for k, v in list(row.items())[1:3]) or " "
                out.append({"id": str(i + 1), "title": str(title), "subtitle": str(subtitle)})
            return out

        if kind == "ranking":
            out = []
            for i, row in enumerate(rows):
                name = next((str(v) for v in row.values() if isinstance(v, str)), str(list(row.values())[0]))
                metric = next((v for v in row.values() if isinstance(v, (int, float)) and not isinstance(v, bool)), 0)
                out.append({"name": name, "metric": metric, "rank": i + 1})
            return out

        if kind == "comparison":
            out = []
            for row in rows:
                entity = next((str(v) for v in row.values() if isinstance(v, str)), "")
                value = next((v for v in row.values() if isinstance(v, (int, float)) and not isinstance(v, bool)), 0)
                out.append({"entity": entity, "metric": card_spec.metric_label or "value", "value": value})
            return out

        if kind == "trend":
            out = []
            for row in rows:
                date_val = next((str(v) for k, v in row.items() if "date" in k.lower() or "day" in k.lower()), None)
                value = next((v for v in row.values() if isinstance(v, (int, float)) and not isinstance(v, bool)), 0)
                if date_val:
                    out.append({"date": date_val, "value": value})
            return out

    except Exception as exc:
        logger.debug("payload reshape failed for kind=%s (%s)", kind, exc)
    return []
