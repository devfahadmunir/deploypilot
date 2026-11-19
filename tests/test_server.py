"""
Automated tests for the Acme MCP server.

Tests talk to the server through a real MCP client connected in-memory
(no subprocess), against the seeded Acme database.

Run:  pytest -v
Requires: database running and seeded (python acme/db/seed.py)
"""

import asyncio
import json

import psycopg
import pytest
from mcp.client import Client

from server.acme_server import mcp
from server.db import query, write_transaction


def call(tool: str, args: dict):
    """Call an MCP tool through a real client session and return the result."""
    async def _run():
        async with Client(mcp) as client:
            return await client.call_tool(tool, args)
    return asyncio.run(_run())


def payload(result) -> dict | list:
    return json.loads(result.content[0].text)


@pytest.fixture
def cleanup_tickets():
    """Delete any tickets (and audit rows) a test creates."""
    created: list[int] = []
    yield created
    if created:
        with write_transaction() as conn:
            conn.execute("DELETE FROM audit_log WHERE tool = 'create_ticket' "
                         "AND (result->>'ticket_id')::int = ANY(%s)", (created,))
            conn.execute("DELETE FROM tickets WHERE id = ANY(%s)", (created,))


# --- Discovery ---------------------------------------------------------------

def test_all_tools_are_exposed():
    async def _run():
        async with Client(mcp) as client:
            return await client.list_tools()
    tools = {t.name: t for t in asyncio.run(_run()).tools}
    assert set(tools) == {"get_customer", "list_orders", "get_order", "list_tickets", "create_ticket", "search_knowledge_base"}
    assert tools["get_customer"].annotations.read_only_hint is True
    assert tools["create_ticket"].annotations.read_only_hint is False


# --- Read tools: business facts the AI depends on ----------------------------

def test_premium_customer_with_open_tickets():
    ali = payload(call("get_customer", {"email": "ali.raza@example.com"}))
    assert ali["tier"] == "premium"
    assert ali["open_tickets"] == 3


def test_email_is_case_insensitive():
    sarah = payload(call("get_customer", {"email": "  Sarah.Khan@Example.com "}))
    assert sarah["name"] == "Sarah Khan"


def test_order_reports_days_since_delivery():
    order = payload(call("get_order", {"order_id": 3}))
    assert order["customer_tier"] == "premium"
    # Seed dates are relative to when seed.py ran, so the exact number drifts day by day.
    # What matters for the refund rule: outside the 30-day standard window,
    # inside the 60-day premium window.
    assert 30 < order["days_since_delivery"] < 60


def test_unknown_customer_returns_helpful_error():
    result = call("get_customer", {"email": "nobody@example.com"})
    assert result.is_error
    assert "No customer found" in result.content[0].text


def test_invalid_email_is_rejected():
    result = call("get_customer", {"email": "not-an-email"})
    assert result.is_error
    assert "not a valid email" in result.content[0].text


# --- Safety: read path really is read-only -----------------------------------

def test_read_queries_cannot_write():
    with pytest.raises(psycopg.errors.ReadOnlySqlTransaction):
        query("UPDATE customers SET tier = 'premium' WHERE id = 1")


# --- Write tool: create_ticket ------------------------------------------------

def test_create_ticket_and_audit_log(cleanup_tickets):
    result = call("create_ticket", {
        "email": "sarah.khan@example.com",
        "subject": "Item arrived damaged",
        "description": "The wireless earbuds case is cracked on arrival.",
        "priority": "high",
        "order_id": 1,
    })
    assert not result.is_error, result.content[0].text
    ticket = payload(result)
    cleanup_tickets.append(ticket["ticket_id"])
    assert ticket["status"] == "open" and ticket["priority"] == "high"

    audit_rows = query("SELECT arguments FROM audit_log WHERE tool = 'create_ticket' "
                       "AND (result->>'ticket_id')::int = %s", (ticket["ticket_id"],))
    assert len(audit_rows) == 1
    assert audit_rows[0]["arguments"]["email"] == "sarah.khan@example.com"


def test_cannot_attach_ticket_to_another_customers_order():
    before = query("SELECT COUNT(*) AS n FROM tickets")[0]["n"]
    result = call("create_ticket", {
        "email": "sarah.khan@example.com",
        "subject": "Refund please",
        "description": "Trying to use someone else's order number.",
        "order_id": 3,  # belongs to Ali Raza
    })
    assert result.is_error
    assert "does not belong" in result.content[0].text
    assert query("SELECT COUNT(*) AS n FROM tickets")[0]["n"] == before  # nothing written


def test_invalid_priority_is_rejected_by_schema():
    result = call("create_ticket", {
        "email": "sarah.khan@example.com",
        "subject": "Question",
        "description": "Just checking how priorities are validated.",
        "priority": "urgent",
    })
    assert result.is_error


def test_too_short_subject_is_rejected_by_schema():
    result = call("create_ticket", {
        "email": "sarah.khan@example.com",
        "subject": "Hi",
        "description": "Subject is shorter than the minimum length.",
    })
    assert result.is_error
