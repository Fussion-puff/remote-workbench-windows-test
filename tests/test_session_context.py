from types import SimpleNamespace

import pytest
from mcp import types
from mcp.server.fastmcp import Context

from remote_workbench.session_context import SessionIdentityUnavailable, get_codex_identity


def context(meta):
    return SimpleNamespace(request_context=SimpleNamespace(meta=meta))


def test_identity_comes_from_sdk_extra_metadata():
    meta = types.RequestParams.Meta(threadId="thread-a", sessionId="transport-1", windowId="window-2")
    identity = get_codex_identity(context(meta))
    assert identity.thread_id == "thread-a"
    assert identity.session_id == "transport-1"
    assert identity.window_id == "window-2"
    assert identity.item_id is None


@pytest.mark.parametrize("meta", [None, {}, {"sessionId": "only-transport"}, {"thread_id": "wrong-key"},
                                  {"threadId": ""}, {"threadId": "../other"}, {"threadId": 12},
                                  {"threadId": "a" * 129}, {"threadId": "valid\n"}])
def test_no_identity_fallback(meta, monkeypatch):
    monkeypatch.setenv("CODEX_THREAD_ID", "environment-must-not-route")
    with pytest.raises(SessionIdentityUnavailable):
        get_codex_identity(context(meta))


def test_context_outside_request_is_not_a_thread():
    with pytest.raises(SessionIdentityUnavailable):
        get_codex_identity(Context())


def test_new_transport_session_does_not_change_conversation_identity():
    a = get_codex_identity(context({"threadId": "thread-a", "sessionId": "first"}))
    b = get_codex_identity(context({"threadId": "thread-a", "sessionId": "second"}))
    assert a.thread_id == b.thread_id
    assert a.session_id != b.session_id
