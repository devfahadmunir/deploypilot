"""
DeployPilot: Acme Store MCP server (Phase 4: remote transport, auth, safe writes).

Exposes Acme's systems to any MCP client:
  - Tools:     look up customers, orders, and tickets; open support tickets;
               semantic search over company policies (the model decides when to call them)
  - Resources: company policies from the knowledge base (the app/user attaches them)
  - Prompts:   reusable workflows for support agents (the user picks them)

Run with the MCP Inspector:   mcp dev server/acme_server.py
Run over stdio:               python server/acme_server.py

IMPORTANT: over stdio, stdout carries the MCP protocol itself.
Never print() to stdout here. Use the logger, which writes to stderr.

Error handling (MCP SDK v2):
  - Raise ToolError / ResourceNotFoundError for EXPECTED problems (bad input,
    not found). The message is sent to the model so it can recover.
  - Any other exception is treated as a crash: the model only sees a generic
    message, and the details stay in the server log. Internals never leak.
"""

import logging
import os
import re
import sys
import time
from pathlib import Path
from typing import Annotated, Literal

# Make the project root importable, so `from server.db import ...` works whether
# this file is run directly, by `mcp dev`, by Claude Desktop, or by pytest.
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import anyio  # noqa: E402
from mcp.server import MCPServer  # noqa: E402
from mcp.server.elicitation import AcceptedElicitation, ElicitationResult  # noqa: E402
from mcp.server.mcpserver.exceptions import ResourceNotFoundError, ToolError  # noqa: E402
from mcp.server.mcpserver.resolve import Elicit, Resolve  # noqa: E402
from mcp.types import ToolAnnotations  # noqa: E402
import psycopg  # noqa: E402
from pydantic import BaseModel, Field  # noqa: E402

from server.auth import AUTH_SETTINGS, ApiKeyVerifier, caller, require_write  # noqa: E402
from server.db import PROJECT_ROOT, audit, clean, log_tool_call, query, write_transaction  # noqa: E402
from server.rag.embeddings import MODEL_NAME, EmbeddingError, embed_query, to_pgvector  # noqa: E402

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------
KNOWLEDGE_BASE = PROJECT_ROOT / "acme" / "knowledge_base"

