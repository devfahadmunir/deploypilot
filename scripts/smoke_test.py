"""
Smoke test: connect to the Acme MCP server as a real MCP client and call
every tool, resource, and prompt.

Usage:
    python scripts/smoke_test.py
"""

import asyncio
import json
import sys
from pathlib import Path

from mcp.client import Client
from mcp.client.stdio import StdioServerParameters

SERVER = Path(__file__).resolve().parents[1] / "server" / "acme_server.py"


def show(title, result):
    print(f"\n=== {title} ===")
    for block in getattr(result, "content", None) or getattr(result, "contents", None) or getattr(result, "messages", []):
        text = getattr(block, "text", None) or getattr(getattr(block, "content", None), "text", None)
        if text:
            try:
                print(json.dumps(json.loads(text), indent=2)[:800])
            except (json.JSONDecodeError, TypeError):
                print(text[:800])


async def main():
    params = StdioServerParameters(command=sys.executable, args=[str(SERVER)])
    async with Client(params) as client:
        tools = await client.list_tools()
        print("Tools:     ", [t.name for t in tools.tools])
        templates = await client.list_resource_templates()
        resources = await client.list_resources()
        print("Resources: ", [str(r.uri) for r in resources.resources] + [t.uri_template for t in templates.resource_templates])
        prompts = await client.list_prompts()
        print("Prompts:   ", [p.name for p in prompts.prompts])

        show("get_customer(ali.raza@example.com)", await client.call_tool("get_customer", {"email": "ali.raza@example.com"}))
        show("list_orders(sarah.khan@example.com)", await client.call_tool("list_orders", {"email": "sarah.khan@example.com", "limit": 5}))
        show("get_order(3)", await client.call_tool("get_order", {"order_id": 3}))
        show("list_tickets(ali.raza@example.com)", await client.call_tool("list_tickets", {"email": "ali.raza@example.com"}))
        show("get_customer(unknown) -> expected error", await client.call_tool("get_customer", {"email": "nobody@example.com"}))
        show("resource acme://policies", await client.read_resource("acme://policies"))
        show("resource acme://policies/refund_policy", await client.read_resource("acme://policies/refund_policy"))
        show("prompt check_refund_eligibility(3)", await client.get_prompt("check_refund_eligibility", {"order_id": "3"}))
    print("\nAll checks ran.")


if __name__ == "__main__":
    asyncio.run(main())
