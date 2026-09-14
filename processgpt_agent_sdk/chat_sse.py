"""processgpt_agent_sdk/chat_sse.py — 채팅 SSE 전송 계층.

하트비트, 재접속(`/chat/stream/attach`), 중지(`/chat/stop`) 를 담는다.
세 가지 모두 codex 와 deepagents 가 각자 같은 것을 구현하고 있던 부분이다.

## 하트비트

한 턴은 LLM 이 오래 생각하거나 도구가 파일을 쓰는 동안 수 분씩 아무 이벤트도
내보내지 않는다. 그 사이 SSE 커넥션은 완전히 유휴 상태가 되는데, 중간 프록시
(Cloudflare 등)는 유휴 커넥션을 100초 안팎에서 끊는다. 그러면 브라우저는
스트림을 잃고 백엔드는 계속 도는데 화면은 "생각 중…" 에서 영원히 멈춘다.

SSE 명세상 `:` 로 시작하는 줄은 주석이라 클라이언트 파서(`data:` 만 처리)는
무시하고, 프록시 입장에서는 커넥션이 살아 있는 것으로 보인다.

## 프론트 계약

두 저장소가 이미 공유하던 계약을 그대로 옮겼다.

  - 활성 스트림이 없으면 404 가 아니라 `200 {"active": false}` 다. 404 로 표현하면
    매 메시지 전송마다(첫 attach 시도는 항상 "아직 스트림 없음") 브라우저 네트워크
    탭에 실패 요청으로 남아 노이즈가 된다.
  - 재접속하면 누적 본문을 `snapshot` 1건으로 먼저 받고 그 뒤부터 실시간이다.
    프론트의 attach 핸들러가 첫 스냅샷을 '치환' 으로 다루므로 화면이 이어붙지 않는다.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
from contextlib import suppress
from typing import Any, AsyncIterator, Optional, Tuple

from .chat_registry import (
    DONE_TYPES,
    get_inflight_registry,
    get_run_registry,
)
from .database import fetch_chat_room_tenant_id
from .tenant_auth import TenantAuthError, authorize_tenant

logger = logging.getLogger(__name__)

# 흔한 프록시 유휴 한도(30~100초)보다 넉넉히 짧게 잡는다.
DEFAULT_HEARTBEAT_SECONDS = 15.0

# 클라이언트는 무시하고 프록시의 유휴 타이머만 리셋하는 SSE 주석 한 줄.
HEARTBEAT_CHUNK = b": keep-alive\n\n"


def heartbeat_interval() -> float:
    try:
        return float(os.environ.get("SSE_HEARTBEAT_SECONDS", DEFAULT_HEARTBEAT_SECONDS))
    except (TypeError, ValueError):
        return DEFAULT_HEARTBEAT_SECONDS


async def with_heartbeat(
    source: AsyncIterator[Any], interval: Optional[float] = None
) -> AsyncIterator[Any]:
    """원본 이터레이터를 감싸, `interval` 초 동안 청크가 없으면 하트비트를 내보낸다.

    펜딩 `__anext__` task 를 타임아웃마다 취소하면 안 된다 — 취소가 상류 제너레이터를
    닫아 릴레이 자체가 멈춘다. 그래서 task 는 살려 둔 채 대기만 다시 건다.
    """
    if interval is None:
        interval = heartbeat_interval()

    iterator = source.__aiter__()
    pending: Optional[asyncio.Future] = None
    try:
        while True:
            if pending is None:
                pending = asyncio.ensure_future(iterator.__anext__())
            done, _ = await asyncio.wait({pending}, timeout=interval)
            if not done:
                # 아직 다음 청크가 없다 → 커넥션만 살려 둔다.
                yield HEARTBEAT_CHUNK
                continue
            task, pending = pending, None
            try:
                chunk = task.result()
            except StopAsyncIteration:
                return
            yield chunk
    finally:
        if pending is not None and not pending.done():
            pending.cancel()
            with suppress(asyncio.CancelledError, Exception):
                await pending
        # 상류도 닫는다. 이걸 빠뜨리면 감싼 쪽만 닫히고 원본 제너레이터의 finally 가
        # 돌지 않아, 재접속 스트림이 끊겨도 구독자가 레지스트리에 남는다(GC 시점까지).
        aclose = getattr(iterator, "aclose", None)
        if aclose is not None:
            with suppress(Exception):
                await aclose()


def format_sse_message(payload: dict) -> bytes:
    """`chat_mode` 의 SSE 포맷(`event: message`)과 동일한 형식으로 직렬화한다."""
    data = json.dumps(payload or {}, ensure_ascii=False)
    lines = data.splitlines() or [""]
    data_block = "".join(f"data: {ln}\n" for ln in lines)
    return f"event: message\n{data_block}\n".encode("utf-8")


def apply_heartbeat(response: Any, interval: Optional[float] = None) -> Any:
    """StreamingResponse 의 body_iterator 에 하트비트를 끼워 넣는다."""
    from starlette.responses import StreamingResponse

    if isinstance(response, StreamingResponse):
        response.body_iterator = with_heartbeat(response.body_iterator, interval)
        response.headers.setdefault("Cache-Control", "no-cache")
        # nginx 계열이 응답을 버퍼링하면 하트비트도 같이 갇힌다.
        response.headers.setdefault("X-Accel-Buffering", "no")
    return response


# ---------------------------------------------------------------------------
# 요청 파싱 + 소유권 확인
# ---------------------------------------------------------------------------

async def _body(request: Any) -> dict:
    try:
        body = await request.json()
    except Exception:
        return {}
    return body if isinstance(body, dict) else {}


async def _resolve_tenant(request: Any, body: dict, *, require_auth: bool) -> Tuple[str, Any]:
    """요청자의 테넌트를 정한다. 실패하면 (빈 문자열, 오류 응답).

    `require_auth` 가 참이면 JWT 로 검증된 값만 쓴다 — 본문 값만 믿으면 방 ID 와
    테넌트 ID 만 아는 외부인이 남의 스트림에 붙거나 남의 턴을 중지시킬 수 있다.
    거짓이면(인증 미설정 서버) 기존 동작대로 본문 값을 쓴다.
    """
    from starlette.responses import JSONResponse

    if not require_auth:
        return str(body.get("tenant_id") or "").strip(), None
    try:
        await authorize_tenant(request)
    except TenantAuthError as e:
        return "", JSONResponse({"error": e.message}, status_code=e.status_code)
    return str(getattr(request.state, "tenant_id", "") or ""), None


async def _owns_room(tenant_id: str, conversation_id: str) -> bool:
    """방 소유 테넌트와 요청자의 테넌트가 같은지 확인한다(fail-closed)."""
    if not tenant_id:
        return False
    room_tenant_id = await fetch_chat_room_tenant_id(conversation_id)
    return bool(room_tenant_id) and room_tenant_id == tenant_id


# ---------------------------------------------------------------------------
# 핸들러
# ---------------------------------------------------------------------------

def make_attach_handler(*, require_auth: bool = True, interval: Optional[float] = None):
    """진행 중인 턴에 재접속하는 핸들러를 만든다."""
    from starlette.responses import JSONResponse, StreamingResponse

    # 활성 스트림 없음은 클라이언트 관점에서 정상 상태(재접속할 대상이 없을 뿐)다.
    def _inactive() -> Any:
        return JSONResponse({"active": False}, status_code=200)

    async def attach_handler(request: Any) -> Any:
        body = await _body(request)
        conversation_id = str(body.get("conversation_id") or "").strip()
        if not conversation_id:
            return JSONResponse(
                {"active": False, "error": "conversation_id required"}, status_code=400
            )

        tenant_id, error = await _resolve_tenant(request, body, require_auth=require_auth)
        if error is not None:
            return error
        if not await _owns_room(tenant_id, conversation_id):
            return _inactive()

        subscription = await get_run_registry().subscribe(conversation_id)
        if subscription is None:
            return _inactive()
        queue, snapshot_text = subscription

        async def _stream() -> AsyncIterator[bytes]:
            try:
                yield format_sse_message({"type": "snapshot", "content": snapshot_text})
                while True:
                    item = await queue.get()
                    yield format_sse_message(item)
                    if isinstance(item, dict) and item.get("type") in DONE_TYPES:
                        return
            finally:
                # 클라이언트 연결이 끊기면(생성기 aclose) 구독자를 레지스트리에서 뺀다.
                await get_run_registry().unsubscribe(conversation_id, queue)

        # 재접속한 스트림도 원본과 똑같이 수 분씩 유휴 상태가 될 수 있다.
        return StreamingResponse(
            with_heartbeat(_stream(), interval),
            media_type="text/event-stream",
            headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
        )

    return attach_handler


def make_stop_handler(*, require_auth: bool = True):
    """진행 중인 턴을 서버에서 실제로 중지하는 핸들러를 만든다.

    프론트의 중지 버튼은 지금까지 자기 쪽 `fetch` 만 abort 했다. 그러면 화면에서는
    스트림이 멈추지만 서버의 실행은 계속 돌아 토큰과 도구 호출을 그대로 소비한다.
    """
    from starlette.responses import JSONResponse

    async def stop_handler(request: Any) -> Any:
        body = await _body(request)
        conversation_id = str(body.get("conversation_id") or "").strip()
        if not conversation_id:
            return JSONResponse(
                {"stopped": False, "error": "conversation_id required"}, status_code=400
            )

        tenant_id, error = await _resolve_tenant(request, body, require_auth=require_auth)
        if error is not None:
            return error
        if not await _owns_room(tenant_id, conversation_id):
            return JSONResponse({"stopped": False, "reason": "forbidden"}, status_code=403)

        cancelled = await get_inflight_registry().cancel(conversation_id)
        await get_run_registry().mark_done(conversation_id)
        if not cancelled:
            # 이미 끝났거나 다른 파드가 들고 있는 턴이다.
            logger.info("chat_stop: 진행 중인 턴 없음 | conversation_id=%s", conversation_id)
            return JSONResponse({"stopped": False, "reason": "no_active_turn"})

        logger.info("chat_stop: 사용자 요청으로 턴 중지 | conversation_id=%s", conversation_id)
        return JSONResponse({"stopped": True})

    return stop_handler


# ---------------------------------------------------------------------------
# 라우트 등록
# ---------------------------------------------------------------------------

def add_route(app: Any, path: str, handler: Any, methods: Optional[list] = None) -> None:
    """Starlette 라우트로 등록한다(FastAPI 앱 포함).

    FastAPI 의 `add_api_route()` 에 넘기면 핸들러 시그니처가
    `async def handler(request)` (타입 주석 없음)라 FastAPI 가 `request` 를 필수
    쿼리 파라미터로 해석해 모든 요청이 422 로 떨어진다. `app.router` 는 FastAPI
    에서도 Starlette `Router` 를 상속하므로 양쪽 모두 여기로 등록하면 된다.
    """
    methods = methods or ["POST"]
    router = getattr(app, "router", None)
    if router is not None and hasattr(router, "add_route"):
        router.add_route(path, handler, methods=methods)
        return
    if hasattr(app, "add_route"):
        app.add_route(path, handler, methods=methods)
        return
    raise TypeError(
        "Unsupported app type: expected FastAPI or Starlette-like app with add_route."
    )
