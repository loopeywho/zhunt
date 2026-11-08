import json
import os
import stat
from unittest.mock import AsyncMock

import pytest

from tests.test_engine import ok
from tests.test_workspace_api import headers, workspace
from zhunt.chats import MAX_CHATS, MAX_MESSAGES, ChatError, ChatStore

A, B, P = "a" * 16, "b" * 16, "c" * 16


def msgs(*pairs):
    return [{"role": r, "content": c} for r, c in pairs]


def api(client, method, path, **kw):
    return getattr(client, method)("/zhunt/api" + path, headers=headers(), **kw)


@pytest.fixture
def client():
    with workspace(AsyncMock(return_value=ok()), env={}) as (c, _hook):
        yield c


def test_every_route_needs_the_master_key(client):
    for method, path in [("get", "/chats"), ("get", f"/chats/{A}"), ("put", f"/chats/{A}"), ("delete", f"/chats/{A}"),
                         ("get", "/projects"), ("put", f"/projects/{P}"), ("delete", f"/projects/{P}")]:
        assert getattr(client, method)("/zhunt/api" + path).status_code == 401, path


def test_save_load_list_and_delete(client):
    body = {"messages": msgs(("user", "How do I plan a trip to Penang?"), ("assistant", "Start with the food.")), "alias": "zhunt-chat"}
    saved = api(client, "put", f"/chats/{A}", json=body).json()
    assert saved["rev"] == 1
    chat = api(client, "get", f"/chats/{A}").json()
    assert chat["title"] == "How do I plan a trip to Penang?" and chat["messages"][1]["content"] == "Start with the food."
    listed = api(client, "get", "/chats").json()["chats"]
    assert [(c["id"], c["count"], c["rev"]) for c in listed] == [(A, 2, 1)] and "messages" not in listed[0]
    assert api(client, "delete", f"/chats/{A}").status_code == 200
    assert api(client, "get", f"/chats/{A}").status_code == 404 and api(client, "get", "/chats").json()["chats"] == []
    assert api(client, "delete", f"/chats/{A}").status_code == 404


def test_a_second_window_with_an_old_copy_cannot_overwrite_newer_messages(client):
    first = api(client, "put", f"/chats/{A}", json={"messages": msgs(("user", "hi"))}).json()
    newer = api(client, "put", f"/chats/{A}", json={"rev": first["rev"], "messages": msgs(("user", "hi"), ("assistant", "hello"))}).json()
    stale = api(client, "put", f"/chats/{A}", json={"rev": first["rev"], "messages": msgs(("user", "hi"), ("user", "lost?"))})
    assert stale.status_code == 409 and newer["rev"] == 2
    assert api(client, "put", f"/chats/{A}", json={"messages": msgs(("user", "no rev"))}).status_code == 409
    assert len(api(client, "get", f"/chats/{A}").json()["messages"]) == 2


def test_validation_and_caps(client):
    put = lambda cid, body: api(client, "put", f"/chats/{cid}", json=body).status_code  # noqa: E731
    assert put(A, {"messages": []}) == 400  # nothing to save: no empty files
    assert put(A, {"messages": [{"role": "system", "content": "x"}]}) == 400
    assert put(A, {"messages": [{"role": "user", "content": 5}]}) == 400
    assert put(A, {"messages": msgs(("user", "x")), "project": "../etc"}) == 400
    assert put(A, {"messages": msgs(*[("user", "x")] * (MAX_MESSAGES + 1))}) == 413
    assert put("not-an-id", {"messages": msgs(("user", "x"))}) == 400
    assert put("../../escape0000000", {"messages": msgs(("user", "x"))}) in (400, 404)
    assert api(client, "put", f"/chats/{A}", content=b"{" * 10).status_code == 400
    assert api(client, "put", f"/chats/{A}", content=b"x" * (1024 * 1024 + 10)).status_code == 413
    assert api(client, "get", "/chats").json()["chats"] == []


def test_the_chat_count_cap_refuses_instead_of_pruning(tmp_path):
    store = ChatStore(tmp_path)
    store._index = {f"{n:016x}": {"id": f"{n:016x}", "title": "t", "project": None, "updated": 1, "count": 1, "rev": 1} for n in range(MAX_CHATS)}
    with pytest.raises(ChatError) as caught:
        store.put(A, {"messages": msgs(("user", "one too many"))})
    assert caught.value.status == 409 and len(store.index()) == MAX_CHATS


