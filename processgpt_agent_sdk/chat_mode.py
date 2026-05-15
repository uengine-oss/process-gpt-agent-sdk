import asyncio
import json
from dataclasses import dataclass
from typing import Any, AsyncIterator, Awaitable, Callable, Dict, Optional, List, Union
from uuid import uuid4

from a2a.helpers import get_artifact_text, get_message_text
from a2a.server.agent_execution import RequestContext
from a2a.server.events import EventQueue, Event
from a2a.types import Message, Task, TaskArtifactUpdateEvent, TaskStatusUpdateEvent
from google.protobuf.json_format import MessageToDict
from google.protobuf.message import Message as ProtobufMessage

from .database import insert_chat_message, fetch_users_grouped
from .context_api import REQUEST_KIND_CHAT
from .utils import set_agent_model


# 채팅 모드에서 "최종 응답"으로 받아들이는 이벤트 타입.
# - TaskArtifactUpdateEvent: 통일 경로(프로세스 라이프사이클과 동일)
# - Message: 하위호환(Message-only 스트림)
FinalEvent = Union[TaskArtifactUpdateEvent, Message]


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
        # Executor/HTTP 채팅과 동일한 식별자 슬롯을 extras 최상위 + input_data 양쪽에 둔다.
        # - 최상위: context_api / 커스텀 코드가 바로 읽기 좋음
        # - input_data: [InputData] JSON 없는 SDK 채팅에서도 동일 키로 도구 보강(merge)이 동작하게 함
        _idem = {
            "tenant_id": req.tenant_id,
            "user_uid": req.user_uid,
            "user_email": req.user_email,
            "user_jwt": req.user_jwt,
            "user_name": req.user_name,
            "conversation_id": req.conversation_id,
            "metadata": dict(req.metadata or {}),
        }
        notify: List[str] = []
        if (req.user_email or "").strip():
            notify.append(str(req.user_email).strip())
        self._extras: Dict[str, Any] = {
            "request_kind": REQUEST_KIND_CHAT,
            **_idem,
            "input_data": _idem,
            "files": req.files,
            "file_count": req.file_count,
            "notify_user_emails": notify,
            "streamer": streamer,
        }

    async def prepare_context(self) -> None:
        """메타데이터에 participant_agent_ids가 있으면 에이전트 정보를 조회하여 extras에 추가합니다."""
        participant_ids = (self.req.metadata or {}).get("participant_agent_ids")
        if not participant_ids:
            return

        # 문자열이면 리스트로 변환 (콤마 구분)
        if isinstance(participant_ids, str):
            participant_ids = [pid.strip() for pid in participant_ids.split(",") if pid.strip()]

        # 자기 자신(process-gpt-agent)은 DB 조회 대상에서 제외
        participant_ids = [pid for pid in participant_ids if pid != "process-gpt-agent"]

        if not participant_ids:
            return

        agents, _users = await fetch_users_grouped(participant_ids)
        if agents:
            set_agent_model(agents[0])
        self._extras["agents"] = agents

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
    """채팅 모드 전용 EventQueue.

    A2A 표준 이벤트 타입을 라우팅 키로 사용합니다 (매직 metadata 없음).

    - Task: 라이프사이클 마커. SSE에 노출 X.
    - Message: 채팅용 청크. 토큰마다 1개씩 SSE message로 발행 (반복 허용).
    - TaskStatusUpdateEvent: 프로세스 모드 전용 신호. 채팅에선 silently ignore.
    - TaskArtifactUpdateEvent(last_chunk=True): 최종 결과. SSE done + chats 저장.
    - TaskArtifactUpdateEvent(last_chunk=False): 정의상 사용 안 함. silently ignore.

    필터링은 Executor 책임이고 SDK 는 받은 대로 라우팅합니다.
    """

    def __init__(
        self,
        out_queue: "asyncio.Queue[Dict[str, Any]]",
        *,
        request: ChatRequest,
        persist: Optional[Callable[[ChatRequest, FinalEvent, str], Awaitable[None]]] = None,
    ):
        super().__init__()
        self._out_queue = out_queue
        self._finalized = False
        self._response_text: str = ""
        self._request = request
        self._persist = persist

    async def enqueue_event(self, event: Event):
        # 라이프사이클 마커: SSE 노출 안 함
        if isinstance(event, Task):
            return

        # 프로세스 전용 이벤트: 채팅 큐는 무시
        if isinstance(event, TaskStatusUpdateEvent):
            return

        # 채팅 토큰 청크: 메시지 1개 = 토큰 1개 = SSE message 1개
        if isinstance(event, Message):
            text = get_message_text(event)
            self._response_text += text
            await self._out_queue.put(
                {"event": "message", "data": {"type": "token", "content": text}}
            )
            return

        # 최종 결과 아티팩트
        if isinstance(event, TaskArtifactUpdateEvent):
            is_final = bool(
                getattr(event, "last_chunk", None)
                or getattr(event, "lastChunk", None)
                or getattr(event, "final", None)
            )
            if not is_final:
                # 본 모델에선 토큰 스트리밍에 Message를 쓰므로 중간 artifact는 의미가 없음.
                # 만에 하나 들어와도 silent ignore (SDK는 dumb transport).
                return

            artifact = getattr(event, "artifact", None)
            final_text = get_artifact_text(artifact) if artifact is not None else ""
            await self._finalize(event, final_text)
            return

        # 그 외는 알 수 없는 이벤트 — 명시적 에러
        raise RuntimeError(
            f"ChatEventQueue: unsupported event type in chat mode: {type(event).__name__}."
        )

    async def _finalize(self, event: FinalEvent, response_text: str) -> None:
        if self._finalized:
            raise RuntimeError(
                "ChatEventQueue: multiple final artifacts are not allowed "
                "(emit exactly one TaskArtifactUpdateEvent(last_chunk=True) per chat session)."
            )
        self._finalized = True
        # 최종 텍스트는 누적된 토큰들이 아니라 artifact가 들고 온 풀 텍스트를 우선 사용
        # (Executor가 artifact에 누적 텍스트를 실어 보내는 게 표준 패턴)
        if response_text:
            self._response_text = response_text
        await self._out_queue.put(
            {
                "event": "message",
                "data": {"type": "done", "content": self._response_text},
            }
        )
        if self._persist is not None:
            await self._persist(self._request, event, self._response_text)


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


