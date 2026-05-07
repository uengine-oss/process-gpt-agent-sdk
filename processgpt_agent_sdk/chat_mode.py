import asyncio
import json
from dataclasses import dataclass
from typing import Any, AsyncIterator, Awaitable, Callable, Dict, Optional, List
from uuid import uuid4

from a2a.helpers import get_message_text
from a2a.server.agent_execution import RequestContext
from a2a.server.events import EventQueue, Event
from a2a.types import Message, TaskStatusUpdateEvent
from google.protobuf.json_format import MessageToDict
from google.protobuf.message import Message as ProtobufMessage

from .database import insert_chat_message
from .context_api import REQUEST_KIND_CHAT


@dataclass
class ChatRequest:
    """채팅 요청(사용자 제공 스키마에 맞춘 형태)."""

    message: str
    tenant_id: str = ""
    user_uid: str = ""
    user_email: str = ""
    user_name: str = ""
    user_jwt: str = ""
    conversation_id: Optional[str] = None
    file: Optional[Dict[str, Any]] = None
    files: List[Dict[str, Any]] = None
    file_count: int = 0
    stream: bool = True
    metadata: Dict[str, Any] = None

    def __post_init__(self):
        if self.files is None:
            self.files = []
        if self.metadata is None:
            self.metadata = {}


class ChatRequestContext(RequestContext):
    """채팅 모드용 경량 RequestContext."""

    def __init__(self, req: ChatRequest, *, streamer: Optional["ChatStreamer"] = None):
        self.req = req
        # A2A 표준 식별자: 없으면 내부적으로 생성
        self._context_id = str(req.conversation_id or uuid4().hex)
        self._task_id = self._context_id
        self._user_input = (req.message or "").strip()
        self._message = self._user_input
        self._current_task = None

        self._row: Dict[str, Any] = {
            "id": req.conversation_id,
            "proc_inst_id": req.conversation_id,
            "tenant_id": req.tenant_id,
            "user_id": req.user_uid,
        }
        self._extras: Dict[str, Any] = {
            # Framework-level contract: request kind + optional streamer
            "request_kind": REQUEST_KIND_CHAT,
            "tenant_id": req.tenant_id,
            "user_uid": req.user_uid,
            "user_email": req.user_email,
            "user_name": req.user_name,
            "user_jwt": req.user_jwt,
            "conversation_id": req.conversation_id,
            "files": req.files,
            "file_count": req.file_count,
            "metadata": req.metadata,
            # 채팅(SSE) 청크 스트리밍용(방법 A)
            "streamer": streamer,
        }

    def get_user_input(self) -> str:
        return self._user_input

    @property
    def message(self) -> str:
        return self._message

    @property
    def current_task(self):
        return self._current_task

    @current_task.setter
    def current_task(self, task):
        self._current_task = task

    def get_context_data(self) -> Dict[str, Any]:
        return {"row": self._row, "extras": self._extras}

    @property
    def metadata(self) -> Dict[str, Any]:
        """A2A 표준 metadata 접근자.

        샘플 Executor가 프레임워크 타입에 의존하지 않고 `context.metadata`만으로
        채팅/프로세스 분기 및 저장 payload 구성을 할 수 있게 합니다.
        """
        return dict(self.req.metadata or {}) | {
            "request_kind": REQUEST_KIND_CHAT,
            "conversation_id": self.req.conversation_id,
            "tenant_id": self.req.tenant_id,
            "user_uid": self.req.user_uid,
        }

    @property
    def task_id(self) -> str:
        return self._task_id

    @property
    def context_id(self) -> str:
        return self._context_id


class ChatEventQueue(EventQueue):
    """채팅 모드 전용 EventQueue (A2A v1.0 Message-only 강제)."""

    def __init__(
        self,
        out_queue: "asyncio.Queue[Dict[str, Any]]",
        *,
        request: ChatRequest,
        persist: Optional[Callable[[ChatRequest, Message, str], Awaitable[None]]] = None,
    ):
        super().__init__()
        self._out_queue = out_queue
        self._sent_message = False
        self._response_text: str = ""
        self._request = request
        self._persist = persist

    async def enqueue_event(self, event: Event):
        # 선택지 B: Executor는 A2A 이벤트만 emit.
        # - 중간 스트리밍(토큰/툴 시작/종료 등)은 TaskStatusUpdateEvent.metadata(Struct)에 JSON 객체를 담아 보냅니다.
        # - 최종 응답은 Message 1회만 허용합니다.
        if isinstance(event, TaskStatusUpdateEvent):
            md = getattr(event, "metadata", None)
            if isinstance(md, ProtobufMessage):
                data = MessageToDict(md, preserving_proto_field_name=True)
            elif md is None:
                data = {}
            else:
                # struct가 아닐 수도 있으니 best-effort
                data = dict(md) if isinstance(md, dict) else {"value": md}

            await self._out_queue.put({"event": "message", "data": data})
            return

        if isinstance(event, Message):
            if self._sent_message:
                raise RuntimeError("ChatEventQueue: multiple Message events are not allowed (Message-only stream).")
            self._sent_message = True
            self._response_text = get_message_text(event)
            # SSE contract: 최종 응답 + 세션 종료 신호는 하나의 done으로 보냅니다.
            await self._out_queue.put(
                {
                    "event": "message",
                    "data": {"type": "done", "content": self._response_text},
                }
            )
            # 요구사항: 채팅 모드에서는 enqueue_event(Message)가 chats 테이블 기록 트리거
            if self._persist is not None:
                await self._persist(self._request, event, self._response_text)
            return

        raise RuntimeError(
            f"ChatEventQueue: unsupported event type in chat mode: {type(event).__name__}. "
            "Chat mode requires Message-only stream."
        )


