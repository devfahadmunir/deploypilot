"""
Markdown-aware chunking.

Policies are split at their "## " headings, so each chunk is one coherent
topic (e.g. "Refund window"). Long sections are further split at paragraph
boundaries. Every chunk starts with its document title and section heading,
which gives the embedding model context: "30 days" alone is ambiguous, but
"Refund Policy > Refund window: 30 days" is not.
"""

import hashlib
from dataclasses import dataclass

MAX_CHARS = 800  # body size per chunk; small enough to stay focused on one topic


@dataclass
class Chunk:
    doc: str
    section: str
    chunk_index: int
    content: str

    @property
    def content_hash(self) -> str:
        return hashlib.sha256(self.content.encode("utf-8")).hexdigest()


def _split_long(body: str, max_chars: int) -> list[str]:
    """Pack paragraphs into pieces of at most ~max_chars without breaking a paragraph."""
    paragraphs = [p.strip() for p in body.split("\n\n") if p.strip()]
    pieces, current = [], ""
    for para in paragraphs:
        if current and len(current) + len(para) + 2 > max_chars:
            pieces.append(current)
            current = para
        else:
            current = f"{current}\n\n{para}" if current else para
    if current:
        pieces.append(current)
    return pieces


def chunk_markdown(doc: str, text: str, max_chars: int = MAX_CHARS) -> list[Chunk]:
    lines = text.strip().splitlines()
    title = doc.replace("_", " ").title()
    if lines and lines[0].startswith("# "):
        title = lines[0][2:].strip()
        lines = lines[1:]

    # Group lines under their "## " heading. Text before the first heading belongs to the title.
    sections: list[tuple[str, list[str]]] = [(title, [])]
    for line in lines:
        if line.startswith("## "):
            sections.append((line[3:].strip(), []))
        else:
            sections[-1][1].append(line)

    chunks: list[Chunk] = []
    for heading, body_lines in sections:
        body = "\n".join(body_lines).strip()
        if not body:
            continue
        for piece in _split_long(body, max_chars):
            header = title if heading == title else f"{title} > {heading}"
            chunks.append(Chunk(doc=doc, section=heading, chunk_index=len(chunks), content=f"{header}\n\n{piece}"))
    return chunks
