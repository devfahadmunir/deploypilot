"""
Index the Acme knowledge base for semantic search.

Reads every Markdown file in acme/knowledge_base/, chunks it, embeds each
chunk with the local embedding provider (Ollama by default), and stores the vectors in PostgreSQL (pgvector).

Usage:
    python scripts/ingest_kb.py

Re-run it whenever the documents change. It rebuilds the index in a single
transaction, so search never sees a half-built index.
"""

import sys
import time
from collections import Counter
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from server.db import PROJECT_ROOT, write_transaction  # noqa: E402
from server.rag.chunking import chunk_markdown  # noqa: E402
from server.rag.embeddings import MODEL_NAME, EmbeddingError, embed_passages, to_pgvector  # noqa: E402

KNOWLEDGE_BASE = PROJECT_ROOT / "acme" / "knowledge_base"


def main() -> None:
    chunks = []
    for path in sorted(KNOWLEDGE_BASE.glob("*.md")):
        chunks.extend(chunk_markdown(path.stem, path.read_text(encoding="utf-8")))
    print(f"Chunked {len({c.doc for c in chunks})} documents into {len(chunks)} chunks.")

    print(f"Embedding with {MODEL_NAME}...")
    start = time.perf_counter()
    try:
        vectors = embed_passages([c.content for c in chunks])
    except EmbeddingError as exc:
        sys.exit(f"Embedding failed: {exc}")
    print(f"Embedded in {time.perf_counter() - start:.1f}s.")

    with write_transaction() as conn:
        conn.execute("DELETE FROM kb_chunks")
        for chunk, vector in zip(chunks, vectors):
            conn.execute(
                """INSERT INTO kb_chunks (doc, section, chunk_index, content, content_hash, model, embedding)
                   VALUES (%s, %s, %s, %s, %s, %s, %s::vector)""",
                (chunk.doc, chunk.section, chunk.chunk_index, chunk.content,
                 chunk.content_hash, MODEL_NAME, to_pgvector(vector)),
            )

    for doc, n in sorted(Counter(c.doc for c in chunks).items()):
        print(f"  {doc:<20} {n:>3} chunks")
    print("Knowledge base indexed.")


if __name__ == "__main__":
    main()
