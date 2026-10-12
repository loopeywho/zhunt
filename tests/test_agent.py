import asyncio
import json
from pathlib import Path
from unittest.mock import AsyncMock, patch

import httpx
import pytest

from zhunt.agent import tools as T
from zhunt.agent.runner import AgentRunner, parse_interval


def reply(content="", calls=None, model="groq/test"):
    message = {"role": "assistant", "content": content}
    if calls:
        message["tool_calls"] = [
            {"id": f"c{i}", "type": "function", "function": {"name": name, "arguments": json.dumps(args)}}
            for i, (name, args) in enumerate(calls)
        ]
    return {"model": model, "choices": [{"message": message}]}


def scripted(*replies):
    """A fake zhunt /v1/chat/completions that returns the given replies in order and records requests."""

    seen = []
    queue = list(replies)

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(json.loads(request.content))
        return httpx.Response(200, json=queue.pop(0))

    return seen, (lambda: httpx.MockTransport(handler))


def runner_with(tmp_path: Path, transport_factory, folders=()):
    settings = tmp_path / "agent.json"
    settings.write_text(json.dumps({"folders": [str(f) for f in folders]}))
    runner = AgentRunner(settings)
    runner._transport_factory = transport_factory
    runner._key = "k"
    return runner


async def finish(runner, task, decide=None, timeout=5.0):
    async def wait():
        while task.state in ("running", "waiting"):
            if decide and task.state == "waiting":
                pending = [e for e in task.events if e["kind"] == "approval" and not any(d["kind"] == "decision" and d["call_id"] == e["call_id"] for d in task.events)]
                for e in pending:
                    runner.decide(task.id, e["call_id"], decide(e))
            await asyncio.sleep(0.01)
    await asyncio.wait_for(wait(), timeout)


def test_read_tools_run_straight_away_and_the_answer_ends_the_task(tmp_path):
    shared = tmp_path / "docs"
    shared.mkdir()
    (shared / "note.txt").write_text("the meeting is on Friday")
    seen, transport = scripted(reply(calls=[("read_file", {"path": "note.txt"})]), reply("It is on Friday."))

    async def go():
        runner = runner_with(tmp_path, transport, [shared])
        task = runner.start("When is the meeting?")
        await finish(runner, task)
        return task

    task = asyncio.run(go())
    assert task.state == "done" and task.answer == "It is on Friday."
    tool = [e for e in task.events if e["kind"] == "tool"][0]
    assert tool["ok"] and "Friday" in tool["result"]
    assert not [e for e in task.events if e["kind"] == "approval"]
    # the tool result went back to the model, and tools were offered
    assert seen[1]["messages"][-1]["role"] == "tool" and "Friday" in seen[1]["messages"][-1]["content"]
    assert {t["function"]["name"] for t in seen[0]["tools"]} >= {"web_search", "open_page", "read_file", "write_file"}


@pytest.mark.parametrize("approve", [True, False])
def test_writes_wait_for_the_user_and_only_happen_when_approved(tmp_path, approve):
    shared = tmp_path / "out"
    shared.mkdir()
    seen, transport = scripted(reply("Saving the summary.", calls=[("write_file", {"path": "summary.md", "content": "# Hi"})]), reply("Done."))

    async def go():
        runner = runner_with(tmp_path, transport, [shared])
        task = runner.start("write a summary")
        await finish(runner, task, decide=lambda e: approve)
        return task

    task = asyncio.run(go())
    assert task.state == "done"
    assert (shared / "summary.md").exists() is approve
    kinds = [e["kind"] for e in task.events]
    assert kinds.index("approval") < kinds.index("decision")
    if not approve:
        assert "declined" in seen[1]["messages"][-1]["content"]


def test_files_outside_the_shared_folders_are_refused(tmp_path):
    shared = tmp_path / "shared"
    shared.mkdir()
    secret = tmp_path / "secret.txt"
    secret.write_text("no")
    settings = T.AgentSettings(folders=[shared])
    for bad in (str(secret), "../secret.txt", "/etc/passwd"):
        with pytest.raises(T.ToolError, match="outside"):
            T._inside(settings, bad)
    with pytest.raises(T.ToolError, match="no folders"):
        T._inside(T.AgentSettings(), "x")


@pytest.mark.parametrize("url", ["http://127.0.0.1:4000/zhunt/api/status", "http://localhost/", "http://10.0.0.5/", "http://169.254.169.254/latest", "file:///etc/passwd", "ftp://example.com"])
def test_the_agent_cannot_open_this_computer_or_private_networks(url):
    with pytest.raises(T.ToolError):
        T.check_url(url)


def test_search_results_are_parsed_from_the_html_page():
    page = (
        '<a class="result__a" href="//duckduckgo.com/l/?uddg=https%3A%2F%2Fexample.com%2Fa&rut=x">Example <b>A</b></a>'
        '<a class="result__snippet" href="#">First &amp; best</a>'
    )
    with patch.object(T, "_get", AsyncMock(return_value=httpx.Response(200, text=page))):
        out = asyncio.run(T.web_search("example"))
    assert "Example A" in out and "https://example.com/a" in out and "First & best" in out


def test_a_failing_model_fails_the_task_with_a_reason(tmp_path):
    def handler(request):
        return httpx.Response(503, json={"error": {"message": "no free model available"}})

    async def go():
        runner = runner_with(tmp_path, lambda: httpx.MockTransport(handler))
        task = runner.start("anything")
        await finish(runner, task)
        return task

    task = asyncio.run(go())
    assert task.state == "failed" and "no free model" in task.events[-1]["text"]


def test_tool_sets_limit_what_the_model_is_offered(tmp_path):
    seen, transport = scripted(reply("ok"))

    async def go():
        runner = runner_with(tmp_path, transport)
        task = runner.start("look it up", tools={"web_search", "open_page"})
        await finish(runner, task)

    asyncio.run(go())
    assert {t["function"]["name"] for t in seen[0]["tools"]} == {"web_search", "open_page"}


def test_intervals():
    assert parse_interval("30m") == 1800 and parse_interval("2h") == 7200 and parse_interval("1d") == 86400
    assert parse_interval("soon") is None
