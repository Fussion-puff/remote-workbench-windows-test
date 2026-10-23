"""Resolve a Codex conversation only from transport-provided MCP metadata."""
from __future__ import annotations

from dataclasses import dataclass
import re

from mcp.server.fastmcp import Context

_IDENTIFIER = re.compile(r"[A-Za-z0-9_-]{1,128}\Z")


class SessionIdentityUnavailable(ValueError):
    """The caller did not provide a usable conversation identity."""


@dataclass(frozen=True)
class SessionIdentity:
    thread_id: str
    session_id: str | None = None
    window_id: str | None = None
    item_id: str | None = None


def get_codex_identity(ctx: Context) -> SessionIdentity:
    try:
        meta = ctx.request_context.meta
    except (AttributeError, LookupError, ValueError):
        meta = None
    if hasattr(meta, "model_dump"):
        meta = meta.model_dump(by_alias=True, exclude_none=True)
    if not isinstance(meta, dict):
        meta = {}

    def identifier(key):
        value = meta.get(key)
        return value if isinstance(value, str) and _IDENTIFIER.fullmatch(value) else None

    thread_id = identifier("threadId")
    if thread_id is None:
        raise SessionIdentityUnavailable(
            "Codex threadId is unavailable in MCP request metadata. "
            "Refusing implicit workspace routing; no remote command was executed."
        )
    return SessionIdentity(thread_id, identifier("sessionId"), identifier("windowId"), identifier("itemId"))


def get_codex_thread_id(ctx: Context) -> str:
    return get_codex_identity(ctx).thread_id
