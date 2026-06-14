"""
Stage 2 — Semantic Layer (Data Dictionary).

Loads config/semantic/dictionary.yaml once (process-cached, like config.settings.get_settings)
and exposes a typed, read-only API consumed by every downstream stage:
  * router (stage 1)      — vocabulary() for in-scope detection
  * retriever (stage 3)   — build_retrieved_tables(), compact_table_list(), iter_embedding_docs()
  * linker (stage 4)      — synonyms(), value_map(), get_column()
  * generator (stage 5)   — render_schema_text(), business_rules()
  * type validator (st 7) — get_table(), get_column()
  * seeders               — to_catalog_rows() / table_summary_rows()

It NEVER raises on load: a missing/invalid YAML file, or even a missing PyYAML, degrades
to MINIMAL_DICTIONARY (stores + active_tasks) so the system behaves exactly as it did
before this package existed.
"""

from __future__ import annotations

import logging
from functools import lru_cache
from pathlib import Path
from typing import Iterator, Optional

from pydantic import BaseModel, Field

from src.pipeline.contracts import RetrievedColumn, RetrievedTable

logger = logging.getLogger(__name__)

# text_to_sql/ project root (this file is src/pipeline/semantic_layer.py).
_PROJECT_ROOT = Path(__file__).resolve().parents[2]


# --------------------------------------------------------------------------- #
# Dictionary schema (validates the YAML itself)
# --------------------------------------------------------------------------- #
class JoinDef(BaseModel):
    to_table: str
    on_left: str
    on_right: str
    kind: str = "left"


class ColumnDef(BaseModel):
    name: str
    type: Optional[str] = None
    description: str = ""
    synonyms: list[str] = Field(default_factory=list)
    example_values: list[str] = Field(default_factory=list)
    value_map: dict[str, str] = Field(default_factory=dict)
    is_filterable: bool = True
    is_metric: bool = False


class TableDef(BaseModel):
    name: str
    db_source: str = "sql"
    description: str = ""
    synonyms: list[str] = Field(default_factory=list)
    business_rules: list[str] = Field(default_factory=list)
    columns: list[ColumnDef] = Field(default_factory=list)
    joins: list[JoinDef] = Field(default_factory=list)

    def get_column(self, column: str) -> Optional[ColumnDef]:
        for c in self.columns:
            if c.name == column:
                return c
        return None


class ProfileDef(BaseModel):
    dialect: str
    db_source: str
    executor: Optional[str] = None
    default: bool = False


class SemanticDictionary(BaseModel):
    version: int = 1
    profiles: dict[str, ProfileDef] = Field(default_factory=dict)
    global_business_rules: list[str] = Field(default_factory=list)
    tables: list[TableDef] = Field(default_factory=list)


