"""
Call one tool on the running HTTP server, as a viewer or an agent.
If the server asks for approval, you answer in the terminal.

    python scripts/try_tool.py agent get_customer '{"email": "ali.raza@example.com"}'
"""

import asyncio
import json
import os
import sys
from pathlib import Path

import httpx2
from dotenv import load_dotenv
from mcp.client import Client
from mcp.client.streamable_http import streamable_http_client
from mcp.types import ElicitResult

load_dotenv(Path(__file__).resolve().parents[1] / ".env")
URL = os.getenv("MCP_URL", "http://127.0.0.1:8000/mcp")


async def ask_human(context, params):
    answer = input(f"\n{params.message} [y/N] ").strip().lower()
    return ElicitResult(action="accept", content={"approve": answer == "y"})


async def main(role: str, tool: str, arguments: str = "{}"):
    key = os.environ[f"MCP_{role.upper()}_KEY"]
    async with httpx2.AsyncClient(headers={"Authorization": f"Bearer {key}"}) as http:
        async with Client(streamable_http_client(URL, http_client=http), elicitation_callback=ask_human) as client:
            result = await client.call_tool(tool, json.loads(arguments))
    print("ERROR:" if result.is_error else "OK:", *(block.text for block in result.content), sep="\n")


if __name__ == "__main__":
    if len(sys.argv) < 3:
        sys.exit(__doc__)
    asyncio.run(main(*sys.argv[1:4]))
