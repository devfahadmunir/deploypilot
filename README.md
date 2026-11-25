# DeployPilot

**An MCP server that connects AI assistants to a company's internal systems, built the way a Forward Deployed Engineer would ship it for a client.**

The client is **Acme Store**, a fictional e-commerce company. DeployPilot exposes Acme's order database, support tickets, and internal knowledge base to any MCP-compatible AI client, with safety controls for risky actions like refunds.

```
 you ──▶ agent/agent.py ──▶ Gemini (picks tools)
              │  ▲
   API key    ▼  │ asks you to approve refunds
        MCP server (Streamable HTTP or stdio)
         ├─ roles, refund rules, tool-call log
         ├─ PostgreSQL + pgvector (orders, tickets, policy embeddings)
         └─ Ollama (local embeddings)
```

| What | Result |
|---|---|
| Retrieval (14 labeled questions) | Hit@1 93%, Hit@3 100%, MRR 0.95 |
| Agent evals (7 cases, `gemini-3.5-flash-lite`, 2026-11-25) | 7/7 passed |
| Cost per eval conversation (paid-tier list price) | about $0.0013 |

## Quick start with Docker

Needs Docker, and [Ollama](https://ollama.com) running on the host with `ollama pull all-minilm`.

```bash
cp .env.example .env
```

Fill in `MCP_VIEWER_KEY`, `MCP_AGENT_KEY` and `GEMINI_API_KEY` in `.env`, then:

```bash
docker compose --profile full up -d --build
docker compose --profile full run --rm server python acme/db/seed.py
docker compose --profile full run --rm server python scripts/ingest_kb.py
```

The server is now on `http://127.0.0.1:8000/mcp`. Run the agent from a local virtual environment (`pip install -r requirements.txt`):

```bash
python agent/agent.py
```

The sections below explain how it was built, phase by phase.

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
| Hit@1 | 93% |
| Hit@3 | 100% |
| MRR | 0.95 |

Measured on 14 labeled questions with `all-minilm`. The one miss at rank 1 ("Can I return headphones six weeks after they were delivered?") ranks the refund policy third, behind the shipping policy.

---

## Phase 4: Remote transport, auth, and safe writes

| Piece | Details |
|---|---|
| Transport | Streamable HTTP, stateless (`MCP_TRANSPORT=http`). stdio stays the default |
| Authentication | Bearer API keys; a missing or wrong key gets 401 |
| Authorization | `viewer` keys can only read; `agent` keys can also write. Local stdio is read-only unless `MCP_ROLE=agent` |
| Refunds | `issue_refund(order_id, email, reason)`: delivered orders only, 30/60-day window by tier, no double refunds, $500 limit |
| Human approval | The server asks the person through MCP elicitation. The model cannot supply the answer |
| Tool-call log | Every call (reads, failures, denials) is recorded in `tool_calls` with caller, outcome, and duration |

**Design decisions**
- **Defense in depth for refunds:** role check, policy checks, human approval, a second check with the order row locked, and a unique index that stops a double refund in the database itself.
- **Two logs, two purposes:** `audit_log` is written in the same transaction as each change. `tool_calls` is best-effort observability and never breaks a call.
- **Least privilege:** the default role is read-only.

**Known limits (not production-ready yet)**
- Static shared keys, not OAuth: no expiry or per-person identity. Rotating a key needs a restart.
- Needs HTTPS in front of it; over plain HTTP the key is readable in transit.
- The approval state is signed with a per-process key, so running several replicas needs a shared `request_state_security` key.
- `tool_calls` stores arguments, including customer emails and ticket text, with no retention or redaction policy.
- Clients without elicitation support cannot issue refunds.

### Run it

```bash
docker exec -i acme-db psql -U acme -d acme < acme/db/migrations/004_phase4.sql
```

Add `MCP_VIEWER_KEY` and `MCP_AGENT_KEY` to `.env` (see `.env.example`), then start the server:

```bash
MCP_TRANSPORT=http python server/acme_server.py
```

In a second terminal:

```bash
python scripts/try_tool.py viewer get_customer '{"email": "ali.raza@example.com"}'
python scripts/try_tool.py agent issue_refund '{"order_id": 1, "email": "sarah.khan@example.com", "reason": "Changed my mind"}'
```

To allow writes from Claude Desktop (stdio), add `"env": {"MCP_ROLE": "agent"}` to its server config.

---

## Phase 5: Custom agent client

`agent/agent.py` is a terminal support agent: Gemini picks the tool, the MCP server runs it.

- **Hand-written tool loop:** no agent framework, so every step is visible.
- **Tools discovered at run time** from the server, so a new server tool needs no agent change.
- **Human approval stays outside the model:** the server's confirmation request goes to the person at the terminal.
- **Bounded:** at most 8 model steps per question. Tool errors are returned to the model so it can recover.
- **Data note:** tool results (customer data) are sent to Google. The data here is synthetic; a real client would need an agreed data-processing setup.

Add `GEMINI_API_KEY` to `.env`, start the server over HTTP, then in a second terminal:

```bash
python agent/agent.py
```

Use `python agent/agent.py viewer` for a read-only session, and set `GEMINI_MODEL` to change the model.

---

## Phase 6: Evals, cost, and deployment

```bash
python scripts/eval_agent.py
```

Each case in `evals/agent_set.json` runs through the real model and server, and checks which tools were called, which must **not** be called, and the key fact in the answer. Approvals are always refused, so no refund is issued. Seed the database first: refund windows depend on dates.

**What testing the agent found:** after a person refused a refund, the model opened a ticket nobody asked for, and in another run asked for the same refund again. Both came from vague refusal messages. The server now returns one explicit "this is final" message for "no", decline and cancel, and the eval fails on either behaviour. This guides the model but does not force it; only refunds are blocked in code.

**Cost:** the agent prints tokens and estimated cost per turn and per conversation. Prices are hard-coded in `agent/agent.py` with the date they were checked.

**Deployment:** `docker compose --profile full up` runs the database and the MCP server, with both ports bound to localhost. A real deployment still needs HTTPS, managed secrets, and a non-default database password.

**Eval limits:** 7 cases and one run each is a smoke test, not a benchmark. Answers are checked with regular expressions, and results can vary between runs.

---

## Roadmap

- [x] **Phase 0:** Acme backend (database, seed data, knowledge base)
- [x] **Phase 1:** MCP fundamentals: tools, resources, prompts
- [x] **Phase 2:** First write action, audit log, connection pooling, migrations, tests
- [x] **Phase 3:** RAG over the knowledge base with pgvector
- [x] **Phase 4:** Remote transport, auth, permissions, and confirmation for risky actions
- [x] **Phase 5:** Custom agent client
- [x] **Phase 6:** Evals, cost tracking, Docker deployment

---

## Author

**Fahad Munir**, AI/ML Engineer | Forward Deployed Engineer
[LinkedIn](https://www.linkedin.com/in/devfahadmunir) · [GitHub](https://github.com/devfahadmunir)