logging.basicConfig(stream=sys.stderr, level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
log = logging.getLogger("acme-mcp")

EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")

# Hints that tell MCP clients how risky each tool is.
READ_ONLY = ToolAnnotations(read_only_hint=True, destructive_hint=False, idempotent_hint=True, open_world_hint=False)
SAFE_WRITE = ToolAnnotations(read_only_hint=False, destructive_hint=False, idempotent_hint=False, open_world_hint=False)
DESTRUCTIVE = ToolAnnotations(read_only_hint=False, destructive_hint=True, idempotent_hint=False, open_world_hint=False)

# Business rules from the knowledge base (refund_policy, support_guidelines).
REFUND_WINDOW_DAYS = {"standard": 30, "premium": 60}
SUPERVISOR_LIMIT = 500


async def log_tool_calls(ctx, call_next):
    """Server middleware: record every tool call, including denied and invalid ones."""
    if ctx.method != "tools/call":
        return await call_next(ctx)
    started = time.perf_counter()
    outcome, error = "error", "request failed before the tool returned"
    try:
        result = await call_next(ctx)
        data = result if isinstance(result, dict) else result.model_dump(by_alias=True, mode="json")
        if data.get("resultType") == "input_required":
            outcome, error = "input_required", None
        elif data.get("isError"):
            error = " ".join(block.get("text", "") for block in data.get("content", []))
        else:
            outcome, error = "ok", None
        return result
    finally:
        params = ctx.params or {}
        await anyio.to_thread.run_sync(
            log_tool_call, str(params.get("name")), params.get("arguments") or {}, caller()[0],
            outcome, error, int((time.perf_counter() - started) * 1000),
        )


mcp = MCPServer(
    name="acme-store",
    title="Acme Store Systems",
    token_verifier=ApiKeyVerifier(),  # only enforced over HTTP, not stdio
    auth=AUTH_SETTINGS,
    middleware=[log_tool_calls],
    instructions=(
        "Tools and documents for Acme Store customer support. "
        "Always identify customers by email before discussing their orders. "
        "For any policy question, use search_knowledge_base and cite the source it returns. "
        "Check the refund policy and the customer's tier before answering refund questions."
    ),
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
def _find_customer(email: str) -> dict:
    email = email.strip().lower()
    if not EMAIL_RE.match(email):
        raise ToolError(f"'{email}' is not a valid email address.")
    rows = query("SELECT id, name, email, city, country, tier, created_at FROM customers WHERE email = %s", (email,))
    if not rows:
        raise ToolError(f"No customer found with email {email}.")
    return rows[0]


# ---------------------------------------------------------------------------
# Tools (model-controlled)
# ---------------------------------------------------------------------------
@mcp.tool(annotations=READ_ONLY)
def get_customer(email: str) -> dict:
    """Look up a customer by email. Returns their profile, membership tier
    (standard or premium), order count, total spent, and open ticket count."""
    customer = _find_customer(email)
    stats = query(
        """SELECT
               (SELECT COUNT(*) FROM orders WHERE customer_id = %(id)s) AS order_count,
               (SELECT COALESCE(SUM(total), 0) FROM orders
                 WHERE customer_id = %(id)s AND status NOT IN ('cancelled', 'refunded')) AS total_spent,
               (SELECT COUNT(*) FROM tickets
                 WHERE customer_id = %(id)s AND status <> 'resolved') AS open_tickets""",
        {"id": customer["id"]},
    )[0]
    log.info("get_customer email=%s", customer["email"])
    return {**customer, **stats}


@mcp.tool(annotations=READ_ONLY)
def list_orders(email: str, limit: int = 10) -> list[dict]:
    """List a customer's most recent orders (newest first) with status,
    total, and order/ship/delivery dates. Use get_order for item details."""
    customer = _find_customer(email)
    limit = max(1, min(limit, 50))
    log.info("list_orders email=%s limit=%d", customer["email"], limit)
    return query(
        """SELECT id AS order_id, status, total, created_at, shipped_at, delivered_at
           FROM orders WHERE customer_id = %s ORDER BY created_at DESC LIMIT %s""",
        (customer["id"], limit),
    )


@mcp.tool(annotations=READ_ONLY)
def get_order(order_id: int) -> dict:
    """Get full details of one order: customer, status, dates, line items,
    days since delivery (useful for refund-window checks), and any refunds."""
    rows = query(
        """SELECT o.id AS order_id, o.status, o.total, o.created_at, o.shipped_at, o.delivered_at,
                  c.name AS customer_name, c.email AS customer_email, c.tier AS customer_tier,
                  CASE WHEN o.delivered_at IS NOT NULL
                       THEN FLOOR(EXTRACT(EPOCH FROM now() - o.delivered_at) / 86400)::int END
                       AS days_since_delivery
           FROM orders o JOIN customers c ON c.id = o.customer_id
           WHERE o.id = %s""",
        (order_id,),
    )
    if not rows:
        raise ToolError(f"Order {order_id} does not exist.")
    order = rows[0]
    order["items"] = query(
        """SELECT p.sku, p.name, p.category, oi.quantity, oi.unit_price
           FROM order_items oi JOIN products p ON p.id = oi.product_id
           WHERE oi.order_id = %s ORDER BY oi.id""",
        (order_id,),
    )
    order["refunds"] = query(
        "SELECT id AS refund_id, amount, reason, status, created_at FROM refunds WHERE order_id = %s",
        (order_id,),
    )
    log.info("get_order order_id=%d", order_id)
    return order


@mcp.tool(annotations=READ_ONLY)
def list_tickets(email: str, include_resolved: bool = False) -> list[dict]:
    """List a customer's support tickets, newest first. By default only
    open and in-progress tickets are returned."""
    customer = _find_customer(email)
    status_filter = "" if include_resolved else "AND status <> 'resolved'"
    log.info("list_tickets email=%s include_resolved=%s", customer["email"], include_resolved)
    return query(
        f"""SELECT id AS ticket_id, order_id, subject, status, priority, created_at, updated_at
            FROM tickets WHERE customer_id = %s {status_filter} ORDER BY created_at DESC""",
        (customer["id"],),
    )


# ---------------------------------------------------------------------------
# Knowledge base search (RAG)
# ---------------------------------------------------------------------------
@mcp.tool(annotations=READ_ONLY)
def search_knowledge_base(
    question: Annotated[str, Field(min_length=3, max_length=500, description="The question, in natural language")],
    top_k: Annotated[int, Field(ge=1, le=8, description="How many passages to return")] = 4,
) -> list[dict]:
    """Semantic search over Acme Store's internal policies (refunds, shipping,
    returns, warranty, membership, support guidelines, FAQ). Use it for any
    question about rules or procedures. Returns the most relevant passages,
    best first, with a similarity score (0 to 1) and a source URI. Base your
    answer on these passages and cite the source."""
    try:
        vector = to_pgvector(embed_query(question))
    except EmbeddingError as exc:
        log.error("Embedding failed: %s", exc)  # full detail stays in the server log
        raise ToolError("Knowledge base search is temporarily unavailable. Please try again shortly.") from exc

    try:
        rows = query(
            """SELECT doc, section, content,
                      ROUND((1 - (embedding <=> %(v)s::vector))::numeric, 3) AS score
               FROM kb_chunks
               WHERE model = %(m)s
               ORDER BY embedding <=> %(v)s::vector
               LIMIT %(k)s""",
            {"v": vector, "m": MODEL_NAME, "k": top_k},
        )
    except psycopg.errors.UndefinedTable as exc:
        raise ToolError("The knowledge base is not set up yet. Apply migration 003 and run scripts/ingest_kb.py.") from exc
    if not rows:
        # Vectors from different models are not comparable, so never mix them.
        raise ToolError(f"The knowledge base has no entries for {MODEL_NAME}. Run scripts/ingest_kb.py to index it.")
    for row in rows:
        row["source"] = f"acme://policies/{row['doc']}"
    log.info("search_knowledge_base top_k=%d best=%s (%.3f)", top_k, rows[0]["doc"], rows[0]["score"])
    return rows


# ---------------------------------------------------------------------------
# Write tools
# ---------------------------------------------------------------------------
@mcp.tool(annotations=SAFE_WRITE)
def create_ticket(
    email: str,
    subject: Annotated[str, Field(min_length=3, max_length=120, description="Short summary of the issue")],
    description: Annotated[str, Field(min_length=10, max_length=2000, description="Details of the issue")],
    priority: Literal["low", "medium", "high"] = "medium",
    order_id: int | None = None,
) -> dict:
    """Open a new support ticket for a customer. Link it to an order with
    order_id when the issue is about a specific order (the order must belong
    to this customer). Priority per support guidelines: high for damaged or
    wrong items, double charges, and lost packages; medium for refund requests
    and order problems; low for general and warranty questions."""
    require_write()
    customer = _find_customer(email)
    subject, description = subject.strip(), description.strip()

    with write_transaction() as conn:
        if order_id is not None:
            order = conn.execute("SELECT customer_id FROM orders WHERE id = %s", (order_id,)).fetchone()
            if order is None:
                raise ToolError(f"Order {order_id} does not exist.")
            if order["customer_id"] != customer["id"]:
                # Never let one customer's ticket touch another customer's order.
                raise ToolError(f"Order {order_id} does not belong to {customer['email']}.")

        ticket = conn.execute(
            """INSERT INTO tickets (customer_id, order_id, subject, description, priority, status)
               VALUES (%s, %s, %s, %s, %s, 'open')
               RETURNING id AS ticket_id, order_id, subject, priority, status, created_at""",
            (customer["id"], order_id, subject, description, priority),
        ).fetchone()

        audit(conn, "create_ticket",
              {"email": customer["email"], "subject": subject, "priority": priority, "order_id": order_id},
              ticket)

    log.info("create_ticket ticket_id=%s email=%s", ticket["ticket_id"], customer["email"])
    return clean({**ticket, "customer_email": customer["email"]})


def _refundable_order(run, order_id: int, email: str, lock: str = "") -> dict:
    """Return the order if policy allows refunding it, else raise ToolError.
    `run(sql, params)` returns rows, so the same rules serve the read-only preview
    and the locked check inside the write transaction."""
    customer = _find_customer(email)
    rows = run(
        f"""SELECT id AS order_id, customer_id, status, total,
                   FLOOR(EXTRACT(EPOCH FROM now() - delivered_at) / 86400)::int AS days
            FROM orders WHERE id = %s {lock}""",
        (order_id,),
    )
    if not rows:
        raise ToolError(f"Order {order_id} does not exist.")
    order = rows[0]
    if order["customer_id"] != customer["id"]:
        raise ToolError(f"Order {order_id} does not belong to {customer['email']}.")
    if run("SELECT 1 FROM refunds WHERE order_id = %s AND status <> 'rejected'", (order_id,)):
        raise ToolError(f"Order {order_id} has already been refunded.")
    if order["status"] != "delivered" or order["days"] is None:
        raise ToolError(f"Order {order_id} is '{order['status']}'. Only delivered orders can be refunded.")
    window = REFUND_WINDOW_DAYS[customer["tier"]]
    if order["days"] > window:
        raise ToolError(
            f"Order {order_id} was delivered {order['days']} days ago, outside the {window}-day window for "
            f"{customer['tier']} customers. If the item was damaged or wrong (90-day exception), open a ticket "
            "so a person can verify it."
        )
    if order["total"] > SUPERVISOR_LIMIT:
        raise ToolError(
            f"Refunds above ${SUPERVISOR_LIMIT} need supervisor approval. Open a ticket to escalate order {order_id}."
        )
    return {**order, "customer_email": customer["email"]}


class RefundApproval(BaseModel):
    approve: bool = Field(description="Approve this refund?")


def confirm_refund(order_id: int, email: str) -> Elicit[RefundApproval]:
    """Runs before issue_refund: refuse early, otherwise ask the human. The model
    cannot supply this answer, because it is not one of the tool's arguments."""
    require_write()
    order = _refundable_order(query, order_id, email)
    return Elicit(
        f"Refund ${order['total']:.2f} for order {order_id} to {order['customer_email']}?", RefundApproval
    )


@mcp.tool(annotations=DESTRUCTIVE)
def issue_refund(
    order_id: int,
    email: str,
    reason: Annotated[str, Field(min_length=5, max_length=500, description="Why the customer wants a refund")],
    # The full result, so "no", decline and cancel all get our message below.
    approval: Annotated[ElicitationResult[RefundApproval], Resolve(confirm_refund)],
) -> dict:
    """Refund the full total of a delivered order. Policy is enforced by the server
    (refund window by tier, no double refunds, $500 limit) and a human must approve
    before money moves. Partial refunds are not supported: open a ticket instead."""
    require_write()
    if not (isinstance(approval, AcceptedElicitation) and approval.data.approve):
        raise ToolError(
            "The refund was not approved: the person asked to confirm it said no. Nothing was changed. "
            "This is their final decision. Do not retry, open a ticket, or take any other action unless "
            "the user asks. Tell the user the refund was declined."
        )

    with write_transaction() as conn:
        # Check again with the row locked: things may have changed while the human decided.
        order = _refundable_order(
            lambda sql, params: conn.execute(sql, params).fetchall(), order_id, email, lock="FOR UPDATE"
        )
        refund = conn.execute(
            """INSERT INTO refunds (order_id, amount, reason, status) VALUES (%s, %s, %s, 'completed')
               RETURNING id AS refund_id, order_id, amount, status, created_at""",
            (order_id, order["total"], reason.strip()),
        ).fetchone()
        conn.execute("UPDATE orders SET status = 'refunded' WHERE id = %s", (order_id,))
        audit(conn, "issue_refund",
              {"email": order["customer_email"], "order_id": order_id, "reason": reason.strip()}, refund)

    log.info("issue_refund refund_id=%s order_id=%s", refund["refund_id"], order_id)
    return clean(refund)


# ---------------------------------------------------------------------------
# Resources (application-controlled)
# ---------------------------------------------------------------------------
def _policy_names() -> list[str]:
    return sorted(p.stem for p in KNOWLEDGE_BASE.glob("*.md"))


@mcp.resource("acme://policies", name="policy_index", mime_type="text/plain")
def policy_index() -> str:
    """List of all Acme Store policy documents."""
    names = _policy_names()
    return "Available policies:\n" + "\n".join(f"- acme://policies/{n}" for n in names)


@mcp.resource("acme://policies/{name}", name="policy", mime_type="text/markdown")
def policy(name: str) -> str:
    """One Acme Store policy document, e.g. refund_policy or shipping_policy."""
    if name not in _policy_names():  # allow-list: blocks path tricks like ../../etc/passwd
        raise ResourceNotFoundError(f"Unknown policy '{name}'. Available: {', '.join(_policy_names())}")
    return (KNOWLEDGE_BASE / f"{name}.md").read_text(encoding="utf-8")


# ---------------------------------------------------------------------------
# Prompts (user-controlled)
# ---------------------------------------------------------------------------
@mcp.prompt(title="Summarize a customer")
def summarize_customer(email: str) -> str:
    """Summarize a customer's account, recent orders, and open issues."""
    return (
        f"Summarize the Acme Store account for {email}.\n"
        "1. Use get_customer for their profile and tier.\n"
        "2. Use list_orders for recent orders.\n"
        "3. Use list_tickets for open issues.\n"
        "Then write a short summary for a support agent: who they are, tier, "
        "recent activity, open issues, and anything that needs attention. "
        "Per the support guidelines, flag the account for escalation if they have 3 or more open tickets."
    )


@mcp.prompt(title="Check refund eligibility")
def check_refund_eligibility(order_id: int) -> str:
    """Decide whether an order is eligible for a refund under Acme's policy."""
    return (
        f"Check whether order {order_id} is eligible for a refund.\n"
        "1. Use search_knowledge_base to find the refund rules that apply.\n"
        f"2. Use get_order to get order {order_id}'s status, customer tier, "
        "days since delivery, and existing refunds.\n"
        "3. Apply the policy: the refund window depends on the customer's tier, "
        "only delivered orders can be refunded, and already-refunded orders cannot be refunded again. "
        "Damaged or wrong items have a 90-day exception.\n"
        "Answer ELIGIBLE or NOT ELIGIBLE, then explain which rule decided it. "
        "Note if the amount requires supervisor approval."
    )


if __name__ == "__main__":
    if os.getenv("MCP_TRANSPORT") == "http":
        # Stateless: no session kept between requests, so any replica can serve any call.
        mcp.run("streamable-http", host=os.getenv("MCP_HOST", "127.0.0.1"), stateless_http=True)  # port 8000, /mcp
    else:
        mcp.run()  # stdio, used by Claude Desktop and the smoke test