class ChatStreamer:
    """채팅(SSE) 청크를 enqueue_event와 분리해 흘려보내는 스트리머.

    - A2A Message-only 규칙을 지키기 위해, 토큰/중간 청크는 enqueue_event(Message)로 보내지 않습니다.
    - Executor는 `processgpt_agent_sdk.context_api.emit_chunk_text()` 같은 프레임워크 API를 통해 청크를 보냅니다.
    """

    def __init__(self, out_queue: "asyncio.Queue[Dict[str, Any]]"):
        self._out_queue = out_queue

    async def send_text(self, text: str) -> None:
        # SSE contract: event=message, token chunks are messages
        await self._out_queue.put({"event": "message", "data": {"type": "token", "content": text}})

    async def send_json(self, data: Any) -> None:
        # JSON chunks are also messages
        await self._out_queue.put({"event": "message", "data": data})


async def default_chat_message_builder(req: ChatRequest, message: Message, response_text: str) -> Any:
    """기본 messages payload (외부 서비스에서 자유롭게 교체 가능)."""
    # 방법 A: execute()에서 message.metadata["chat_payload"]를 구성해 넣으면 그걸 그대로 저장
    metadata = getattr(message, "metadata", None)
    if isinstance(metadata, ProtobufMessage):
        md = MessageToDict(metadata, preserving_proto_field_name=True)
        chat_payload = md.get("chat_payload")
        if chat_payload is not None:
            return chat_payload

    return {
        "role": "assistant",
        "content": response_text,
        "conversation_id": req.conversation_id,
        "tenant_id": req.tenant_id,
        "user": {
            "uid": req.user_uid,
            "email": req.user_email,
            "name": req.user_name,
        },
        "metadata": req.metadata,
    }


async def persist_chat_to_db(req: ChatRequest, message: Message, response_text: str) -> None:
    """chats 테이블에 '단일 메시지 row'를 저장합니다."""
    payload = await default_chat_message_builder(req, message, response_text)
    message_uuid = uuid4().hex
    chat_id = req.conversation_id or message_uuid
    await insert_chat_message(
        uuid=message_uuid,
        chat_id=chat_id,
        tenant_id=req.tenant_id or None,
        thread_id=req.conversation_id,
        messages=payload,
    )


def _sse_format(event: str, data: str) -> str:
    lines = (data or "").splitlines() or [""]
    data_block = "".join([f"data: {ln}\n" for ln in lines])
    return f"event: {event}\n{data_block}\n"


async def drain_sse_queue(q: "asyncio.Queue[Dict[str, Any]]") -> AsyncIterator[bytes]:
    while True:
        item = await q.get()
        # New contract: only 'metadata' or 'message', with JSON object data
        if item.get("event") in ("metadata", "message"):
            yield _sse_format(item["event"], json.dumps(item.get("data") or {}, ensure_ascii=False)).encode("utf-8")
            # done/error are expressed as message data types
            if item.get("event") == "message" and isinstance(item.get("data"), dict) and item["data"].get("type") in ("done", "error"):
                return
            continue

        # Backward-compat: accept legacy queue items (type-based)
        if item.get("type") == "done":
            yield _sse_format("message", json.dumps({"type": "done"}, ensure_ascii=False)).encode("utf-8")
            return
        if item.get("type") == "error":
            yield _sse_format("message", json.dumps({"type": "error", **(item.get("data") or {})}, ensure_ascii=False)).encode("utf-8")
            return
        if item.get("type") == "chunk":
            yield _sse_format("message", json.dumps({"type": "token", "content": item.get("text") or ""}, ensure_ascii=False)).encode("utf-8")
            continue
        if item.get("type") == "chunk_json":
            yield _sse_format("message", json.dumps(item.get("data") or {}, ensure_ascii=False)).encode("utf-8")
            continue
        if item.get("type") == "message":
            yield _sse_format("message", json.dumps({"type": "final", "content": item.get("text") or ""}, ensure_ascii=False)).encode("utf-8")
            continue

        yield _sse_format("message", json.dumps({"type": "warning", "data": item}, ensure_ascii=False)).encode("utf-8")

