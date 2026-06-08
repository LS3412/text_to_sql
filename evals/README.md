# Evaluation (DeepEval)

DeepEval-based eval harness for the A2A Text-to-SQL agent. It scores the agent on
the spec's target questions (and a few broader insights) along two axes:

| Layer | Metrics |
|-------|---------|
| **SQL generation** | `SQL Safety` (deterministic: read-only SELECT + LIMIT), `SQL Correctness` (G-Eval vs reference SQL) |
| **Card output** | `Card Schema` (deterministic: valid `Card`, concrete `card_kind`), `Answer Relevancy`, `Faithfulness` (body grounded in the DB rows), `Insight Correctness` (G-Eval vs expected insight) |

Files: [dataset.py](dataset.py) (goldens), [metrics.py](metrics.py),
[judge.py](judge.py) (judge model), [runner.py](runner.py) (in-process agent),
[test_text_to_sql.py](test_text_to_sql.py) (pytest), [run_eval.py](run_eval.py) (standalone).

## The judge model

LLM-judged metrics need a judge. By default it reuses the agent's `LLM_*` settings
(offline Ollama). **A small local model is an unreliable judge** — for meaningful
scores, point the judge at a stronger model via env (the agent itself can stay on
Ollama):

```env
EVAL_PROVIDER=anthropic
EVAL_MODEL=claude-sonnet-4-6
EVAL_API_KEY=...
```

(Then `pip install llama-index-llms-anthropic`.) The deterministic metrics
(`SQL Safety`, `Card Schema`) need no judge and always run.

## Running

```bash
pip install -r requirements.txt          # installs deepeval + pytest

# Offline sanity (deterministic metrics only — no infra needed):
pytest evals/test_text_to_sql.py -k offline

# Full suite (needs Postgres + ClickHouse + LLM up; judged tests skip if not):
deepeval test run evals/test_text_to_sql.py

# Standalone report:
python evals/run_eval.py            # Card metrics
python evals/run_eval.py --mode sql # SQL metrics
```

Tip: `export DEEPEVAL_TELEMETRY_OPT_OUT=YES` to silence telemetry when running fully
offline. Questions flagged `requires_mql` in the dataset (root-cause, "15 minutes")
need historical/event data that the SQL-only agent can't fully serve yet — they're
expected to degrade to a partial/text answer, not fabricate.
