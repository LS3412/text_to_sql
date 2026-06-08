"""
Golden dataset for the Text-to-SQL agent.

Covers the spec's 4 target questions (A2A_Specification.md §8) plus a few of the
broader insights. Each golden carries a reference SQL (for SQL-correctness judging)
and an expected insight (for Card-correctness judging). Questions that genuinely
need historical/event data are flagged `requires_mql` — out of SQL scope today, so
the agent is expected to degrade to a partial/text answer rather than fabricate.

Reference SQL is written against the expanded schema in database/schema.sql:
stores, active_tasks (with due_date/completed_at/priority/...), districts, users.
Tenant filtering is intentionally absent (RLS handles it).
"""

from dataclasses import dataclass, field


@dataclass
class Golden:
    id: str
    question: str
    expected_sql: str
    expected_insight: str
    tags: list[str] = field(default_factory=list)
    requires_mql: bool = False


GOLDENS: list[Golden] = [
    Golden(
        id="q1_store_summary_today",
        question="Give me a summary of my store's task performance today.",
        expected_sql=(
            "SELECT s.store_name, COUNT(t.task_id) AS total_tasks, "
            "COUNT(*) FILTER (WHERE t.status = 'Completed') AS completed_tasks "
            "FROM stores s JOIN active_tasks t ON t.store_id = s.store_id "
            "GROUP BY s.store_name"
        ),
        expected_insight=(
            "A per-store breakdown of how many tasks exist and how many are completed "
            "vs still open, summarizing today's execution."
        ),
        tags=["store_manager", "summary"],
    ),
    Golden(
        id="q2_at_risk_overdue_district",
        question="What tasks are at risk of becoming overdue in my district?",
        expected_sql=(
            "SELECT t.task_name, s.store_name, t.due_date, t.status "
            "FROM active_tasks t JOIN stores s ON t.store_id = s.store_id "
            "WHERE t.status <> 'Completed' AND t.due_date < now() + INTERVAL '1 day' "
            "ORDER BY t.due_date"
        ),
        expected_insight=(
            "A list of not-yet-completed tasks whose due_date is in the past or "
            "imminent, i.e. overdue or at risk, with their store and deadline."
        ),
        tags=["district_manager", "alert", "overdue"],
    ),
    Golden(
        id="q3_why_late_store_118",
        question="Why are tasks being completed late in Store 118?",
        expected_sql=(
            "SELECT t.task_name, t.project_type, t.priority, t.due_date, t.completed_at "
            "FROM active_tasks t JOIN stores s ON t.store_id = s.store_id "
            "WHERE s.store_name = 'Store 118' AND t.completed_at > t.due_date"
        ),
        expected_insight=(
            "Tasks at Store 118 that were completed after their due date, with the "
            "kind of task and priority — partial root cause (full duration/bottleneck "
            "analysis needs historical logs)."
        ),
        tags=["store_manager", "root_cause"],
        requires_mql=True,
    ),
    Golden(
        id="q4_fifteen_minutes",
        question="I have 15 minutes left in my shift — what task can I knock out?",
        expected_sql=(
            "SELECT t.task_name, t.priority FROM active_tasks t JOIN stores s "
            "ON t.store_id = s.store_id WHERE t.status <> 'Completed' "
            "ORDER BY t.priority DESC LIMIT 5"
        ),
        expected_insight=(
            "A short, actionable task suggestion. Best answered with historical task "
            "durations (MQL); from SQL alone, the open low-effort/high-priority tasks."
        ),
        tags=["store_manager", "recommendation"],
        requires_mql=True,
    ),
    Golden(
        id="i1_district_rollup",
        question="What is the average task completion rate per district?",
        expected_sql=(
            "SELECT d.district_name, AVG(s.completion_rate) AS avg_completion "
            "FROM districts d JOIN stores s ON s.district_id = d.district_id "
            "GROUP BY d.district_name"
        ),
        expected_insight="The average store completion rate rolled up to each district.",
        tags=["district_manager", "rollup"],
    ),
    Golden(
        id="i2_low_completion_stores",
        question="Which stores have a completion rate below 70%?",
        expected_sql="SELECT store_name, completion_rate FROM stores WHERE completion_rate < 70",
        expected_insight=(
            "The underperforming stores whose completion rate is under 70% (corporate "
            "task-effectiveness view)."
        ),
        tags=["corporate", "adoption"],
    ),
    Golden(
        id="i3_store_comparison",
        question="Compare the completion rates of Store 118 and Store 202.",
        expected_sql=(
            "SELECT store_name, completion_rate FROM stores "
            "WHERE store_name IN ('Store 118', 'Store 202')"
        ),
        expected_insight="A side-by-side of Store 118 vs Store 202 completion rates.",
        tags=["district_manager", "comparison"],
    ),
]
