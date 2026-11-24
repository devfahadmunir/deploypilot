-- Migration 004: double-refund protection and a log of every tool call.
-- Safe to run more than once.

-- The database itself refuses a second live refund for the same order.
CREATE UNIQUE INDEX IF NOT EXISTS uniq_live_refund_per_order ON refunds(order_id) WHERE status <> 'rejected';

CREATE TABLE IF NOT EXISTS tool_calls (
    id          BIGSERIAL   PRIMARY KEY,
    tool        TEXT        NOT NULL,
    arguments   JSONB       NOT NULL,
    client_id   TEXT        NOT NULL,
    outcome     TEXT        NOT NULL CHECK (outcome IN ('ok', 'error', 'input_required')),
    error       TEXT,
    duration_ms INTEGER     NOT NULL,
    created_at  TIMESTAMPTZ NOT NULL DEFAULT now()
);
