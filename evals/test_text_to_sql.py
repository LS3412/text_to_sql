"""
DeepEval test suite for the Text-to-SQL agent.

Run with the DeepEval pytest runner:
    deepeval test run evals/test_text_to_sql.py
or plain pytest (the offline deterministic tests always run):
    pytest evals/test_text_to_sql.py

The judged tests require live infra (Postgres + ClickHouse + the LLM). If the agent
runner can't be built, those tests SKIP rather than fail. The deterministic-metric
tests need nothing and validate the harness wiring offline.
"""

import asyncio
import json

import pytest

from deepeval import assert_test
from deepeval.test_case import LLMTestCase

from evals.dataset import GOLDENS
from evals.metrics import (
    CardSchemaMetric,
    SQLSafetyMetric,
    card_quality_metrics,
    sql_metrics,
)

_SQL_GOLDENS = [g for g in GOLDENS if not g.requires_mql]


# --------------------------------------------------------------------------- #
# Offline deterministic-metric tests (no infra, no judge) — always run
# --------------------------------------------------------------------------- #
def test_card_schema_metric_offline():
    good = LLMTestCase(
        input="q", actual_output="body",
        metadata={"card": {
            "title": "t", "body": "body", "card_kind": "metric",
            "data_payload": [{"label": "Completion", "value": 95.5, "unit": "%"}],
            "suggested_actions": [],
        }},
    )
    m = CardSchemaMetric()
    assert m.measure(good) == 1.0 and m.is_successful()

    bad = LLMTestCase(
        input="q", actual_output="body",
        metadata={"card": {
            "title": "t", "body": "body", "card_kind": "metric",
            "data_payload": [{"label": "only label"}],  # missing value/unit
        }},
    )
    m2 = CardSchemaMetric()
    assert m2.measure(bad) == 0.0 and not m2.is_successful()


def test_sql_safety_metric_offline():
    ok = LLMTestCase(input="q", actual_output="SELECT store_name FROM stores LIMIT 10")
    assert SQLSafetyMetric().measure(ok) == 1.0

    for unsafe in ("DROP TABLE stores", "SELECT 1; DROP TABLE stores", "SELECT * FROM stores"):
        m = SQLSafetyMetric()
        assert m.measure(LLMTestCase(input="q", actual_output=unsafe)) == 0.0, unsafe


# --------------------------------------------------------------------------- #
# Judged tests against the live agent (skip if infra is unavailable)
# --------------------------------------------------------------------------- #
@pytest.fixture(scope="module")
def runner():
    try:
        from evals.runner import get_runner
        return get_runner()
    except Exception as e:  # DB/ClickHouse/LLM unreachable
        pytest.skip(f"agent infra unavailable: {e}")


@pytest.fixture(scope="module")
def judge():
    from evals.judge import get_judge_model
    return get_judge_model()


def _card_case(golden, result) -> LLMTestCase:
    card = result["card"]
    return LLMTestCase(
        input=golden.question,
        actual_output=card.body,
        expected_output=golden.expected_insight,
        retrieval_context=[json.dumps(result["rows"])] if result["rows"] else ["[]"],
        metadata={"card": card.model_dump()},
        name=golden.id,
    )


@pytest.mark.parametrize("golden", GOLDENS, ids=[g.id for g in GOLDENS])
def test_card_quality(golden, runner, judge):
    result = asyncio.run(runner.run(golden.question))
    threshold = runner.settings.eval.threshold
    assert_test(_card_case(golden, result), card_quality_metrics(judge, threshold))


@pytest.mark.parametrize("golden", _SQL_GOLDENS, ids=[g.id for g in _SQL_GOLDENS])
def test_sql_generation(golden, runner, judge):
    result = asyncio.run(runner.run(golden.question))
    tc = LLMTestCase(
        input=golden.question,
        actual_output=result["sql"],
        expected_output=golden.expected_sql,
        name=golden.id,
    )
    assert_test(tc, sql_metrics(judge, runner.settings.eval.threshold))
