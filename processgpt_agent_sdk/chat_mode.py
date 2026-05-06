import asyncio
import json
from dataclasses import dataclass
from typing import Any, AsyncIterator, Awaitable, Callable, Dict, Optional, List
from uuid import uuid4

from a2a.helpers import get_message_text
from a2a.server.agent_execution import RequestContext
from a2a.server.events import EventQueue, Event
from a2a.types import Message
from google.protobuf.json_format import MessageToDict
from google.protobuf.message import Message as ProtobufMessage

from .database import insert_chat_message


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

    def __init__(self, req: ChatRequest):
        self.req = req
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
            "tenant_id": req.tenant_id,
            "user_uid": req.user_uid,
            "user_email": req.user_email,
            "user_name": req.user_name,
            "user_jwt": req.user_jwt,
            "conversation_id": req.conversation_id,
            "files": req.files,
            "file_count": req.file_count,
            "metadata": req.metadata,
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
        if isinstance(event, Message):
            if self._sent_message:
                raise RuntimeError("ChatEventQueue: multiple Message events are not allowed (Message-only stream).")
            self._sent_message = True
            self._response_text = get_message_text(event)
            await self._out_queue.put({"type": "message", "text": self._response_text})
            # 요구사항: 채팅 모드에서는 enqueue_event(Message)가 chats 테이블 기록 트리거
            if self._persist is not None:
                await self._persist(self._request, event, self._response_text)
            return

        raise RuntimeError(
            f"ChatEventQueue: unsupported event type in chat mode: {type(event).__name__}. "
            "Chat mode requires Message-only stream."
        )


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
        if item.get("type") == "done":
            yield _sse_format("done", "").encode("utf-8")
            return
        if item.get("type") == "error":
            yield _sse_format("error", json.dumps(item.get("data"), ensure_ascii=False)).encode("utf-8")
            return
        if item.get("type") == "message":
            yield _sse_format("message", item.get("text") or "").encode("utf-8")
            continue
        yield _sse_format("warning", json.dumps(item, ensure_ascii=False)).encode("utf-8")

