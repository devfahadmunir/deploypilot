# DeployPilot

**An MCP server that connects AI assistants to a company's internal systems, built the way a Forward Deployed Engineer would ship it for a client.**

The client is **Acme Store**, a fictional e-commerce company. DeployPilot exposes Acme's order database, support tickets, and internal knowledge base to any MCP-compatible AI client, with safety controls for risky actions like refunds.

> 🚧 Work in progress. Built phase by phase; see the roadmap below.

---

## Phase 0: Acme Store backend

Acme's "systems" consist of:

- **PostgreSQL database** (with pgvector) containing customers, products, orders, order items, refunds, and support tickets
- **Knowledge base** of internal documents in `acme/knowledge_base/` (refund policy, shipping, returns, warranty, membership, support guidelines, FAQ)

All data is synthetic and generated reproducibly with Faker (fixed seed).

### Setup

```bash
# 1. Start the database
docker compose up -d

# 2. Create a virtual environment and install dependencies
python -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt

# 3. Configure and seed
cp .env.example .env
python acme/db/seed.py
```

Expected output:

```
  customers      303
  products        38
  orders        1005
  order_items   2499
  refunds         40
  tickets        153
Done. Acme Store is open for business.
```

Re-running `seed.py` wipes and regenerates the same data.

### Demo customers (planted edge cases)

| Customer | Tier | Scenario |
|---|---|---|
| Sarah Khan (`sarah.khan@example.com`) | Standard | One order delivered 10 days ago (refundable) and one delivered 45 days ago (outside the 30-day window) |
| Ali Raza (`ali.raza@example.com`) | Premium | Order delivered 50 days ago (still refundable under the 60-day premium window) and 3 open tickets (should be escalated) |
| John Miller (`john.miller@example.com`) | Standard | Order shipped 12 days ago and not delivered (late) and an order already refunded |

These cases test whether the AI applies business rules correctly, not just whether it can query data.

---

## Phase 1: MCP server (read-only)

`server/acme_server.py` exposes Acme's systems through MCP, using the official Python SDK (v2, `MCPServer`).

| Primitive | Name | What it does |
|---|---|---|
| Tool | `get_customer(email)` | Profile, tier, order count, total spent, open tickets |
| Tool | `list_orders(email, limit)` | Recent orders with status and dates |
| Tool | `get_order(order_id)` | Items, customer tier, days since delivery, refunds |
| Tool | `list_tickets(email, include_resolved)` | Support tickets |
| Resource | `acme://policies` | Index of company policies |
| Resource | `acme://policies/{name}` | One policy document |
| Prompt | `summarize_customer(email)` | Account summary workflow for support agents |
| Prompt | `check_refund_eligibility(order_id)` | Applies the refund policy to an order |

**Design decisions**
- **Read-only by design:** every tool is annotated `read_only_hint=True`, and the database connection itself is opened in read-only mode, so writes are refused at the database level, not just by convention. Write actions arrive in Phase 4 with permission and confirmation controls.
- **Safe errors:** expected problems (invalid email, unknown order) raise `ToolError`, so the model gets an actionable message. Unexpected failures are logged server-side and the model only sees a generic error, so internals never leak.
- **Allow-listed resources:** policy names are checked against the knowledge base, which blocks path-traversal tricks.
- **stdio-safe logging:** all logs go to stderr, because stdout carries the MCP protocol.

### Run the smoke test

```bash
python scripts/smoke_test.py
```

This starts the server, connects as an MCP client, and calls every tool, resource, and prompt.

### Explore it in the MCP Inspector

Requires Node.js (for `npx`).

```bash
mcp dev server/acme_server.py
```

### Connect it to Claude Desktop

Open **Claude Desktop → Settings → Developer → Edit Config** and add (use your own absolute paths):

```json
{
  "mcpServers": {
    "acme-store": {
      "command": "/ABSOLUTE/PATH/TO/deploypilot/.venv/bin/python",
      "args": ["/ABSOLUTE/PATH/TO/deploypilot/server/acme_server.py"]
    }
  }
}
```

Restart Claude Desktop, then try: *"Is Ali Raza's order eligible for a refund? His email is ali.raza@example.com."*

---

## Phase 2: First write action, audit log, and tests

The AI can now **open support tickets**, and every write it makes is recorded.

| Addition | Details |
|---|---|
| `create_ticket` tool | Validated write: subject/description length and priority are enforced by the tool schema; an order can only be linked if it belongs to the same customer |
| Audit log | Every write is recorded in `audit_log`, **in the same transaction** as the write, so neither can happen without the other |
| Connection pool | `server/db.py` reuses up to 5 database connections instead of opening one per call |
| Read-only transactions | Read queries run in `READ ONLY` transactions, so PostgreSQL rejects any write on the read path |
| Migrations | Schema changes ship as files in `acme/db/migrations/` |
| Tests | `pytest` suite covering business facts, error handling, read-only enforcement, and write validation |

