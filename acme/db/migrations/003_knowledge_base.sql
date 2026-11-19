-- Migration 003: vector store for the knowledge base (RAG).
-- Safe to run more than once.

CREATE EXTENSION IF NOT EXISTS vector;

CREATE TABLE IF NOT EXISTS kb_chunks (
    id            BIGSERIAL   PRIMARY KEY,
    doc           TEXT        NOT NULL,           -- source document, e.g. refund_policy
    section       TEXT        NOT NULL,           -- heading the chunk came from
    chunk_index   INTEGER     NOT NULL,           -- position within the document
    content       TEXT        NOT NULL,
    content_hash  TEXT        NOT NULL,
    model         TEXT        NOT NULL,           -- embedding model that produced the vector
    embedding     vector(384) NOT NULL,
    created_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (doc, chunk_index)
);

-- HNSW index for fast approximate nearest-neighbour search by cosine distance.
CREATE INDEX IF NOT EXISTS idx_kb_chunks_embedding
    ON kb_chunks USING hnsw (embedding vector_cosine_ops);