# --------------------------------------------------------------------------- #
# Minimal fallback dictionary — mirrors the stores/active_tasks of today's catalog
# so to_catalog_rows() reproduces the current 8 rows even with no YAML/PyYAML.
# --------------------------------------------------------------------------- #
MINIMAL_DICTIONARY: dict = {
    "version": 1,
    "global_business_rules": [
        "Generate a single read-only SELECT (or WITH ... SELECT) query only.",
        "Do NOT filter by tenant_id; row-level security isolates tenants automatically.",
        "Do not append a trailing semicolon.",
    ],
    "tables": [
        {
            "name": "stores",
            "db_source": "sql",
            "description": "Retail stores and their overall audit task completion performance.",
            "synonyms": ["store", "stores", "shop", "location", "branch"],
            "columns": [
                {"name": "store_id", "type": "integer",
                 "description": "Unique identifier of the physical store",
                 "example_values": ["1", "2", "3"]},
                {"name": "store_name", "type": "varchar",
                 "description": "The name of the retail store",
                 "example_values": ["Store 118", "Store 202"],
                 "value_map": {"store 118": "Store 118", "118": "Store 118"}},
                {"name": "district_id", "type": "integer",
                 "description": "ID of the regional district",
                 "example_values": ["1", "2", "3"]},
                {"name": "completion_rate", "type": "numeric",
                 "description": "Overall audit task completion percentage of the store",
                 "synonyms": ["completion", "completion rate"],
                 "example_values": ["95.50", "82.10"], "is_metric": True},
            ],
        },
        {
            "name": "active_tasks",
            "db_source": "sql",
            "description": "Audit tasks assigned to stores, including their current completion status.",
            "synonyms": ["task", "tasks", "active task", "active tasks", "audit"],
            "columns": [
                {"name": "task_id", "type": "integer",
                 "description": "Unique task identifier", "example_values": ["1", "2", "3"]},
                {"name": "store_id", "type": "integer",
                 "description": "ID of the store this audit task belongs to",
                 "example_values": ["1", "2"]},
                {"name": "task_name", "type": "varchar",
                 "description": "The name or description of the audit task",
                 "example_values": ["Audit Inventory", "Restock Items"]},
                {"name": "status", "type": "varchar",
                 "description": "Current completion status of the audit task",
                 "synonyms": ["status", "state"],
                 "example_values": ["Pending", "In Progress", "Completed"],
                 "value_map": {"pending": "Pending", "in progress": "In Progress",
                               "completed": "Completed", "done": "Completed"}},
            ],
        },
    ],
}


