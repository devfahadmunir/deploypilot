"""Phase 4: roles, API keys, confirmed refunds, and the tool-call log."""

import asyncio
import os
import threading
import time

import httpx2
import pytest
import uvicorn
from mcp.client import Client
from mcp.client.streamable_http import streamable_http_client
from mcp.types import ElicitResult

from server.acme_server import mcp
from server.db import query, write_transaction

SARAH = "sarah.khan@example.com"


def refund(order_id: int, email: str = SARAH, answer: str = "accept", approve: bool = True):
    """Call issue_refund while playing the human. Returns (result, questions asked)."""
    asked: list[str] = []

    async def on_question(context, params):
        asked.append(params.message)
        return ElicitResult(action=answer, content={"approve": approve} if answer == "accept" else None)

    async def _run():
        async with Client(mcp, elicitation_callback=on_question) as client:
            return await client.call_tool(
                "issue_refund", {"order_id": order_id, "email": email, "reason": "Changed my mind"}
            )

    return asyncio.run(_run()), asked


@pytest.fixture
def make_order():
    """Temporary orders for Sarah, so tests never use up the seeded demo data."""
    created: list[int] = []

    def _make(days_ago: int = 5, total: float = 40, status: str = "delivered") -> int:
        with write_transaction() as conn:
            order_id = conn.execute(
                """INSERT INTO orders (customer_id, status, total, created_at, delivered_at)
                   SELECT id, %s, %s, now() - make_interval(days => %s),
                          CASE WHEN %s = 'delivered' THEN now() - make_interval(days => %s) END
                   FROM customers WHERE email = %s RETURNING id""",
                (status, total, days_ago + 4, status, days_ago, SARAH),
            ).fetchone()["id"]
        created.append(order_id)
        return order_id

    yield _make
    with write_transaction() as conn:
        conn.execute("DELETE FROM audit_log WHERE tool = 'issue_refund' AND (result->>'order_id')::int = ANY(%s)", (created,))
        conn.execute("DELETE FROM refunds WHERE order_id = ANY(%s)", (created,))
        conn.execute("DELETE FROM orders WHERE id = ANY(%s)", (created,))


def refunds_for(order_id: int) -> int:
    return query("SELECT COUNT(*) AS n FROM refunds WHERE order_id = %s", (order_id,))[0]["n"]


def test_approved_refund_is_written_and_audited(make_order):
    order_id = make_order()
    result, asked = refund(order_id)
    assert not result.is_error, result.content[0].text
    assert "$40.00" in asked[0]
    assert refunds_for(order_id) == 1
    assert query("SELECT status FROM orders WHERE id = %s", (order_id,))[0]["status"] == "refunded"
    assert query("SELECT 1 AS x FROM audit_log WHERE tool = 'issue_refund' AND (result->>'order_id')::int = %s", (order_id,))

    again, asked_again = refund(order_id)
    assert again.is_error and "already been refunded" in again.content[0].text
    assert not asked_again and refunds_for(order_id) == 1


@pytest.mark.parametrize("answer, approve", [("accept", False), ("decline", True), ("cancel", True)])
def test_refund_without_approval_changes_nothing(make_order, answer, approve):
    order_id = make_order()
    result, asked = refund(order_id, answer=answer, approve=approve)
    assert asked and result.is_error
    assert refunds_for(order_id) == 0


@pytest.mark.parametrize("order, email, message", [
    ({"days_ago": 45}, SARAH, "outside the 30-day window"),
    ({"total": 600}, SARAH, "supervisor"),
    ({"status": "shipped"}, SARAH, "Only delivered"),
    ({}, "ali.raza@example.com", "does not belong"),
])
def test_policy_refuses_before_asking_the_human(make_order, order, email, message):
    order_id = make_order(**order)
    result, asked = refund(order_id, email=email)
    assert result.is_error and message in result.content[0].text
    assert not asked and refunds_for(order_id) == 0


def test_viewer_cannot_write_but_can_read(make_order, monkeypatch):
    order_id = make_order()
    monkeypatch.setenv("MCP_ROLE", "viewer")
    result, asked = refund(order_id)
    assert result.is_error and "Permission denied" in result.content[0].text
    assert not asked

    async def _run():
        async with Client(mcp) as client:
            ticket = await client.call_tool(
                "create_ticket", {"email": SARAH, "subject": "Question", "description": "A viewer tries to write."}
            )
            read = await client.call_tool("get_customer", {"email": SARAH})
            return ticket, read

    ticket, read = asyncio.run(_run())
    assert ticket.is_error and "Permission denied" in ticket.content[0].text
    assert not read.is_error


def test_every_tool_call_is_logged_including_failures():
    before = query("SELECT COALESCE(MAX(id), 0) AS id FROM tool_calls")[0]["id"]

    async def _run():
        async with Client(mcp) as client:
            await client.call_tool("get_customer", {"email": SARAH})
            await client.call_tool("get_customer", {"email": "nobody@example.com"})

    asyncio.run(_run())
    rows = query("SELECT tool, client_id, outcome, error FROM tool_calls WHERE id > %s ORDER BY id", (before,))
    assert [r["outcome"] for r in rows] == ["ok", "error"]
    assert rows[0]["client_id"] == "local:agent"
    assert "No customer found" in rows[1]["error"]


def test_http_requires_a_key_and_enforces_the_role(monkeypatch):
    """A real server on a spare port: keys, roles and logging only meet over HTTP."""
    monkeypatch.setenv("MCP_AGENT_KEY", "test-agent-key")
    monkeypatch.setenv("MCP_VIEWER_KEY", "test-viewer-key")
    monkeypatch.setenv("MCP_ROLE", "viewer")  # must be ignored when a key is present
    server = uvicorn.Server(uvicorn.Config(mcp.streamable_http_app(stateless_http=True), port=0, log_level="critical"))
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    while not server.started:
        time.sleep(0.05)
    url = f"http://127.0.0.1:{server.servers[0].sockets[0].getsockname()[1]}/mcp"
    args = {"email": SARAH, "order_id": 999999, "subject": "Question", "description": "Which role is calling?"}

    async def _run():
        async with httpx2.AsyncClient() as http:
            for headers in ({}, {"Authorization": "Bearer wrong"}):
                assert (await http.post(url, json={}, headers=headers)).status_code == 401
        texts = []
        for key in ("test-viewer-key", "test-agent-key"):
            async with httpx2.AsyncClient(headers={"Authorization": f"Bearer {key}"}) as http:
                async with Client(streamable_http_client(url, http_client=http)) as client:
                    texts.append((await client.call_tool("create_ticket", args)).content[0].text)
        return texts

    try:
        viewer, agent = asyncio.run(_run())
    finally:
        server.should_exit = True
        thread.join(timeout=5)
    assert "Permission denied" in viewer
    assert "does not exist" in agent  # got past the role check; nothing is written
    last = query("SELECT client_id FROM tool_calls ORDER BY id DESC LIMIT 2")
    assert [r["client_id"] for r in last] == ["agent", "viewer"]
