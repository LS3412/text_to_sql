"""
Stage 4 — Schema Linking (Entity Resolution).

Resolves natural-language phrases to concrete table/column/value targets using the semantic
layer's synonyms and value-maps (e.g. "done" -> active_tasks.status = 'Completed',
"store 118" -> stores.store_name = 'Store 118'), with an optional difflib fuzzy fallback.
Only links against tables present in the RetrievedSchema, keeping the generation prompt
scoped. Linking is ADVISORY and additive — if nothing links the generator proceeds on the
retrieved schema alone; it is never a gate, and each sub-matcher is independently guarded.
"""

from __future__ import annotations

import difflib
import logging
import re

from src.pipeline.contracts import (
    LINK_EXACT,
    LINK_FUZZY,
    LINK_SYNONYM,
    LINK_VALUE_MAP,
    EntityLink,
    RetrievedSchema,
    SchemaLinks,
)

logger = logging.getLogger(__name__)
_WORD_RE = re.compile(r"[a-z0-9]+")


class SchemaLinker:
    def __init__(self, semantic, embed_model=None, settings=None):
        self.semantic = semantic
        self.embed_model = embed_model
        self.settings = settings

    def link(self, question: str, retrieved: RetrievedSchema) -> SchemaLinks:
        q_lower = question.lower()
        links: list[EntityLink] = []
        for rt in retrieved.tables:
            tdef = self.semantic.get_table(rt.table)
            if not tdef:
                continue
            for col in tdef.columns:
                try:
                    self._match_column(q_lower, rt.table, col, links)
                except Exception as exc:  # one bad column must not break linking
                    logger.debug("link match failed for %s.%s (%s)", rt.table, col.name, exc)

        try:
            self._fuzzy_columns(q_lower, retrieved, links)
        except Exception as exc:
            logger.debug("fuzzy linking skipped (%s)", exc)

        return SchemaLinks(links=self._dedup(links))

    # ------------------------------------------------------------------ #
    def _match_column(self, q_lower: str, table: str, col, links: list[EntityLink]) -> None:
        # 1. value-map: phrase -> canonical literal (strongest signal)
        for phrase, literal in col.value_map.items():
            if phrase.lower() in q_lower:
                links.append(EntityLink(
                    phrase=phrase, table=table, column=col.name, literal=literal,
                    confidence=0.95, source=LINK_VALUE_MAP,
                ))

        # 2. synonyms / column-name match
        candidates = [col.name.replace("_", " "), *(s for s in col.synonyms)]
        for cand in candidates:
            cand = cand.strip().lower()
            if cand and len(cand) > 2 and cand in q_lower:
                links.append(EntityLink(
                    phrase=cand, table=table, column=col.name,
                    confidence=0.8,
                    source=LINK_EXACT if cand == col.name.replace("_", " ") else LINK_SYNONYM,
                ))
                break

    def _fuzzy_columns(self, q_lower: str, retrieved: RetrievedSchema, links: list[EntityLink]) -> None:
        cutoff = getattr(self.settings.retrieval, "fuzzy_cutoff", 0.8) if self.settings else 0.8
        already = {(ln.table, ln.column) for ln in links}
        col_index: dict[str, tuple[str, str]] = {}
        for rt in retrieved.tables:
            for c in rt.columns:
                col_index[c.column.replace("_", " ")] = (rt.table, c.column)
        names = list(col_index.keys())
        if not names:
            return
        for token in {w for w in _WORD_RE.findall(q_lower) if len(w) > 4}:
            match = difflib.get_close_matches(token, names, n=1, cutoff=cutoff)
            if match:
                table, column = col_index[match[0]]
                if (table, column) not in already:
                    links.append(EntityLink(
                        phrase=token, table=table, column=column,
                        confidence=cutoff, source=LINK_FUZZY,
                    ))
                    already.add((table, column))

    @staticmethod
    def _dedup(links: list[EntityLink]) -> list[EntityLink]:
        seen: set[tuple] = set()
        out: list[EntityLink] = []
        for ln in sorted(links, key=lambda x: -x.confidence):
            key = (ln.table, ln.column, ln.literal)
            if key not in seen:
                seen.add(key)
                out.append(ln)
        return out
