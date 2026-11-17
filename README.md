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

## Roadmap

- [x] **Phase 0:** Acme backend (database, seed data, knowledge base)
- [ ] **Phase 1:** MCP fundamentals: tools, resources, prompts
- [ ] **Phase 2:** Database integration with validation and error handling
- [ ] **Phase 3:** RAG over the knowledge base with pgvector
- [ ] **Phase 4:** Remote transport, auth, permissions, and confirmation for risky actions
- [ ] **Phase 5:** Custom agent client
- [ ] **Phase 6:** Evals, cost tracking, Docker deployment

---

## Author

**Fahad Munir**, AI/ML Engineer | Forward Deployed Engineer
[LinkedIn](https://www.linkedin.com/in/devfahadmunir) · [GitHub](https://github.com/devfahadmunir)
