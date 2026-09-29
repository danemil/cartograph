"""Tool 7: embed_graph."""

from __future__ import annotations

import logging
from typing import Any

from ..embeddings import EmbeddingStore, embed_all_nodes
from ..incremental import get_db_path
from ._common import (
    _get_store,
)

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Tool 7: embed_graph
# ---------------------------------------------------------------------------


def embed_graph(
    repo_root: str | None = None,
    model: str | None = None,
    provider: str | None = None,
) -> dict[str, Any]:
    """Compute vector embeddings for all graph nodes to enable semantic search.

    Requires: ``pip install cartograph[embeddings]`` (local provider only;
    cloud providers like ``openai`` / ``google`` / ``minimax`` / ``voyage`` use
    stdlib ``urllib``).
    Default model: all-MiniLM-L6-v2. Override via ``model`` param or
    provider-specific env vars such as CRG_EMBEDDING_MODEL, CRG_OPENAI_MODEL, or
    CRG_VOYAGE_MODEL.
    Changing the model or provider re-embeds all nodes automatically.

    Only embeds nodes that don't already have up-to-date embeddings.

    Args:
        repo_root: Repository root path. Auto-detected if omitted.
        model: Embedding model name. For local: HuggingFace ID or path;
               for openai: model ID (e.g. ``text-embedding-3-small``);
               for google: Gemini model ID; for voyage: Voyage model ID
               (e.g. ``voyage-code-3``). Falls back to CRG_EMBEDDING_MODEL /
               CRG_OPENAI_MODEL / CRG_VOYAGE_MODEL env vars as appropriate.
        provider: Provider name: ``local`` (default), ``openai``, ``google``,
                  ``minimax``, or ``voyage``. ``openai`` requires CRG_OPENAI_BASE_URL +
                  CRG_OPENAI_API_KEY + CRG_OPENAI_MODEL env vars and accepts
                  any OpenAI-compatible endpoint (real OpenAI, Azure, new-api,
                  LiteLLM, vLLM, LocalAI, Ollama openai-mode, etc.).
                  ``voyage`` requires VOYAGE_API_KEY and defaults to
                  voyage-code-3 unless a model arg or CRG_VOYAGE_MODEL is
                  supplied.

    Returns:
        Number of nodes embedded and total embedding count.
    """
    store, root = _get_store(repo_root)
    try:
        db_path = get_db_path(root)
        try:
            emb_store = EmbeddingStore(db_path, provider=provider, model=model)
        except ValueError as exc:
            # Unknown provider name or missing provider env vars — surface
            # as a structured error rather than a traceback.
            logger.error("embed_graph: %s", exc)
            return {"status": "error", "error": str(exc)}
        try:
            if not emb_store.available:
                if provider in ("openai", "google", "minimax", "voyage"):
                    err = (
                        f"The '{provider}' embedding provider is not available. "
                        "Check the required environment variables "
                        "(see README and `get_provider()` docstring) and that "
                        "the endpoint is reachable."
                    )
                else:
                    err = (
                        "The local embedding provider needs sentence-transformers. "
                        "Install with: pip install cartograph[embeddings] — "
                        "or switch provider to 'openai' / 'google' / 'minimax' "
                        "/ 'voyage'."
                    )
                return {"status": "error", "error": err}

            newly_embedded = embed_all_nodes(store, emb_store)
            total = emb_store.count()

            return {
                "status": "ok",
                "summary": (
                    f"Embedded {newly_embedded} new node(s). "
                    f"Total embeddings: {total}. "
                    "Semantic search is now active."
                ),
                "newly_embedded": newly_embedded,
                "total_embeddings": total,
            }
        finally:
            emb_store.close()
    finally:
        store.close()
