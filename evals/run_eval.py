#!/usr/bin/env python3
"""
Standalone evaluation runner (no pytest).

Runs every golden through the live agent and scores the Card with DeepEval's
`evaluate(...)`, printing a per-metric report. Requires live infra (Postgres +
ClickHouse + the configured LLM).

    python evals/run_eval.py
    # SQL-only metrics instead of Card metrics:
    python evals/run_eval.py --mode sql
"""

import argparse
import asyncio
import json
import sys

sys.path.insert(0, ".")

from deepeval import evaluate
from deepeval.test_case import LLMTestCase

from evals.dataset import GOLDENS
from evals.judge import get_judge_model
from evals.metrics import card_quality_metrics, sql_metrics
from evals.runner import get_runner


async def _build_cases(runner, mode: str) -> list[LLMTestCase]:
    cases: list[LLMTestCase] = []
    for g in GOLDENS:
        if mode == "sql" and g.requires_mql:
            continue
        try:
            r = await runner.run(g.question)
        except Exception as e:
            print(f"  ! skipping {g.id}: {e}")
            continue
        if mode == "sql":
            cases.append(LLMTestCase(
                input=g.question, actual_output=r["sql"],
                expected_output=g.expected_sql, name=g.id,
            ))
        else:
            cases.append(LLMTestCase(
                input=g.question, actual_output=r["card"].body,
                expected_output=g.expected_insight,
                retrieval_context=[json.dumps(r["rows"])] if r["rows"] else ["[]"],
                metadata={"card": r["card"].model_dump()}, name=g.id,
            ))
    return cases


def main() -> int:
    parser = argparse.ArgumentParser(description="Run DeepEval over the agent goldens.")
    parser.add_argument("--mode", choices=["card", "sql"], default="card")
    args = parser.parse_args()

    runner = get_runner()
    judge = get_judge_model()
    threshold = runner.settings.eval.threshold

    cases = asyncio.run(_build_cases(runner, args.mode))
    if not cases:
        print("No test cases produced (is the infra up?).")
        return 1

    metrics = sql_metrics(judge, threshold) if args.mode == "sql" else card_quality_metrics(judge, threshold)
    evaluate(test_cases=cases, metrics=metrics)
    return 0


if __name__ == "__main__":
    sys.exit(main())
