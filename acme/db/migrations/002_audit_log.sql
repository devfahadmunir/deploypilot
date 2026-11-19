-- Migration 002: audit log for every write made through the MCP server.
-- Safe to run more than once.

CREATE TABLE IF NOT EXISTS audit_log (
    id          BIGSERIAL   PRIMARY KEY,
    tool        TEXT        NOT NULL,
    arguments   JSONB       NOT NULL,
    result      JSONB,
    created_at  TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_audit_tool_time ON audit_log(tool, created_at DESC);
