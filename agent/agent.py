"""
A terminal support agent for Acme Store: Gemini decides, the MCP server acts.

The loop is written by hand on purpose, to show what an agent actually is:
  1. send the conversation and the tool list to the model
  2. if it asks for tools, run them through MCP and send the results back
  3. repeat until it answers in plain text

Start the server first (MCP_TRANSPORT=http python server/acme_server.py), then:
    python agent/agent.py            # agent key: can write
    python agent/agent.py viewer     # read-only key
"""

import asyncio
import os
import sys
from pathlib import Path

import httpx2
from dotenv import load_dotenv
from google import genai
from google.genai import errors, types
from mcp.client import Client
from mcp.client.streamable_http import streamable_http_client
from mcp.types import ElicitResult

load_dotenv(Path(__file__).resolve().parents[1] / ".env")
URL = os.getenv("MCP_URL", "http://127.0.0.1:8000/mcp")
MODEL = os.getenv("GEMINI_MODEL", "gemini-3.5-flash-lite")
MAX_STEPS = 8  # stops a confused model from calling tools forever

# USD per 1M (input, output incl. thinking) tokens, paid tier, ai.google.dev pricing, checked 2026-11-25.
# gemini-3.8-flash rises to (1.50, 7.50) on 2027-01-01. Free-tier use costs nothing.
PRICES = {"gemini-3.8-flash": (0.75, 3.75), "gemini-3.5-flash-lite": (0.30, 2.50)}


def cost_usd(input_tokens: int, output_tokens: int) -> float | None:
    """Estimated cost, or None for a model whose price is not listed above."""
    if MODEL not in PRICES:
        return None
    input_price, output_price = PRICES[MODEL]
    return (input_tokens * input_price + output_tokens * output_price) / 1_000_000


def make_llm():
    """Return `generate(contents, config)` backed by Gemini. Tests pass a fake instead."""
    client = genai.Client(http_options=types.HttpOptions(
        timeout=60_000,  # milliseconds; without it a stuck request hangs forever
        retry_options=types.HttpRetryOptions(attempts=3),
    ))

    async def generate(contents, config):
        return await client.aio.models.generate_content(model=MODEL, contents=contents, config=config)

    return generate


async def ask(generate, mcp_client: Client, history: list, question: str) -> dict:
    """Answer one question. Returns the answer, the tools used, and token counts."""
    tools = (await mcp_client.list_tools()).tools  # discovered at run time, not hard-coded
    config = types.GenerateContentConfig(
        system_instruction=mcp_client.instructions,
        tools=[types.Tool(function_declarations=[
            types.FunctionDeclaration(name=t.name, description=t.description, parameters_json_schema=t.input_schema)
            for t in tools
        ])],
    )
    history.append(types.Content(role="user", parts=[types.Part(text=question)]))
    calls: list[tuple[str, dict]] = []
    input_tokens = output_tokens = 0

    for _ in range(MAX_STEPS):
        response = await generate(history, config)
        usage = response.usage_metadata
        if usage:
            input_tokens += usage.prompt_token_count or 0
            # Thinking tokens are billed as output.
            output_tokens += (usage.candidates_token_count or 0) + (usage.thoughts_token_count or 0)

        content = response.candidates[0].content if response.candidates else None
        if not content or not content.parts:
            answer = "The model returned nothing. Please try rephrasing."
            break
        history.append(content)  # keep it unchanged: it carries the model's thought signatures

        requested = [p.function_call for p in content.parts if p.function_call]
        if not requested:
            answer = "".join(p.text for p in content.parts if p.text and not p.thought)
            break

        results = []
        for call in requested:
            arguments = dict(call.args or {})
            calls.append((call.name, arguments))
            print(f"  -> {call.name}({arguments})", file=sys.stderr)
            try:
                result = await mcp_client.call_tool(call.name, arguments)
                text = "\n".join(block.text for block in result.content)
                payload = {"error" if result.is_error else "result": text}
            except Exception as exc:  # unknown tool, lost connection: tell the model, don't crash
                payload = {"error": str(exc)}
            results.append(types.Part(function_response=types.FunctionResponse(
                id=call.id, name=call.name, response=payload)))
        history.append(types.Content(role="user", parts=results))
    else:
        answer = f"Stopped after {MAX_STEPS} steps without a final answer."

    return {"answer": answer, "calls": calls, "input_tokens": input_tokens, "output_tokens": output_tokens}


async def ask_human(context, params):
    """The server's confirmation request goes to the person, never to the model."""
    answer = input(f"\nAPPROVAL NEEDED: {params.message} [y/N] ").strip().lower()
    return ElicitResult(action="accept", content={"approve": answer == "y"})


async def main(role: str = "agent"):
    if role not in ("agent", "viewer"):
        sys.exit("Usage: python agent/agent.py [agent|viewer]")
    generate = make_llm()
    headers = {"Authorization": f"Bearer {os.environ[f'MCP_{role.upper()}_KEY']}"}
    async with httpx2.AsyncClient(headers=headers) as http:
        async with Client(streamable_http_client(URL, http_client=http), elicitation_callback=ask_human) as client:
            print(f"Acme support agent ({role}, {MODEL}). Empty line to quit.")
            history: list = []
            total_cost = 0.0
            while question := input("\nYou: ").strip():
                turn_start = len(history)
                try:
                    result = await ask(generate, client, history, question)
                except (errors.APIError, httpx2.TransportError) as exc:  # transport: timeouts, network
                    del history[turn_start:]  # drop the half-finished turn
                    print(f"Model unavailable: {exc}")
                    continue
                print(f"\nAgent: {result['answer']}")
                usage = f"{result['input_tokens']} input / {result['output_tokens']} output tokens"
                cost = cost_usd(result["input_tokens"], result["output_tokens"])
                if cost is not None:
                    total_cost += cost
                    usage += f", ${cost:.4f}, conversation ${total_cost:.4f}"
                print(f"  [{usage}]")


if __name__ == "__main__":
    asyncio.run(main(*sys.argv[1:2]))
