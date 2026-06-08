"""
LLM provider factory.

Builds the LlamaIndex LLM that powers SQL generation (via NLSQLTableQueryEngine)
and Card synthesis, selected by provider (default: ollama). Imports are LAZY per
branch so the anthropic / openai integration packages stay optional — the app only
needs `llama-index-llms-ollama` to run in the default local mode.

`build_llm_from` operates on a bare LLMSettings so the eval harness can build a
separate judge model (see evals/judge.py) without duplicating provider wiring.
"""

from config.settings import Settings, LLMSettings


def build_llm_from(llm: LLMSettings):
    """Return a configured LlamaIndex LLM for the given LLM settings."""
    provider = llm.provider.lower()

    if provider == "ollama":
        from llama_index.llms.ollama import Ollama

        return Ollama(
            model=llm.model,
            base_url=llm.base_url,
            request_timeout=float(llm.timeout),
        )

    if provider == "anthropic":
        # pip install llama-index-llms-anthropic
        from llama_index.llms.anthropic import Anthropic

        return Anthropic(
            model=llm.model,
            api_key=llm.api_key or None,
            timeout=float(llm.timeout),
            max_tokens=llm.max_tokens,
        )

    if provider == "openai":
        # pip install llama-index-llms-openai
        from llama_index.llms.openai import OpenAI

        return OpenAI(
            model=llm.model,
            api_key=llm.api_key or None,
            timeout=float(llm.timeout),
        )

    raise ValueError(
        f"Unknown LLM provider '{llm.provider}'. Expected one of: ollama, anthropic, openai."
    )


def build_llm(settings: Settings):
    """Return the agent's LLM (built from settings.llm)."""
    return build_llm_from(settings.llm)
