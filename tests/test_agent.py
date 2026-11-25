"""The agent loop, with a scripted fake model: no API key or network needed."""

import asyncio

from google.genai import types
from mcp.client import Client

from agent.agent import MAX_STEPS, ask
from scripts.eval_agent import check
from server.acme_server import mcp


def reply(part: types.Part) -> types.GenerateContentResponse:
    return types.GenerateContentResponse(candidates=[types.Candidate(content=types.Content(role="model", parts=[part]))])


def wants(tool: str, **args):
    return reply(types.Part(function_call=types.FunctionCall(name=tool, args=args)))


def run(script):
    history: list = []

    async def generate(contents, config):
        return script(contents)

    async def _run():
        async with Client(mcp) as client:
            return await ask(generate, client, history, "Who is Ali?")

    return asyncio.run(_run()), history


def test_runs_the_requested_tool_and_returns_the_answer():
    replies = iter([wants("get_customer", email="ali.raza@example.com"), reply(types.Part(text="Ali is premium."))])
    result, history = run(lambda contents: next(replies))
    assert result["answer"] == "Ali is premium."
    assert result["calls"] == [("get_customer", {"email": "ali.raza@example.com"})]
    assert '"premium"' in history[2].parts[0].function_response.response["result"]


def test_tool_errors_go_back_to_the_model():
    replies = iter([wants("get_customer", email="nobody@example.com"), reply(types.Part(text="Not found."))])
    _, history = run(lambda contents: next(replies))
    assert "No customer found" in history[2].parts[0].function_response.response["error"]


def test_loop_is_bounded():
    result, _ = run(lambda contents: wants("get_customer", email="ali.raza@example.com"))
    assert "Stopped after" in result["answer"] and len(result["calls"]) == MAX_STEPS


def test_eval_checker_catches_a_forbidden_tool_and_a_wrong_answer():
    case = {"expect_tools": ["issue_refund"], "forbid_tools": ["create_ticket"], "answer_must_match": "declin"}
    assert check(case, [("issue_refund", {})], "The refund was declined.") == []
    assert len(check(case, [("create_ticket", {})], "Awaiting review.")) == 3