def test_files_are_private_and_symlinks_are_not_followed(client, tmp_path):
    api(client, "put", f"/chats/{A}", json={"messages": msgs(("user", "secret plan"))})
    base = client.app.app.state.chats.directory if hasattr(client.app, "app") else client.app.state.chats.directory
    for name in (f"{A}.json", "index.json"):
        assert stat.S_IMODE(os.stat(base / name).st_mode) == 0o600
    outside = tmp_path / "outside.json"
    outside.write_text(json.dumps({"id": B, "title": "stolen", "rev": 1, "updated": 1, "messages": []}))
    (base / f"{B}.json").symlink_to(outside)
    assert api(client, "get", f"/chats/{B}").status_code == 404
    assert [c["id"] for c in api(client, "get", "/chats").json()["chats"]] == [A]


def test_a_damaged_chat_file_is_skipped_and_a_lost_index_is_rebuilt(tmp_path):
    store = ChatStore(tmp_path)
    store.put(A, {"messages": msgs(("user", "keep me"))})
    store.put(B, {"messages": msgs(("user", "break me"))})
    (tmp_path / f"{B}.json").write_text("{not json")
    (tmp_path / "index.json").write_text("garbage")
    fresh = ChatStore(tmp_path)
    assert [m["id"] for m in fresh.list()] == [A]
    assert fresh.get(A)["messages"][0]["content"] == "keep me"
    (tmp_path / "index.json").unlink()
    assert [m["id"] for m in ChatStore(tmp_path).list()] == [A]


def test_search_reads_titles_and_message_text_but_the_plain_list_does_not(tmp_path, monkeypatch):
    store = ChatStore(tmp_path)
    store.put(A, {"messages": msgs(("user", "Plan a trip"), ("assistant", "Try the laksa near the harbour."))})
    store.put(B, {"messages": msgs(("user", "Write a regex"), ("assistant", "Use ^[A-Z]{1,2}[0-9]"))})
    by_text = store.list(q="LAKSA")
    assert [m["id"] for m in by_text] == [A] and "laksa" in by_text[0]["snippet"]
    assert [m["id"] for m in store.list(q="regex")] == [B]
    reads = []
    original = ChatStore._valid
    monkeypatch.setattr(ChatStore, "_valid", staticmethod(lambda chat, cid: reads.append(cid) or original(chat, cid)))
    store.list()
    assert reads == []  # listing comes from the index


def test_projects_group_chats_and_deleting_one_leaves_chats_without_a_project(client):
    made = api(client, "put", f"/projects/{P}", json={"name": "Trips", "instructions": "Answer like a local guide."})
    assert made.json() == {"id": P, "name": "Trips", "instructions": "Answer like a local guide."}
    assert api(client, "put", f"/projects/{'d' * 16}", json={"name": "trips"}).status_code == 409  # same name
    assert api(client, "put", f"/projects/{'d' * 16}", json={"name": " "}).status_code == 400
    api(client, "put", f"/chats/{A}", json={"messages": msgs(("user", "Penang")), "project": P})
    api(client, "put", f"/chats/{B}", json={"messages": msgs(("user", "regex"))})
    assert [c["id"] for c in api(client, "get", f"/chats?project={P}").json()["chats"]] == [A]
    assert [c["id"] for c in api(client, "get", "/chats?project=none").json()["chats"]] == [B]
    assert api(client, "delete", f"/projects/{P}").status_code == 200
    assert api(client, "get", "/projects").json()["projects"] == []
    assert api(client, "get", f"/chats/{A}").json()["project"] == P  # the chat is untouched; the UI shows "no project"
    assert api(client, "delete", f"/projects/{P}").status_code == 404


def test_a_chat_deleted_elsewhere_is_not_brought_back_by_a_stale_save(client):
    saved = api(client, "put", f"/chats/{A}", json={"messages": msgs(("user", "hi"))}).json()
    api(client, "delete", f"/chats/{A}")
    stale = api(client, "put", f"/chats/{A}", json={"rev": saved["rev"], "messages": msgs(("user", "hi"), ("assistant", "back?"))})
    assert stale.status_code == 404 and api(client, "get", "/chats").json()["chats"] == []
    assert api(client, "put", f"/chats/{A}", json={"messages": msgs(("user", "fresh"))}).status_code == 200  # a new chat with no rev is fine


def test_search_does_not_hold_the_lock_while_it_reads_files(tmp_path, monkeypatch):
    import zhunt.chats as module
    store = ChatStore(tmp_path)
    store.put(A, {"messages": msgs(("user", "alpha"), ("assistant", "beta"))})
    held = []
    original = module._safe_read
    monkeypatch.setattr(module, "_safe_read", lambda path: held.append(store.lock._is_owned()) or original(path))
    store.list(q="beta")
    assert held and not any(held)
