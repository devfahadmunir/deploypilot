-- Acme Store: backend schema
-- Runs automatically the first time the Postgres container starts.

CREATE EXTENSION IF NOT EXISTS vector;  -- used later for RAG (Phase 3)

CREATE TABLE IF NOT EXISTS customers (
    id          SERIAL PRIMARY KEY,
    name        TEXT        NOT NULL,
    email       TEXT        NOT NULL UNIQUE,
    phone       TEXT,
    city        TEXT,
    country     TEXT,
    tier        TEXT        NOT NULL DEFAULT 'standard'
                CHECK (tier IN ('standard', 'premium')),
    created_at  TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS products (
    id        SERIAL PRIMARY KEY,
    sku       TEXT          NOT NULL UNIQUE,
    name      TEXT          NOT NULL,
    category  TEXT          NOT NULL,
    price     NUMERIC(10,2) NOT NULL CHECK (price >= 0),
    stock     INTEGER       NOT NULL DEFAULT 0 CHECK (stock >= 0)
);

CREATE TABLE IF NOT EXISTS orders (
    id            SERIAL PRIMARY KEY,
    customer_id   INTEGER       NOT NULL REFERENCES customers(id),
    status        TEXT          NOT NULL
                  CHECK (status IN ('pending', 'paid', 'shipped', 'delivered', 'cancelled', 'refunded')),
    total         NUMERIC(10,2) NOT NULL DEFAULT 0,
    created_at    TIMESTAMPTZ   NOT NULL,
    shipped_at    TIMESTAMPTZ,
    delivered_at  TIMESTAMPTZ
);

CREATE TABLE IF NOT EXISTS order_items (
    id          SERIAL PRIMARY KEY,
    order_id    INTEGER       NOT NULL REFERENCES orders(id) ON DELETE CASCADE,
    product_id  INTEGER       NOT NULL REFERENCES products(id),
    quantity    INTEGER       NOT NULL CHECK (quantity > 0),
    unit_price  NUMERIC(10,2) NOT NULL
);

CREATE TABLE IF NOT EXISTS refunds (
    id          SERIAL PRIMARY KEY,
    order_id    INTEGER       NOT NULL REFERENCES orders(id),
    amount      NUMERIC(10,2) NOT NULL CHECK (amount > 0),
    reason      TEXT,
    status      TEXT          NOT NULL DEFAULT 'completed'
                CHECK (status IN ('pending', 'completed', 'rejected')),
    created_at  TIMESTAMPTZ   NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS tickets (
    id           SERIAL PRIMARY KEY,
    customer_id  INTEGER     NOT NULL REFERENCES customers(id),
    order_id     INTEGER     REFERENCES orders(id),
    subject      TEXT        NOT NULL,
    description  TEXT,
    status       TEXT        NOT NULL DEFAULT 'open'
                 CHECK (status IN ('open', 'in_progress', 'resolved')),
    priority     TEXT        NOT NULL DEFAULT 'medium'
                 CHECK (priority IN ('low', 'medium', 'high')),
    created_at   TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at   TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_orders_customer  ON orders(customer_id);
CREATE INDEX IF NOT EXISTS idx_items_order      ON order_items(order_id);
CREATE INDEX IF NOT EXISTS idx_tickets_customer ON tickets(customer_id);
CREATE INDEX IF NOT EXISTS idx_refunds_order    ON refunds(order_id);

-- Added in Phase 2 (existing databases: apply acme/db/migrations/002_audit_log.sql)
CREATE TABLE IF NOT EXISTS audit_log (
    id          BIGSERIAL   PRIMARY KEY,
    tool        TEXT        NOT NULL,
    arguments   JSONB       NOT NULL,
    result      JSONB,
    created_at  TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS idx_audit_tool_time ON audit_log(tool, created_at DESC);

-- Added in Phase 3 (existing databases: apply acme/db/migrations/003_knowledge_base.sql)
CREATE TABLE IF NOT EXISTS kb_chunks (
    id            BIGSERIAL   PRIMARY KEY,
    doc           TEXT        NOT NULL,
    section       TEXT        NOT NULL,
    chunk_index   INTEGER     NOT NULL,
    content       TEXT        NOT NULL,
    content_hash  TEXT        NOT NULL,
    model         TEXT        NOT NULL,
    embedding     vector(384) NOT NULL,
    created_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (doc, chunk_index)
);
CREATE INDEX IF NOT EXISTS idx_kb_chunks_embedding ON kb_chunks USING hnsw (embedding vector_cosine_ops);