def _build_chat_message_payload(req: ChatRequest, event: FinalEvent, response_text: str) -> Dict[str, Any]:
    """chats.messages에 저장할 표준 payload를 구성합니다.

    Executor는 A2A 이벤트만 emit하면 되고, 저장 형식은 프레임워크가 결정합니다.
    """
    payload: Dict[str, Any] = {
        "name": "Process GPT Agent",
        "role": "assistant",
        "email": "agent:process-gpt-agent",
        "agentId": "process-gpt-agent",
        "profile": "/images/chat-icon.png",
        "userName": "Process GPT Agent",
        "content": response_text,
    }

    req_meta = req.metadata or {}
    agent_profile = req_meta.get("agent_profile")
    if isinstance(agent_profile, dict):
        payload.update(agent_profile)

    try:
        # Protobuf message일 경우 dict로 변환하여 metadata 추출
        event_dict = MessageToDict(event, preserving_proto_field_name=True)
        event_meta = event_dict.get("metadata")
        if isinstance(event_meta, dict):
            payload.update(event_meta)
    except Exception:
        # Protobuf message가 아니거나 변환 실패 시 속성 직접 접근
        event_meta = getattr(event, "metadata", None)
        if isinstance(event_meta, dict):
            payload.update(event_meta)

    return payload


async def persist_chat_to_db(req: ChatRequest, event: FinalEvent, response_text: str) -> None:
    """chats 테이블에 '단일 메시지 row'를 저장합니다."""
    payload = _build_chat_message_payload(req, event, response_text)
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

