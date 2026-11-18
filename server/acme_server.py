"""
DeployPilot: Acme Store MCP server (Phase 1, read-only).

Exposes Acme's systems to any MCP client:
  - Tools:     look up customers, orders, and tickets (the model decides when to call them)
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
from datetime import datetime
from decimal import Decimal
from pathlib import Path
from typing import Any

import psycopg
from dotenv import load_dotenv
from mcp.server import MCPServer
from mcp.server.mcpserver.exceptions import ResourceNotFoundError, ToolError
from mcp.types import ToolAnnotations
from psycopg.rows import dict_row

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------
PROJECT_ROOT = Path(__file__).resolve().parents[1]
KNOWLEDGE_BASE = PROJECT_ROOT / "acme" / "knowledge_base"

# Load .env from the project root, so this works no matter which folder
# the MCP host launches the server from.
load_dotenv(PROJECT_ROOT / ".env")
DATABASE_URL = os.getenv("DATABASE_URL", "postgresql://acme:acme@localhost:5432/acme")

logging.basicConfig(stream=sys.stderr, level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("acme-mcp")

EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
READ_ONLY = ToolAnnotations(read_only_hint=True, destructive_hint=False, idempotent_hint=True, open_world_hint=False)

mcp = MCPServer(
    name="acme-store",
    title="Acme Store Systems",
    instructions=(
        "Tools and documents for Acme Store customer support. "
        "Always identify customers by email before discussing their orders. "
        "Check the refund policy and the customer's tier before answering refund questions."
    ),
)


# ---------------------------------------------------------------------------
# Database helpers
# ---------------------------------------------------------------------------
def _clean(value: Any) -> Any:
    """Convert DB types (Decimal, datetime) into JSON-friendly values."""
    if isinstance(value, Decimal):
        return float(value)
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, dict):
        return {k: _clean(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_clean(v) for v in value]
    return value


def query(sql: str, params: tuple | dict = ()) -> list[dict]:
    """Run a read-only query and return rows as dicts."""
    try:
        with psycopg.connect(DATABASE_URL, row_factory=dict_row, connect_timeout=5) as conn:
            conn.read_only = True  # the database itself refuses writes from this connection
            with conn.cursor() as cur:
                cur.execute(sql, params)
                return _clean(cur.fetchall())
    except psycopg.OperationalError as exc:
        # Log the real cause on the server; give the model a safe, actionable message.
        log.error("Database unavailable: %s", exc)
        raise ToolError("The Acme database is unavailable right now. Please try again shortly.") from exc


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
        "1. Read the refund policy resource (acme://policies/refund_policy).\n"
        f"2. Use get_order to get order {order_id}'s status, customer tier, "
        "days since delivery, and existing refunds.\n"
        "3. Apply the policy: the refund window depends on the customer's tier, "
        "only delivered orders can be refunded, and already-refunded orders cannot be refunded again.\n"
        "Answer ELIGIBLE or NOT ELIGIBLE, then explain which rule decided it. "
        "Note if the amount requires supervisor approval."
    )


if __name__ == "__main__":
    mcp.run()  # stdio transport by default
