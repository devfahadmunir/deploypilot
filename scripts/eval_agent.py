"""
Agent evals: does the model pick the right tools and give a correct answer?

Runs each case in evals/agent_set.json through the real model and the real server
(in memory). Every approval request is answered "no", so no refund is ever issued.

    python scripts/eval_agent.py

Needs the database (freshly seeded: refund windows depend on dates), Ollama, and GEMINI_API_KEY.
"""

import asyncio
import json
import logging
import os
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
os.environ["MCP_ROLE"] = "agent"  # the declined-refund case needs write access

import httpx2  # noqa: E402
from google.genai import errors  # noqa: E402
from mcp.client import Client  # noqa: E402
from mcp.types import ElicitResult  # noqa: E402

from agent.agent import MODEL, ask, cost_usd, make_llm  # noqa: E402
from server.acme_server import mcp  # noqa: E402
from server.db import query, write_transaction  # noqa: E402


def check(case: dict, calls: list, answer: str) -> list[str]:
    """Return the reasons a case failed (empty list = passed)."""
    used = {name for name, _ in calls}
    problems = [f"did not call {t}" for t in case.get("expect_tools", []) if not used & set(t.split("|"))]
    problems += [f"called forbidden {t}" for t in case.get("forbid_tools", []) if t in used]
    names = [name for name, _ in calls]
    problems += [f"called {t} more than once" for t in case.get("only_once", []) if names.count(t) > 1]
    # ponytail: regex on the answer; use an LLM judge if wording variety causes false failures
    if not re.search(case["answer_must_match"], answer, re.I):
        problems.append("answer is missing the expected fact")
    if case.get("answer_must_not_match") and re.search(case["answer_must_not_match"], answer, re.I):
        problems.append("answer states the opposite")
    return problems


async def decline(context, params):
    return ElicitResult(action="decline")


async def main():
    logging.disable(logging.INFO)
    cases = json.loads((ROOT / "evals" / "agent_set.json").read_text())
    generate = make_llm()
    last_ticket = query("SELECT COALESCE(MAX(id), 0) AS id FROM tickets")[0]["id"]
    passed = ran = input_tokens = output_tokens = 0
    try:
        async with Client(mcp, elicitation_callback=decline) as client:
            for case in cases:
                try:
                    try:
                        result = await ask(generate, client, [], case["question"])
                    except errors.APIError as exc:
                        if exc.code != 429:
                            raise
                        print("    rate limited, waiting 60s...", flush=True)
                        await asyncio.sleep(60)  # free-tier limits are per minute
                        result = await ask(generate, client, [], case["question"])
                except (errors.APIError, httpx2.TransportError) as exc:
                    print(f"⚠️  {case['id']}: model unavailable ({type(exc).__name__} {getattr(exc, 'code', '')}), not scored", flush=True)
                    continue
                ran += 1
                input_tokens += result["input_tokens"]
                output_tokens += result["output_tokens"]
                problems = check(case, result["calls"], result["answer"])
                passed += not problems
                print(f"{'❌' if problems else '✅'} {case['id']}: {', '.join(n for n, _ in result['calls']) or 'no tools'}")
                if problems:
                    print(f"    {'; '.join(problems)}\n    answer: {result['answer'][:300]!r}")
    finally:
        # A model that misbehaves may open a ticket; remove anything this run created.
        with write_transaction() as conn:
            conn.execute("DELETE FROM audit_log WHERE tool = 'create_ticket' AND (result->>'ticket_id')::int > %s", (last_ticket,))
            conn.execute("DELETE FROM tickets WHERE id > %s", (last_ticket,))

    cost = cost_usd(input_tokens, output_tokens)
    print(f"\n{MODEL}: passed {passed}/{ran} (of {len(cases)} cases)")
    print(f"tokens: {input_tokens} input, {output_tokens} output")
    if cost is not None and ran:
        print(f"cost: ${cost:.4f} total, ${cost / ran:.4f} per conversation (paid-tier list price)")


if __name__ == "__main__":
    asyncio.run(main())
