import asyncio
import json
import time

import pytest

from tests.test_agent import finish, reply, runner_with, scripted
from zhunt.agent import runner as R
from zhunt.agent.calls import BadCall, fit_arguments, parse_arguments, resolve_name
from zhunt.agent.tools import Tool, ToolError


def tool(name, run, risk="read", properties=None, required=()):
    return Tool(name, f"{name} tool", {"type": "object", "properties": properties or {"q": {"type": "string"}}, "required": list(required)}, risk, run)


async def echo(q: str = "") -> str:
    return f"echo:{q}"


async def search(query: str, limit: int = 6) -> str:
    return f"search:{query}:{limit}"


TOOLS = {"web_search": tool("web_search", search, properties={"query": {"type": "string"}, "limit": {"type": "integer", "minimum": 1, "maximum": 10}}, required=["query"]),
         "read_file": tool("read_file", echo, properties={"path": {"type": "string"}}, required=["path"])}


def test_names_are_normalised_but_never_guessed():
    assert resolve_name("read_file<|channel|>commentary", TOOLS) == "read_file"
    assert resolve_name(" functions.read_file ", TOOLS) == "read_file"
    assert resolve_name("READ-FILE", TOOLS) == "read_file"
    assert resolve_name("read_files", TOOLS) is None  # a near miss is an error, not another tool
    assert resolve_name("write_file", TOOLS) is None
    assert resolve_name(None, TOOLS) is None


def test_arguments_survive_common_free_model_slips():
    assert parse_arguments('{"path": "a.txt"}') == {"path": "a.txt"}
    assert parse_arguments(json.dumps(json.dumps({"path": "a.txt"}))) == {"path": "a.txt"}  # encoded twice
    assert parse_arguments("") == {} and parse_arguments(None) == {} and parse_arguments("null") == {}
    assert parse_arguments('{"path": "a\nb"}') == {"path": "a\nb"}  # raw newline inside a string
    for bad in ("[1, 2]", "42", "{not json"):
        with pytest.raises(BadCall):
            parse_arguments(bad)


def test_arguments_are_fitted_to_the_schema():
    search = TOOLS["web_search"]
    assert fit_arguments(search, {"query": "cats", "limit": "5", "junk": 1}) == {"query": "cats", "limit": 5}
    assert fit_arguments(search, {"query": 42, "limit": 99}) == {"query": "42", "limit": 10}  # clamped
    assert fit_arguments(search, {"query": "x", "limit": 3.0})["limit"] == 3
    with pytest.raises(BadCall, match=r"missing query.*Usage: web_search\(query: string, limit\?: integer\)"):
        fit_arguments(search, {})
    with pytest.raises(BadCall, match="limit must be integer"):
        fit_arguments(search, {"query": "x", "limit": "many"})
    with pytest.raises(BadCall):
        fit_arguments(search, {"query": "x", "limit": float("inf")})
    with pytest.raises(BadCall):
        fit_arguments(search, {"query": "x", "limit": True})


def run_task(tmp_path, replies, tools, goal="go", decide=None):
    seen, transport = scripted(*replies)

    async def go():
        runner = runner_with(tmp_path, transport)
        runner.tools = lambda: tools
        task = runner.start(goal)
        await finish(runner, task, decide)
        return task

    return seen, asyncio.run(go())


def tool_messages(request):
    return [m["content"] for m in request["messages"] if m["role"] == "tool"]


def test_a_repaired_call_runs_without_another_model_round_trip(tmp_path):
    seen, task = run_task(tmp_path, [
        reply(calls=[("web_search<|channel|>json", {"query": "cats", "limit": "3"})]), reply("done"),
    ], dict(TOOLS))
    assert task.state == "done" and len(seen) == 2
    assert tool_messages(seen[1]) == ["search:cats:3"]
    event = [e for e in task.events if e["kind"] == "tool"][0]
    assert event["name"] == "web_search" and event["ok"] is True and event["args"] == {"query": "cats", "limit": 3}


def test_a_wrong_call_gets_one_specific_error_the_model_can_fix(tmp_path):
    seen, task = run_task(tmp_path, [
        reply(calls=[("read_file", {})]), reply(calls=[("nothing_like_it", {})]), reply("giving up"),
    ], dict(TOOLS))
    assert "missing path. Usage: read_file(path: string)" in tool_messages(seen[1])[0]
    unknown = tool_messages(seen[2])[-1]
    assert "no tool called 'nothing_like_it'" in unknown and "Available tools: read_file, web_search" in unknown
    assert task.state == "done"


def test_independent_read_calls_run_together_and_results_keep_their_order(tmp_path):
    async def slow(q: str = "") -> str:
        await asyncio.sleep(0.3)
        return f"done:{q}"

    tools = {"a": tool("a", slow), "b": tool("b", slow), "c": tool("c", slow)}
    started = time.perf_counter()
    seen, task = run_task(tmp_path, [reply(calls=[("a", {"q": "1"}), ("b", {"q": "2"}), ("c", {"q": "3"})]), reply("ok")], tools)
    elapsed = time.perf_counter() - started
    assert elapsed < 0.75  # three 0.3 s calls would take 0.9 s one by one
    assert tool_messages(seen[1]) == ["done:1", "done:2", "done:3"]


def test_calls_needing_approval_are_never_run_together_or_before_approval(tmp_path):
    ran = []

    async def write(q: str = "") -> str:
        ran.append(q)
        return "written"

    tools = {"write": tool("write", write, risk="write"), "look": tool("look", echo)}
    approved = []

    def decide(event):
        approved.append(list(ran))
        return True

    _, task = run_task(tmp_path, [reply(calls=[("look", {"q": "x"}), ("write", {"q": "1"}), ("write", {"q": "2"})]), reply("ok")], tools, decide=decide)
    assert task.state == "done" and ran == ["1", "2"]
    assert approved == [[], ["1"]]  # each write waited for its own approval, in order


def test_the_same_failing_call_is_not_run_a_third_time(tmp_path):
    runs = []

    async def broken(q: str = "") -> str:
        runs.append(q)
        raise ToolError("no such page")

    tools = {"broken": tool("broken", broken)}
    call = ("broken", {"q": "x"})
    seen, task = run_task(tmp_path, [reply(calls=[call]), reply(calls=[call]), reply(calls=[call]), reply("stop")], tools)
    assert runs == ["x", "x"]
    assert "already failed twice" in tool_messages(seen[3])[-1]
    assert task.state == "done"


def test_an_empty_reply_is_nudged_once_then_the_task_continues(tmp_path):
    seen, task = run_task(tmp_path, [reply(""), reply("the answer")], dict(TOOLS))
    assert task.state == "done" and task.answer == "the answer" and len(seen) == 2
    assert seen[1]["messages"][-1] == {"role": "user", "content": "Your last reply was empty. Call a tool or give your final answer."}
    # a second empty reply is not nudged again
    _, again = run_task(tmp_path, [reply(""), reply("")], dict(TOOLS))
    assert again.state == "done" and "without a written answer" in again.answer


def test_a_stuck_tool_times_out_instead_of_holding_the_task(tmp_path, monkeypatch):
    monkeypatch.setattr(R, "TOOL_TIMEOUT", 0.05)

    async def hang(q: str = "") -> str:
        await asyncio.sleep(10)
        return "never"

    seen, task = run_task(tmp_path, [reply(calls=[("hang", {})]), reply("ok")], {"hang": tool("hang", hang)})
    assert "did not finish within" in tool_messages(seen[1])[0] and task.state == "done"
