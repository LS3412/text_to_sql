"""
Stage 1 — Query Classifier & Database Router.

Runs AFTER the prebuilt-KPI fast-path and Redis cache (so its cost is only paid on true
dynamic queries) and BEFORE schema retrieval. It decides two things:
  (a) in-scope (answerable from the schema) vs out-of-scope (greeting / smalltalk), and
  (b) which DBProfile handles the question (dialect target + catalog db_source).

Strategy is deterministic rules/keyword overlap by default (~0ms); an optional tiny LLM
classification runs only when the rules are inconclusive AND settings.router.use_llm_fallback
is on (off by default). The router fails OPEN: when unsure it routes in-scope to the default
(postgres) profile rather than wrongly rejecting a real question — downstream validators are
the safety net.
"""

from __future__ import annotations

import json
import logging
import re

from src.card_model import Card
from src.pipeline.contracts import RouteDecision
from src.pipeline.profiles import default_profile, get_profile
from src.pipeline.semantic_layer import SemanticLayer

logger = logging.getLogger(__name__)

# Phrases that, with ZERO schema-vocabulary overlap, mark a turn as out-of-scope.
_GREETING_PATTERNS = (
    "hello", "hi ", "hey", "good morning", "good afternoon", "good evening",
    "how are you", "who are you", "what can you do", "what are you",
    "thanks", "thank you", "bye", "goodbye", "help me", "test",
)

# Hints that a question targets a ClickHouse-resident table (dialect/catalog target only).
_CLICKHOUSE_HINTS = ("events", "event log", "logs", "raw", "analytics", "clickstream", "telemetry")

_WORD_RE = re.compile(r"[a-z0-9]+")


def build_out_of_scope_card(message: str) -> Card:
    return Card(title="I can only answer data questions", body=message, card_kind="text", data_payload=[])


class DatabaseRouter:
    def __init__(self, semantic: SemanticLayer, settings):
        self.semantic = semantic
        self.settings = settings

    async def route(self, question: str) -> RouteDecision:
        try:
            return await self._route(question)
        except Exception as exc:  # fail-open — never let routing break a real query
            logger.warning("Router error (%s); failing open to in_scope+default", exc)
            prof = default_profile()
            return RouteDecision(
                in_scope=True, profile=prof,
                source_dialect=self._source_dialect(), target_dialect=prof.dialect,
                reason="router_error_fail_open", confidence=0.0, ambiguous=True,
            )

    # ------------------------------------------------------------------ #
    async def _route(self, question: str) -> RouteDecision:
        profile = self._select_profile(question)
        overlap = self._vocab_overlap(question, profile.db_source)
        q_lower = question.lower()
        is_greeting = any(pat in q_lower for pat in _GREETING_PATTERNS)

        if overlap >= max(1, self.settings.router.ambiguity_min_overlap):
            return self._decision(True, profile, "vocabulary_overlap", confidence=1.0)

        if is_greeting:
            return self._decision(False, profile, "greeting_no_overlap", confidence=0.9)

        # Inconclusive: optional LLM tie-breaker, else fail open.
        if self.settings.router.use_llm_fallback:
            verdict = await self._llm_classify(question)
            if verdict is not None:
                in_scope, prof_name = verdict
                return self._decision(
                    in_scope, get_profile(prof_name), "llm_classifier",
                    confidence=0.7, ambiguous=True,
                )

        return self._decision(True, profile, "fail_open", confidence=0.3, ambiguous=True)

    # ------------------------------------------------------------------ #
    def _decision(self, in_scope, profile, reason, confidence=1.0, ambiguous=False) -> RouteDecision:
        return RouteDecision(
            in_scope=in_scope, profile=profile,
            source_dialect=self._source_dialect(), target_dialect=profile.dialect,
            reason=reason, confidence=confidence, ambiguous=ambiguous,
        )

    def _source_dialect(self) -> str:
        return getattr(self.settings.pipeline, "source_dialect", "postgres")

    def _select_profile(self, question: str):
        q_lower = question.lower()
        clickhouse_tables = self.semantic.all_table_names("clickhouse")
        if clickhouse_tables and any(h in q_lower for h in _CLICKHOUSE_HINTS):
            return get_profile("clickhouse")
        return get_profile(self.settings.router.default_profile)

    def _vocab_overlap(self, question: str, db_source: str) -> int:
        vocab = self.semantic.vocabulary(db_source)
        q_lower = question.lower()
        words = set(_WORD_RE.findall(q_lower))
        # single-word overlap
        count = len(words & vocab)
        # multi-word synonym phrase substring overlap (e.g. "completion rate")
        for token in vocab:
            if " " in token and token in q_lower:
                count += 1
        return count

    async def _llm_classify(self, question: str):
        """Returns (in_scope, profile_name) or None on any failure."""
        try:
            from llama_index.core import Settings as LISettings

            table_list = ", ".join(self.semantic.all_table_names())
            prompt = (
                "Classify the user question for a SQL data assistant. Available tables: "
                f"{table_list}.\n"
                "Return ONLY raw JSON: {\"in_scope\": true|false, \"profile\": \"postgres\"}\n"
                "in_scope is false ONLY for greetings/smalltalk/requests unrelated to the data.\n\n"
                f"Question: {question}\nJSON:"
            )
            resp = await LISettings.llm.acomplete(prompt)
            text = re.sub(r"^```(?:json)?|```$", "", resp.text.strip(), flags=re.IGNORECASE).strip()
            parsed = json.loads(text)
            return bool(parsed.get("in_scope", True)), parsed.get("profile", "postgres")
        except Exception as exc:
            logger.warning("Router LLM classify failed (%s); ignoring", exc)
            return None
