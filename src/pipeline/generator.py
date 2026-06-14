"""
Stage 5 — SQL Generation Engine (LLM).

Assembles a prompt from the RetrievedSchema (top-K tables/columns or the compact
all-tables list), the resolved entity links (stage 4), and the semantic-layer business
rules, then produces SQL in the canonical SOURCE dialect plus a presentation CardSpec in a
SINGLE LLM call. SQL and CardSpec are returned as TWO separate objects: the correction loop
re-runs generation to fix the SQL, and stage 11 consumes the CardSpec.

Uses a dedicated low-temperature ``sql_llm`` (built at startup) for deterministic SQL,
falling back to the global ``Settings.llm`` if one was not provided.
"""

from __future__ import annotations

import json
import logging
import re

from src.pipeline.contracts import (
    RETRIEVAL_ALL_TABLES_COMPACT,
    RETRIEVAL_CORE_FLOOR,
    VALID_CARD_KINDS,
    CardSpec,
    GeneratedSQL,
    PipelineContext,
    RetrievedSchema,
    RouteDecision,
    SchemaLinks,
    StageError,
)

logger = logging.getLogger(__name__)

_JSON_KEYS_PROMPT = """Return ONLY a raw JSON object with exactly these keys (no markdown, no commentary):
{
  "sql": "a single SELECT query in <DIALECT> SQL (no trailing semicolon)",
  "title": "a short card title",
  "body_template": "plain-English answer; use '{value}' where the primary result will be inserted",
  "card_kind": "metric | summary | list | ranking | text",
  "metric_label": "label for the primary value (e.g. 'Completion rate')",
  "metric_unit": "unit of the value (e.g. '%', 'tasks', or '')",
  "suggested_actions": ["follow-up 1", "follow-up 2"]
}"""


def _strip_markdown(text: str) -> str:
    text = re.sub(r"^```(?:json|sql)?", "", text.strip(), flags=re.IGNORECASE)
    text = re.sub(r"```$", "", text).strip()
    return text


class SQLGenerator:
    def __init__(self, llm, semantic, settings):
        self.llm = llm
        self.semantic = semantic
        self.settings = settings

    async def generate(
        self,
        ctx: PipelineContext,
        retrieved: RetrievedSchema,
        route: RouteDecision,
        links: SchemaLinks | None = None,
        correction_hint: StageError | None = None,
    ) -> tuple[GeneratedSQL, CardSpec]:
        prompt = self.build_prompt(ctx.question, retrieved, route, links, correction_hint)
        try:
            resp = await self.llm.acomplete(prompt)
            raw = (resp.text or "").strip()
        except Exception as exc:
            raise StageError("generate", "generation", f"LLM call failed: {exc}", retryable=True)

        if not raw:
            raise StageError("generate", "generation", "empty LLM response", retryable=True)

        try:
            parsed = json.loads(_strip_markdown(raw))
        except Exception as exc:
            raise StageError("generate", "generation", f"unparseable JSON: {exc}", retryable=True)

        sql = (parsed.get("sql") or "").strip()
        if not sql:
            raise StageError("generate", "empty", "no SQL produced", retryable=True)

        card_kind = parsed.get("card_kind", "auto")
        if card_kind not in VALID_CARD_KINDS:
            card_kind = "auto"
        card_spec = CardSpec(
            title=parsed.get("title", "Result"),
            body_template=parsed.get("body_template", "{value}"),
            card_kind=card_kind,
            metric_label=parsed.get("metric_label", "Value"),
            metric_unit=parsed.get("metric_unit", ""),
            suggested_actions=parsed.get("suggested_actions", []) or [],
        )
        return GeneratedSQL(sql=sql, source_dialect=route.source_dialect), card_spec

    # ------------------------------------------------------------------ #
    def build_prompt(
        self,
        question: str,
        retrieved: RetrievedSchema,
        route: RouteDecision,
        links: SchemaLinks | None,
        correction_hint: StageError | None,
    ) -> str:
        compact = (
            retrieved.retrieval_mode in (RETRIEVAL_ALL_TABLES_COMPACT, RETRIEVAL_CORE_FLOOR)
            or (retrieved.tables and all(not t.columns for t in retrieved.tables))
        )

        if retrieved.is_empty():
            schema_block = "(no schema could be retrieved)"
            picker_note = ""
        elif compact:
            schema_block = retrieved.compact_table_list()
            picker_note = (
                "\nThe list above shows tables and descriptions only (no columns). Choose the "
                "most relevant table(s) and reference only columns you are confident exist.\n"
            )
        else:
            schema_block = retrieved.to_prompt_block()
            picker_note = ""

        rules = self.semantic.business_rules(list(retrieved.table_names()))
        rules_block = "\n".join(f"- {r}" for r in rules) or "- (none)"

        parts: list[str] = [
            f"You are a precise {route.source_dialect} SQL generation engine.",
            "",
            "Schemas:",
            schema_block,
            picker_note,
        ]

        if links and not links.is_empty():
            parts += ["Entity mappings:", links.to_prompt_block(), ""]

        parts += ["Rules:", rules_block, ""]

        if correction_hint is not None:
            parts += [
                "YOUR PREVIOUS ATTEMPT FAILED.",
                f"  Stage: {correction_hint.stage} ({correction_hint.kind})",
                f"  Error: {correction_hint.message}",
                f"  Previous SQL: {correction_hint.sql or '(none)'}",
                "Fix the query. Use ONLY the tables and columns shown above.",
                "",
            ]

        parts += [
            _JSON_KEYS_PROMPT.replace("<DIALECT>", route.source_dialect),
            "",
            f"Question: {question}",
            "JSON Output:",
        ]
        return "\n".join(parts)