**Design decisions**
- **Schema-level validation:** constraints like `priority: Literal["low","medium","high"]` and `min_length` appear in the tool's JSON schema, so the model sees the rules up front, and invalid calls are rejected before any code runs.
- **Ownership checks:** the server, not the model, verifies that an order belongs to the customer. Never trust the model to enforce data boundaries.
- **Atomic writes:** if validation fails mid-way, the whole transaction rolls back, so no partial tickets or orphaned audit rows.
- **Write annotations:** `create_ticket` is marked `read_only_hint=False`, so clients like Claude Desktop ask the user before running it.

### Upgrade an existing Phase 1 database

`schema.sql` only runs when the database is first created, so apply the migration manually:

```bash
docker exec -i acme-db psql -U acme -d acme < acme/db/migrations/002_audit_log.sql
```

### Run the tests

```bash
pip install -r requirements.txt
pytest -v
```

The database must be running and seeded. Tests clean up any tickets they create.

---

## Phase 3: RAG over the knowledge base

The AI no longer has to open whole documents. It **searches** Acme's policies by meaning.

```
Policies (.md) → markdown-aware chunking → embeddings (Ollama, local) → pgvector (HNSW)
                                                                            │
            question → embed query → cosine similarity search ◀──────────────┘
                                            │
                                search_knowledge_base (MCP tool)
                                → top passages + scores + source URIs
```

| Piece | Details |
|---|---|
| Chunking | `server/rag/chunking.py` splits at `##` headings, then at paragraphs if a section is long. Each chunk carries its title and section ("Refund Policy > Refund window") so the embedding has context |
| Embeddings | `all-minilm` (384 dimensions) served by **Ollama** as a separate local service, called over HTTP with the standard library. Provider is configurable (`EMBEDDING_PROVIDER=ollama` or `fastembed`) |
| Vector store | `kb_chunks` table in the same PostgreSQL database, with a pgvector HNSW index on cosine distance |
| Ingestion | `scripts/ingest_kb.py` rebuilds the index in one transaction, so search never sees a half-built index |
| MCP tool | `search_knowledge_base(question, top_k)` returns passages with similarity scores and `acme://policies/...` source URIs to cite |
| Evaluation | `scripts/eval_retrieval.py` measures Hit@1, Hit@3, and MRR on 14 labeled questions in `evals/retrieval_set.json` |

**Design decisions**
- **One database for everything:** pgvector keeps business data and embeddings in PostgreSQL, so there's no extra vector database to run, secure, or back up.
- **Embeddings as a separate local service:** the model runs in Ollama, so it can be swapped, upgraded, or scaled without touching the MCP server, and the server has no heavy ML dependencies. Documents and questions never leave the machine.
- **No mixed vectors:** every chunk records the model that embedded it, and search only compares vectors from the current model. Switching models without re-indexing produces a clear error instead of silently wrong results.
- **Dimension check:** a model returning anything other than 384-dimensional vectors fails loudly at ingestion.
- **Measured, not assumed:** retrieval quality is tracked with an eval set, so changes to chunking or models can be compared with numbers.

### Set up

```bash
# 1. Install Ollama and pull the embedding model (one time)
brew install ollama
ollama serve            # or open the Ollama app; leave it running
ollama pull all-minilm  # in a second terminal

# 2. Add the vector table and index the knowledge base
docker exec -i acme-db psql -U acme -d acme < acme/db/migrations/003_knowledge_base.sql
python scripts/ingest_kb.py

# 3. Measure and test
python scripts/eval_retrieval.py
pytest -v
```

### Retrieval results

| Metric | Score |
|---|---|
| Hit@1 | `[fill in from eval_retrieval.py]` |
| Hit@3 | `[fill in]` |
| MRR | `[fill in]` |

---

## Roadmap

- [x] **Phase 0:** Acme backend (database, seed data, knowledge base)
- [x] **Phase 1:** MCP fundamentals: tools, resources, prompts
- [x] **Phase 2:** First write action, audit log, connection pooling, migrations, tests
- [x] **Phase 3:** RAG over the knowledge base with pgvector
- [ ] **Phase 4:** Remote transport, auth, permissions, and confirmation for risky actions
- [ ] **Phase 5:** Custom agent client
- [ ] **Phase 6:** Evals, cost tracking, Docker deployment

---

## Author

**Fahad Munir**, AI/ML Engineer | Forward Deployed Engineer
[LinkedIn](https://www.linkedin.com/in/devfahadmunir) · [GitHub](https://github.com/devfahadmunir)
