"""
Tests for the RAG pipeline.

Chunking tests need nothing. Search tests need the knowledge base indexed:
    python scripts/ingest_kb.py
They are skipped automatically if it hasn't been indexed yet.
"""

import pytest

from server.db import query
from server.rag.chunking import MAX_CHARS, chunk_markdown

REFUND_DOC = """# Acme Store Refund Policy

## Refund window
- Standard customers: 30 days.
- Premium customers: 60 days.

## Processing time
Refunds take 5-7 business days.
"""


# --- Chunking (no database or model needed) ----------------------------------

def test_chunks_follow_headings():
    chunks = chunk_markdown("refund_policy", REFUND_DOC)
    assert [c.section for c in chunks] == ["Refund window", "Processing time"]


def test_chunks_carry_title_and_section_context():
    first = chunk_markdown("refund_policy", REFUND_DOC)[0]
    assert first.content.startswith("Acme Store Refund Policy > Refund window")
    assert "Premium customers: 60 days" in first.content


def test_long_sections_are_split_without_breaking_paragraphs():
    paragraphs = [f"Paragraph {i}. " + "word " * 40 for i in range(10)]
    doc = "# Big Doc\n\n## Long section\n\n" + "\n\n".join(paragraphs)
    chunks = chunk_markdown("big_doc", doc)
    assert len(chunks) > 1
    for chunk in chunks:
        body = chunk.content.split("\n\n", 1)[1]
        assert len(body) <= MAX_CHARS
    joined = " ".join(c.content for c in chunks)
    assert all(p.strip() in joined for p in paragraphs)  # nothing lost


# --- Search (needs the indexed knowledge base) --------------------------------

def _kb_indexed() -> bool:
    try:
        return query("SELECT COUNT(*) AS n FROM kb_chunks")[0]["n"] > 0
    except Exception:
        return False


needs_kb = pytest.mark.skipif(not _kb_indexed(), reason="run scripts/ingest_kb.py first")


@needs_kb
def test_refund_question_finds_refund_policy():
    from server.acme_server import search_knowledge_base
    results = search_knowledge_base("Can a premium customer still get a refund after 50 days?", top_k=3)
    docs = [r["doc"] for r in results]
    # Both documents legitimately answer this: membership lists the 60-day window
    # as a premium benefit. What matters is that the refund policy is retrieved.
    assert docs[0] in {"refund_policy", "membership"}
    assert "refund_policy" in docs
    assert all(r["source"] == f"acme://policies/{r['doc']}" for r in results)


@needs_kb
def test_results_are_sorted_by_score():
    from server.acme_server import search_knowledge_base
    results = search_knowledge_base("How long does shipping take?", top_k=5)
    scores = [r["score"] for r in results]
    assert scores == sorted(scores, reverse=True)