# --------------------------------------------------------------------------- #
# Runtime API
# --------------------------------------------------------------------------- #
class SemanticLayer:
    def __init__(self, dictionary: SemanticDictionary):
        self._dict = dictionary
        self._by_name: dict[str, TableDef] = {t.name: t for t in dictionary.tables}

    # ---- loading -------------------------------------------------------- #
    @classmethod
    def load(cls) -> "SemanticLayer":
        """Process-cached load (mirrors config.settings.get_settings)."""
        return _load_cached()

    @classmethod
    def from_data(cls, data: dict) -> "SemanticLayer":
        return cls(SemanticDictionary.model_validate(data))

    # ---- lookups -------------------------------------------------------- #
    def get_table(self, name: str) -> Optional[TableDef]:
        return self._by_name.get(name)

    def get_column(self, table: str, column: str) -> Optional[ColumnDef]:
        t = self._by_name.get(table)
        return t.get_column(column) if t else None

    def column_exists(self, table: str, column: str) -> bool:
        return self.get_column(table, column) is not None

    def tables_for_source(self, db_source: str = "sql") -> list[TableDef]:
        return [t for t in self._dict.tables if t.db_source == db_source]

    def all_table_names(self, db_source: Optional[str] = None) -> list[str]:
        return [t.name for t in self._dict.tables if db_source is None or t.db_source == db_source]

    # ---- linker helpers ------------------------------------------------- #
    def synonyms(self, table: str) -> list[str]:
        t = self._by_name.get(table)
        return list(t.synonyms) if t else []

    def value_map(self, table: str, column: str) -> dict[str, str]:
        c = self.get_column(table, column)
        return dict(c.value_map) if c else {}

    def joins(self, table: str) -> list[JoinDef]:
        t = self._by_name.get(table)
        return list(t.joins) if t else []

    def business_rules(self, table_names: Optional[list[str]] = None) -> list[str]:
        rules = list(self._dict.global_business_rules)
        for t in self._dict.tables:
            if table_names is None or t.name in table_names:
                rules.extend(t.business_rules)
        return rules

    def vocabulary(self, db_source: Optional[str] = None) -> set[str]:
        """Lowercased token set (table/column names + synonyms) for router scope detection."""
        vocab: set[str] = set()
        for t in self._dict.tables:
            if db_source is not None and t.db_source != db_source:
                continue
            vocab.add(t.name.lower())
            vocab.update(s.lower() for s in t.synonyms)
            for c in t.columns:
                vocab.add(c.name.lower())
                vocab.update(s.lower() for s in c.synonyms)
        # Split multi-word tokens into individual words too (helps single-word overlap).
        words: set[str] = set()
        for token in vocab:
            words.update(w for w in token.replace("_", " ").split() if len(w) > 2)
        return vocab | words

    # ---- retrieval / prompt rendering ----------------------------------- #
    def build_retrieved_tables(
        self, table_names: list[str], db_source: str = "sql"
    ) -> list[RetrievedTable]:
        out: list[RetrievedTable] = []
        for name in table_names:
            t = self._by_name.get(name)
            if not t or t.db_source != db_source:
                continue
            out.append(
                RetrievedTable(
                    table=t.name,
                    description=t.description,
                    db_source=t.db_source,
                    columns=[
                        RetrievedColumn(
                            table=t.name,
                            column=c.name,
                            description=c.description,
                            data_type=c.type,
                            example_values=list(c.example_values),
                        )
                        for c in t.columns
                    ],
                )
            )
        return out

    def render_schema_text(self, table_names: list[str], dialect: str = "postgres") -> str:
        from src.pipeline.contracts import RetrievedSchema

        schema = RetrievedSchema(
            tables=self.build_retrieved_tables(table_names), dialect=dialect
        )
        return schema.to_prompt_block()

    def compact_table_list(self, db_source: str = "sql", limit: Optional[int] = None) -> str:
        tables = self.tables_for_source(db_source)
        if limit:
            tables = tables[:limit]
        return "\n".join(f"- {t.name}: {t.description}" for t in tables)

    # ---- projections (seeders) ----------------------------------------- #
    def to_catalog_rows(self) -> list[tuple]:
        """Column-level rows shaped like scripts/init_clickhouse.COLUMNS:
        (table_name, column_name, description, db_source, example_values)."""
        rows: list[tuple] = []
        for t in self._dict.tables:
            for c in t.columns:
                rows.append(
                    (t.name, c.name, c.description, t.db_source, ", ".join(c.example_values))
                )
        return rows

    def table_summary_rows(self) -> list[tuple]:
        """One table-level row per table (column_name='') for table-grain keyword search."""
        return [(t.name, "", t.description, t.db_source, "") for t in self._dict.tables]

    def iter_embedding_docs(self) -> Iterator[dict]:
        """table + column granularity docs for the pgvector index."""
        for t in self._dict.tables:
            yield {
                "object_type": "table",
                "table_name": t.name,
                "column_name": None,
                "db_source": t.db_source,
                "content": f"{t.name}: {t.description} ({', '.join(t.synonyms)})",
            }
            for c in t.columns:
                syn = f" ({', '.join(c.synonyms)})" if c.synonyms else ""
                yield {
                    "object_type": "column",
                    "table_name": t.name,
                    "column_name": c.name,
                    "db_source": t.db_source,
                    "content": f"{t.name}.{c.name}: {c.description}{syn}",
                }


@lru_cache(maxsize=1)
def _load_cached() -> SemanticLayer:
    """Load + validate the YAML dictionary, degrading to MINIMAL_DICTIONARY on any failure."""
    try:
        from config.settings import get_settings

        path_str = get_settings().semantic.dictionary_path
    except Exception:  # settings import/parse problem — use the default location
        path_str = "config/semantic/dictionary.yaml"

    path = Path(path_str)
    if not path.is_absolute():
        path = _PROJECT_ROOT / path

    try:
        import yaml  # PyYAML is a soft dependency

        with open(path, "r", encoding="utf-8") as fh:
            data = yaml.safe_load(fh)
        if not isinstance(data, dict) or not data.get("tables"):
            raise ValueError("dictionary.yaml is empty or has no 'tables'")
        return SemanticLayer.from_data(data)
    except Exception as exc:  # FileNotFoundError, ImportError(yaml), YAMLError, ValidationError
        logger.warning(
            "SemanticLayer: falling back to MINIMAL_DICTIONARY (%s: %s)",
            type(exc).__name__, exc,
        )
        return SemanticLayer.from_data(MINIMAL_DICTIONARY)
