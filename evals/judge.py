"""
DeepEval judge model.

DeepEval's LLM-as-judge metrics need a model to score with. We wrap the project's
own LlamaIndex LLM in a `DeepEvalBaseLLM` so the judge uses the SAME configurable
provider as the agent (offline by default). Override the judge independently with
EVAL_PROVIDER / EVAL_MODEL / EVAL_API_KEY / EVAL_BASE_URL — recommended, since a
small local model is an unreliable judge for the structured verdicts DeepEval needs.
"""

import json
import re
from functools import lru_cache

from deepeval.models import DeepEvalBaseLLM

from config.settings import get_settings, LLMSettings
from config.llm import build_llm_from


def _judge_llm_settings(settings) -> LLMSettings:
    """LLMSettings for the judge: EVAL_* values where set, else inherit LLM_*."""
    e, base = settings.eval, settings.llm
    return LLMSettings(
        provider=e.provider or base.provider,
        model=e.model or base.model,
        api_key=e.api_key or base.api_key,
        base_url=e.base_url or base.base_url,
        timeout=base.timeout,
        temperature=base.temperature,
        max_tokens=base.max_tokens,
    )


def _coerce(text: str, schema):
    """Return raw text, or parse the first JSON object/array into `schema`."""
    if schema is None:
        return text
    match = re.search(r"(\{.*\}|\[.*\])", text, re.DOTALL)
    raw = match.group(0) if match else text
    data = json.loads(raw)
    if hasattr(schema, "model_validate"):
        return schema.model_validate(data)
    return schema(**data)


class LlamaIndexJudge(DeepEvalBaseLLM):
    """Adapts a LlamaIndex LLM to DeepEval's judge interface."""

    def __init__(self, llm, model_name: str):
        self._llm = llm
        self._name = model_name

    def load_model(self):
        return self._llm

    def get_model_name(self) -> str:
        return self._name

    def generate(self, prompt: str, schema=None, *args, **kwargs):
        return _coerce(self._llm.complete(prompt).text, schema)

    async def a_generate(self, prompt: str, schema=None, *args, **kwargs):
        resp = await self._llm.acomplete(prompt)
        return _coerce(resp.text, schema)


@lru_cache
def get_judge_model() -> LlamaIndexJudge:
    """Build (and cache) the configured DeepEval judge model."""
    settings = get_settings()
    js = _judge_llm_settings(settings)
    return LlamaIndexJudge(build_llm_from(js), f"{js.provider}:{js.model}")
