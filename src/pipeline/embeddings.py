"""
Dedicated embedding model for the RAG path (stage 3) ONLY.

This is deliberately separate from the global ``llama_index.core.Settings.embed_model``
(which stays ``MockEmbedding(1536)`` so the LlamaIndex NLSQL internals are unaffected).
It mirrors config/llm.py's lazy-import provider-factory pattern: if the integration
package or the Ollama endpoint is unavailable, ``build_embed_model`` returns ``None`` and
the retriever degrades to keyword-only — no exception ever propagates.
"""

from __future__ import annotations

import logging
from typing import Optional

logger = logging.getLogger(__name__)


def build_embed_model(settings) -> Optional[object]:
    """Build the Ollama embedding model, or return None if unavailable."""
    embed = settings.embedding
    if not getattr(embed, "enabled", True):
        return None
    provider = embed.provider.lower()
    try:
        if provider == "ollama":
            from llama_index.embeddings.ollama import OllamaEmbedding

            return OllamaEmbedding(model_name=embed.model, base_url=embed.base_url)
        logger.warning("Unknown embedding provider '%s'; vector layer disabled", provider)
        return None
    except Exception as exc:  # ImportError or construction failure
        logger.warning("Embedding model unavailable (%s); vector layer disabled", exc)
        return None


async def aembed_query(model, text: str) -> Optional[list[float]]:
    """Embed a query string asynchronously; None on any failure."""
    if model is None:
        return None
    try:
        return await model.aget_query_embedding(text)
    except Exception as exc:
        logger.warning("aembed_query failed (%s); skipping vector signal", exc)
        return None


def embed_text(model, text: str) -> Optional[list[float]]:
    """Synchronous embed (used by the offline indexer script); None on failure."""
    if model is None:
        return None
    try:
        return model.get_text_embedding(text)
    except Exception as exc:
        logger.warning("embed_text failed (%s)", exc)
        return None


def embed_available(model) -> bool:
    """One-shot probe: can we actually get an embedding from this model?"""
    if model is None:
        return False
    try:
        vec = model.get_text_embedding("healthcheck")
        return bool(vec)
    except Exception:
        return False
