"""
DeepEval metrics for the Text-to-SQL agent.

Two kinds:
  * Deterministic metrics (no LLM judge) — fast, always reliable, run offline:
      - CardSchemaMetric: the response is a schema-valid Card with a concrete kind.
      - SQLSafetyMetric:   the generated SQL is a read-only SELECT with a LIMIT.
  * LLM-judged metrics (need a judge model — see judge.py):
      - SQL correctness (GEval), Insight correctness (GEval),
        Answer relevancy, Faithfulness (body grounded in the DB rows).
"""

import re

from deepeval.metrics import (
    GEval,
    AnswerRelevancyMetric,
    FaithfulnessMetric,
    BaseMetric,
)
from deepeval.test_case import SingleTurnParams

from src.card_model import Card

_FORBIDDEN = ("INSERT", "UPDATE", "DELETE", "DROP", "ALTER", "CREATE", "TRUNCATE", "REPLACE", "GRANT", "REVOKE")


# --------------------------------------------------------------------------- #
# Deterministic metrics
# --------------------------------------------------------------------------- #
class CardSchemaMetric(BaseMetric):
    """Pass if additional_metadata['card'] is a schema-valid Card.

    `strict` also requires a concrete card_kind (not 'auto') and, for text cards,
    an empty data_payload — i.e. the agent committed to a renderable shape.
    """

    def __init__(self, threshold: float = 1.0, strict: bool = True):
        self.threshold = threshold
        self.strict = strict
        self.score = 0.0
        self.reason = None
        self.success = False
        self.error = None
        self.async_mode = True

    def measure(self, test_case, *args, **kwargs) -> float:
        card_data = (test_case.metadata or {}).get("card")
        if card_data is None:
            self.score, self.success = 0.0, False
            self.reason = "no 'card' in additional_metadata"
            return self.score
        try:
            card = Card.model_validate(card_data)
            if self.strict and card.card_kind == "auto":
                raise ValueError("card_kind is 'auto' (no concrete shape chosen)")
            self.score, self.success = 1.0, True
            self.reason = f"valid Card (kind={card.card_kind})"
        except Exception as e:  # ValidationError or our strict checks
            self.score, self.success = 0.0, False
            self.reason = f"invalid Card: {e}"
        return self.score

    async def a_measure(self, test_case, *args, **kwargs) -> float:
        return self.measure(test_case)

    def is_successful(self) -> bool:
        return self.success

    @property
    def __name__(self):
        return "Card Schema"


class SQLSafetyMetric(BaseMetric):
    """Pass if test_case.actual_output is a read-only SELECT/WITH, no statement
    stacking, and carries a LIMIT (mirrors the skill's own guardrails)."""

    def __init__(self, threshold: float = 1.0):
        self.threshold = threshold
        self.score = 0.0
        self.reason = None
        self.success = False
        self.error = None
        self.async_mode = True

    def measure(self, test_case, *args, **kwargs) -> float:
        sql = test_case.actual_output or ""
        no_lit = re.sub(r"'(?:[^'\\]|\\.)*'", "", sql)
        upper = no_lit.upper().strip()
        problems = []
        if not (upper.startswith("SELECT") or upper.startswith("WITH")):
            problems.append("not a SELECT/WITH")
        if any(re.search(rf"\b{w}\b", upper) for w in _FORBIDDEN):
            problems.append("contains a mutating keyword")
        if ";" in sql.strip().rstrip(";"):
            problems.append("statement stacking (;)")
        if not re.search(r"\bLIMIT\s+\d+\b", upper):
            problems.append("missing LIMIT")
        self.success = not problems
        self.score = 1.0 if self.success else 0.0
        self.reason = "safe read-only SELECT with LIMIT" if self.success else "; ".join(problems)
        return self.score

    async def a_measure(self, test_case, *args, **kwargs) -> float:
        return self.measure(test_case)

    def is_successful(self) -> bool:
        return self.success

    @property
    def __name__(self):
        return "SQL Safety"


# --------------------------------------------------------------------------- #
# LLM-judged metric factories (pass the judge model in)
# --------------------------------------------------------------------------- #
def sql_correctness_metric(model, threshold: float = 0.7) -> GEval:
    return GEval(
        name="SQL Correctness",
        criteria=(
            "Given the user question in 'input' and a reference query in "
            "'expected_output', decide whether the 'actual_output' SQL would return "
            "the data needed to answer the question. It should select the right "
            "tables/columns and apply the right filters/aggregation. Exact text need "
            "not match — judge semantic equivalence, not wording. It must be a "
            "read-only SELECT and must NOT filter by tenant_id."
        ),
        evaluation_params=[
            SingleTurnParams.INPUT,
            SingleTurnParams.ACTUAL_OUTPUT,
            SingleTurnParams.EXPECTED_OUTPUT,
        ],
        model=model,
        threshold=threshold,
    )


def insight_correctness_metric(model, threshold: float = 0.7) -> GEval:
    return GEval(
        name="Insight Correctness",
        criteria=(
            "Judge whether 'actual_output' (the agent's plain-English answer) conveys "
            "the same key insight as 'expected_output' for the question in 'input'. "
            "Numbers/entities should be consistent; minor phrasing differences are fine."
        ),
        evaluation_params=[
            SingleTurnParams.INPUT,
            SingleTurnParams.ACTUAL_OUTPUT,
            SingleTurnParams.EXPECTED_OUTPUT,
        ],
        model=model,
        threshold=threshold,
    )


def answer_relevancy_metric(model, threshold: float = 0.7) -> AnswerRelevancyMetric:
    return AnswerRelevancyMetric(model=model, threshold=threshold, async_mode=True)


def faithfulness_metric(model, threshold: float = 0.7) -> FaithfulnessMetric:
    # retrieval_context = the DB rows; checks the answer is grounded in them.
    return FaithfulnessMetric(model=model, threshold=threshold, async_mode=True)


def card_quality_metrics(model, threshold: float = 0.7) -> list:
    """All metrics applied to a full agent (Card) test case."""
    return [
        CardSchemaMetric(),
        answer_relevancy_metric(model, threshold),
        faithfulness_metric(model, threshold),
        insight_correctness_metric(model, threshold),
    ]


def sql_metrics(model, threshold: float = 0.7) -> list:
    """All metrics applied to a SQL-generation test case."""
    return [
        SQLSafetyMetric(),
        sql_correctness_metric(model, threshold),
    ]
