from __future__ import annotations

from typing import Any, Dict, Optional, Protocol, runtime_checkable

from a2a.server.agent_execution import RequestContext


REQUEST_KIND_CHAT = "chat"
REQUEST_KIND_PROCESS = "process"


@runtime_checkable
class TextStreamer(Protocol):
    async def send_text(self, text: str) -> None: ...


def _extras(context: RequestContext) -> Dict[str, Any]:
    """Framework-level accessor for RequestContext extras.

    We intentionally avoid depending on concrete RequestContext subclasses here.
    """
    try:
        data = context.get_context_data() or {}
    except Exception:
        return {}
    extras = data.get("extras")
    return extras if isinstance(extras, dict) else {}


def get_request_kind(context: RequestContext) -> Optional[str]:
    """Returns the request kind (e.g. 'chat', 'process') if present."""
    kind = _extras(context).get("request_kind")
    return kind if isinstance(kind, str) and kind else None


def is_chat_request(context: RequestContext) -> bool:
    """True if the context declares itself as chat mode."""
    return get_request_kind(context) == REQUEST_KIND_CHAT


def get_streamer(context: RequestContext) -> Optional[TextStreamer]:
    """Returns the streaming handle if present.

    Executors should use this instead of reaching into extras directly.
    """
    s = _extras(context).get("streamer")
    return s if isinstance(s, TextStreamer) else None


async def emit_chunk_text(context: RequestContext, text: str) -> bool:
    """Emit a best-effort streaming chunk.

    - In chat(SSE) mode this will stream a chunk.
    - In non-streaming contexts it becomes a no-op.
    """
    streamer = get_streamer(context)
    if streamer is None:
        return False
    await streamer.send_text(text)
    return True


async def emit_chunk_json(context: RequestContext, data: Any) -> bool:
    """Emit a best-effort JSON chunk if supported."""
    streamer = _extras(context).get("streamer")
    if streamer is None:
        return False
    send_json = getattr(streamer, "send_json", None)
    if callable(send_json):
        await send_json(data)
        return True
    return False

