"""
Pluggable DB-profile registry (referenced by stages 1, 8, 9).

A profile maps a logical target name to:
  * a sqlglot ``dialect`` (transpiler, stage 8),
  * a catalog ``db_source`` filter (retrieval, stage 3),
  * an ``executor_key`` into the executor registry (stage 9), or ``None`` if the
    target is a dialect/catalog target only and not yet executable.

Per the locked decision, all execution stays on Postgres: ``postgres`` and ``alloydb``
both resolve to the wired Postgres executor (AlloyDB is Postgres-wire compatible), and
``clickhouse`` is a dialect/catalog target with ``executor_key=None`` so stage 9 can
refuse-and-fallback cleanly. The registry is open for new executors via
``register_profile``.
"""

from __future__ import annotations

from src.pipeline.contracts import DBProfile

PROFILE_REGISTRY: dict[str, DBProfile] = {
    "postgres": DBProfile(
        name="postgres", dialect="postgres", db_source="sql",
        executor_key="postgres", is_default=True,
    ),
    "alloydb": DBProfile(
        name="alloydb", dialect="postgres", db_source="sql",
        executor_key="postgres", is_default=False,
    ),
    "clickhouse": DBProfile(
        name="clickhouse", dialect="clickhouse", db_source="clickhouse",
        executor_key=None, is_default=False,
    ),
}


def default_profile() -> DBProfile:
    for p in PROFILE_REGISTRY.values():
        if p.is_default:
            return p
    return PROFILE_REGISTRY["postgres"]


def get_profile(name: str | None) -> DBProfile:
    """Return the named profile, falling back to the default on unknown/empty input.

    Never raises — an unknown name degrades to the default profile.
    """
    if not name:
        return default_profile()
    return PROFILE_REGISTRY.get(name.strip().lower(), default_profile())


def register_profile(profile: DBProfile) -> None:
    """Extension hook so a future executor (e.g. a real ClickHouse engine) can be added."""
    PROFILE_REGISTRY[profile.name] = profile
