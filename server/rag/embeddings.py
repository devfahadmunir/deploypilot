"""
Text embeddings, from a configurable local provider.

EMBEDDING_PROVIDER=ollama     (default) Ollama runs the model as a separate local
                              service; we call it over HTTP with the standard library,
                              so this works on any Python version.
EMBEDDING_PROVIDER=fastembed  Runs the model in-process via ONNX
                              (needs `pip install fastembed`; Python <= 3.14 for now).

Either way, everything stays on this machine: client documents and customer
questions are never sent to a third party.
"""

import json
import os
import urllib.error
import urllib.request
from pathlib import Path

from dotenv import load_dotenv

PROJECT_ROOT = Path(__file__).resolve().parents[2]
load_dotenv(PROJECT_ROOT / ".env")

PROVIDER = os.getenv("EMBEDDING_PROVIDER", "ollama").lower()
OLLAMA_URL = os.getenv("OLLAMA_URL", "http://localhost:11434").rstrip("/")
EMBEDDING_DIM = 384  # must match vector(384) in the kb_chunks table

_DEFAULT_MODELS = {"ollama": "all-minilm", "fastembed": "BAAI/bge-small-en-v1.5"}
if PROVIDER not in _DEFAULT_MODELS:
    raise ValueError(f"Unknown EMBEDDING_PROVIDER '{PROVIDER}'. Use 'ollama' or 'fastembed'.")

MODEL = os.getenv("EMBEDDING_MODEL", _DEFAULT_MODELS[PROVIDER])
# Stored with every chunk, so vectors from different models are never mixed.
MODEL_NAME = f"{PROVIDER}/{MODEL}"


class EmbeddingError(RuntimeError):
    """The embedding service is unavailable or returned something unexpected."""


# ---------------------------------------------------------------------------
# Ollama provider
# ---------------------------------------------------------------------------
def _ollama_embed(texts: list[str]) -> list[list[float]]:
    body = json.dumps({"model": MODEL, "input": texts}).encode("utf-8")
    request = urllib.request.Request(
        f"{OLLAMA_URL}/api/embed", data=body, headers={"Content-Type": "application/json"}
    )
    try:
        with urllib.request.urlopen(request, timeout=60) as response:
            data = json.loads(response.read())
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", "replace")
        hint = f" Run: ollama pull {MODEL}" if exc.code == 404 else ""
        raise EmbeddingError(f"Ollama returned HTTP {exc.code}: {detail}.{hint}") from exc
    except (urllib.error.URLError, TimeoutError) as exc:
        raise EmbeddingError(
            f"Cannot reach Ollama at {OLLAMA_URL}. Is it running? (Start the Ollama app or run: ollama serve)"
        ) from exc
    return data["embeddings"]


# ---------------------------------------------------------------------------
# fastembed provider (optional)
# ---------------------------------------------------------------------------
_fastembed_model = None


def _fastembed(texts: list[str], kind: str) -> list[list[float]]:
    global _fastembed_model
    if _fastembed_model is None:
        try:
            from fastembed import TextEmbedding
        except ImportError as exc:
            raise EmbeddingError("fastembed is not installed. Run: pip install fastembed") from exc
        _fastembed_model = TextEmbedding(MODEL, cache_dir=str(PROJECT_ROOT / ".cache" / "fastembed"))
    # bge models encode questions and documents slightly differently.
    vectors = _fastembed_model.query_embed(texts) if kind == "query" else _fastembed_model.passage_embed(texts)
    return [v.tolist() for v in vectors]


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------
def _embed(texts: list[str], kind: str) -> list[list[float]]:
    vectors = _ollama_embed(texts) if PROVIDER == "ollama" else _fastembed(texts, kind)
    if len(vectors) != len(texts) or any(len(v) != EMBEDDING_DIM for v in vectors):
        dims = sorted({len(v) for v in vectors})
        raise EmbeddingError(
            f"{MODEL_NAME} returned {dims}-dimensional vectors, but the database expects {EMBEDDING_DIM}. "
            "Use a 384-dimension model or change vector(384) in the schema."
        )
    return vectors


def embed_passages(texts: list[str]) -> list[list[float]]:
    return _embed(texts, "passage")


def embed_query(text: str) -> list[float]:
    return _embed([text], "query")[0]


def to_pgvector(vector: list[float]) -> str:
    """Format a vector as a pgvector literal, e.g. '[0.12,-0.03,...]'."""
    return "[" + ",".join(f"{x:.6f}" for x in vector) + "]"
