"""processgpt_agent_sdk/chat_owner.py — 진행 중인 턴을 돌리는 파드로 요청을 넘긴다.

## 왜 필요한가

진행 중인 턴은 그 턴을 만든 프로세스의 메모리에만 있다 — SSE 구독자(`ChatRunRegistry`),
실행 task(`InflightRegistry`), 방향 바꾸기 접수함(`SteeringInbox`). 셋 다 프로세스
로컬이고, 한 대화의 요청이 늘 같은 프로세스로 오면 그대로 정확하다(chat_registry 문서).

여러 파드가 대화를 나눠 받는 배포(서버형 에이전트 — deepagents 등)에서는 그 조건이
깨진다. 새로고침 뒤 재접속 · 중지 · 방향 바꾸기 · 같은 방의 새 질문이 턴을 돌리지 않는
파드에 떨어지면, 그 파드는 "돌고 있는 턴이 없다" 고 답하거나 받아 두고 아무도 꺼내지
않는다(조용한 유실).

예전에는 이벤트 · 중지 신호 · 턴 종료를 Redis 로 실어 날라 이 문제를 풀었다. 그러면
새 동작(방향 바꾸기 같은)이 생길 때마다 Redis 위에 규약을 하나씩 더 만들어야 한다.
여기서는 거꾸로 **요청 자체를 턴을 돌리는 파드로 넘긴다.** 요청이 그 파드에 닿으면
나머지는 지금의 프로세스 로컬 구현이 그대로 처리한다.

## Redis 에 두는 것

`chat:owner:{conversation_id}` = 턴을 돌리는 파드의 주소(`http://<pod-ip>:<port>`) 한 줄.
짧은 만료 시간을 두고, 턴이 도는 동안 갱신하고, 끝나면 지운다. 이벤트도 지시 문장도
Redis 를 지나가지 않는다 — 넘긴 요청은 평범한 HTTP 로 주인 파드에 간다.

## 흐름

- 새 턴(스트림 경로, 동작이 `steer` 가 아님): 주인 자리를 잡는다(SET NX). 내가 잡았거나
  이미 내 것이면 직접 처리하고, 다른 파드가 주인이면 그 파드로 넘긴다 — 같은 방에서 턴이
  돌고 있으면 그 파드가 대체/대기를 판단해야 하기 때문이다.
- 재접속 · 중지 · 방향 바꾸기: 주인을 찾아, 다른 파드면 넘기고 아니면 직접 처리한다.
- 주인 파드에 연결이 안 되면(죽었거나 사라졌으면) 그 값일 때만 기록을 지우고 직접 처리한다.
- 넘겨받은 요청(`X-Chat-Forwarded-By`)은 다시 넘기지 않는다 — 기록이 잠깐 어긋나도
  파드 사이를 맴돌지 않게.
- Redis 가 응답하지 않으면 직접 처리한다(넘겨주기가 없는 것과 같다).

## 수명

미들웨어가 잡은 자리는 짧은 예약이다. 핸들러가 실행을 시작하면 `hold_ownership()` 이
실행이 끝날 때까지 만료를 갱신하고, 끝나면 지운다. 실행을 시작하지 못하고 끝난 요청
(400·401 등)이면 예약을 바로 푼다. 한 대화에서 앞 턴과 새 턴이 잠깐 겹칠 수 있으므로
(새 턴이 앞 턴을 대체) 같은 프로세스 안의 보유 수를 세어, 마지막 보유가 끝날 때만 지운다.

## 설정

    CHAT_OWNER_FORWARDING   redis(켬) | memory(한 프로세스 안 — 시험용) | 비우면 끔
    CHAT_OWNER_REDIS_URL    없으면 REDIS_URL
    CHAT_OWNER_ADDRESS      다른 파드가 이 파드에 닿는 주소. 없으면 http://$POD_IP:$PORT
    CHAT_OWNER_TTL_SECONDS  기본 30
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import time
from dataclasses import dataclass, field
from typing import Any, Dict, Iterable, Optional, Tuple

from .steering import ACTION_STEER

logger = logging.getLogger(__name__)

FORWARDED_HEADER = "x-chat-forwarded-by"
CONVERSATION_HEADER = "x-conversation-id"
DEFAULT_TTL_SECONDS = 30.0
_KEY_PREFIX = "chat:owner:"
_MAX_BODY = 32 * 1024 * 1024
# 요청 · 응답 어느 쪽으로도 그대로 옮기면 안 되는 헤더(홉 단위).
_HOP_HEADERS = frozenset(
    {
        "host",
        "connection",
        "keep-alive",
        "proxy-authenticate",
        "proxy-authorization",
        "te",
        "trailer",
        "trailers",
        "transfer-encoding",
        "upgrade",
        "content-length",
    }
)


class OwnerDirectory:
    """대화 → 주인 파드 주소. 구현은 메모리(시험) · Redis(운영)."""

    async def claim(self, conversation_id: str, me: str, ttl: float) -> Tuple[str, bool]:
        """자리를 잡는다. (지금 주인, 이번에 새로 잡았는가) 를 돌려준다."""
        raise NotImplementedError

    async def lookup(self, conversation_id: str) -> Optional[str]:
        raise NotImplementedError

    async def renew(self, conversation_id: str, me: str, ttl: float) -> bool:
        """내가 주인일 때만 만료를 늘린다. 주인이 아니면 False."""
        raise NotImplementedError

    async def release(self, conversation_id: str, owner: str) -> None:
        """그 값일 때만 지운다 — 그사이 다른 파드가 잡았으면 건드리지 않는다."""
        raise NotImplementedError

    async def close(self) -> None:
        return None


class MemoryOwnerDirectory(OwnerDirectory):
    """한 프로세스 안의 기록. 시험과 단일 프로세스 개발용."""

    def __init__(self) -> None:
        self._rows: Dict[str, Tuple[str, float]] = {}

    def _get(self, conversation_id: str) -> Optional[str]:
        row = self._rows.get(conversation_id)
        if row is None:
            return None
        owner, expires = row
        if expires <= time.monotonic():
            self._rows.pop(conversation_id, None)
            return None
        return owner

    async def claim(self, conversation_id: str, me: str, ttl: float) -> Tuple[str, bool]:
        owner = self._get(conversation_id)
        if owner is None:
            self._rows[conversation_id] = (me, time.monotonic() + ttl)
            return me, True
        if owner == me:
            self._rows[conversation_id] = (me, time.monotonic() + ttl)
        return owner, False

    async def lookup(self, conversation_id: str) -> Optional[str]:
        return self._get(conversation_id)

    async def renew(self, conversation_id: str, me: str, ttl: float) -> bool:
        if self._get(conversation_id) != me:
            return False
        self._rows[conversation_id] = (me, time.monotonic() + ttl)
        return True

    async def release(self, conversation_id: str, owner: str) -> None:
        if self._get(conversation_id) == owner:
            self._rows.pop(conversation_id, None)


# 값이 같을 때만 만료를 늘리거나 지운다. GET 과 PEXPIRE/DEL 사이에 다른 파드가 자리를
# 잡을 수 있으므로 한 번에(스크립트로) 한다.
_RENEW_LUA = """
if redis.call('GET', KEYS[1]) == ARGV[1] then
  return redis.call('PEXPIRE', KEYS[1], ARGV[2])
end
return 0
"""
_RELEASE_LUA = """
if redis.call('GET', KEYS[1]) == ARGV[1] then
  return redis.call('DEL', KEYS[1])
end
return 0
"""


class RedisOwnerDirectory(OwnerDirectory):
    def __init__(self, url: str, *, prefix: str = _KEY_PREFIX) -> None:
        try:
            import redis.asyncio as aioredis  # type: ignore[import-not-found]
        except Exception as e:  # pragma: no cover
            raise RuntimeError(
                "넘겨주기에 Redis 를 쓰려면 `process-gpt-agent-sdk[redis]` 를 설치하세요."
            ) from e
        self._redis = aioredis.from_url(url, decode_responses=True)
        self._prefix = prefix
        self._renew = self._redis.register_script(_RENEW_LUA)
        self._release = self._redis.register_script(_RELEASE_LUA)

    def _key(self, conversation_id: str) -> str:
        return f"{self._prefix}{conversation_id}"

    async def claim(self, conversation_id: str, me: str, ttl: float) -> Tuple[str, bool]:
        key = self._key(conversation_id)
        ms = max(1, int(ttl * 1000))
        # SET NX 와 GET 사이에 기록이 만료될 수 있어 두 번까지 본다.
        for _ in range(2):
            if await self._redis.set(key, me, nx=True, px=ms):
                return me, True
            owner = await self._redis.get(key)
            if owner is None:
                continue
            if owner == me:
                await self._renew(keys=[key], args=[me, ms])
            return owner, False
        return me, False

    async def lookup(self, conversation_id: str) -> Optional[str]:
        return await self._redis.get(self._key(conversation_id))

    async def renew(self, conversation_id: str, me: str, ttl: float) -> bool:
        ms = max(1, int(ttl * 1000))
        return bool(await self._renew(keys=[self._key(conversation_id)], args=[me, ms]))

    async def release(self, conversation_id: str, owner: str) -> None:
        await self._release(keys=[self._key(conversation_id)], args=[owner])

    async def close(self) -> None:
        try:
            await self._redis.aclose()
        except Exception:  # pragma: no cover
            pass


@dataclass
class OwnerForwarding:
    directory: OwnerDirectory
    address: str
    ttl: float = DEFAULT_TTL_SECONDS
    # 이 프로세스가 그 대화를 몇 번 붙잡고 있는가(앞 턴과 새 턴이 잠깐 겹친다).
    _holds: Dict[str, int] = field(default_factory=dict)
    _beats: Dict[str, asyncio.Task] = field(default_factory=dict)
    _client: Any = None

    def client(self) -> Any:
        if self._client is None:
            import httpx

            # 연결은 빨리 포기한다(죽은 주인을 오래 기다리지 않게). 읽기는 끝없이 —
            # 한 턴의 SSE 는 몇 분씩 간다.
            self._client = httpx.AsyncClient(
                timeout=httpx.Timeout(connect=2.0, read=None, write=30.0, pool=5.0)
            )
        return self._client

    # -- 보유 ---------------------------------------------------------------

    def hold(self, conversation_id: str, task: "asyncio.Task[Any]") -> None:
        """실행 task 가 끝날 때까지 이 대화의 주인 자리를 지킨다."""
        if not conversation_id:
            return
        self._holds[conversation_id] = self._holds.get(conversation_id, 0) + 1
        if conversation_id not in self._beats:
            self._beats[conversation_id] = asyncio.create_task(self._beat(conversation_id))
        task.add_done_callback(lambda _t: asyncio.ensure_future(self._unhold(conversation_id)))

    def holding(self, conversation_id: str) -> bool:
        return self._holds.get(conversation_id, 0) > 0

    async def _beat(self, conversation_id: str) -> None:
        interval = max(0.5, self.ttl / 3)
        try:
            await self._claim_quietly(conversation_id)
            while True:
                await asyncio.sleep(interval)
                try:
                    if not await self.directory.renew(conversation_id, self.address, self.ttl):
                        owner, _ = await self.directory.claim(conversation_id, self.address, self.ttl)
                        if owner != self.address:
                            logger.warning(
                                "chat-owner: 턴이 도는 동안 주인이 바뀌었다 | conversation_id=%s now=%s",
                                conversation_id, owner,
                            )
                except Exception:
                    logger.warning("chat-owner: 주인 갱신 실패 | conversation_id=%s", conversation_id, exc_info=True)
        except asyncio.CancelledError:
            pass

    async def _claim_quietly(self, conversation_id: str) -> None:
        try:
            await self.directory.claim(conversation_id, self.address, self.ttl)
        except Exception:
            logger.warning("chat-owner: 주인 자리를 잡지 못했다 | conversation_id=%s", conversation_id, exc_info=True)

    async def _unhold(self, conversation_id: str) -> None:
        left = self._holds.get(conversation_id, 0) - 1
        if left > 0:
            self._holds[conversation_id] = left
            return
        self._holds.pop(conversation_id, None)
        beat = self._beats.pop(conversation_id, None)
        if beat is not None:
            beat.cancel()
        await self.release_quietly(conversation_id, self.address)

    async def release_quietly(self, conversation_id: str, owner: str) -> None:
        try:
            await self.directory.release(conversation_id, owner)
        except Exception:
            logger.warning("chat-owner: 주인 기록을 지우지 못했다 | conversation_id=%s", conversation_id, exc_info=True)

    async def close(self) -> None:
        for beat in self._beats.values():
            beat.cancel()
        self._beats.clear()
        self._holds.clear()
        if self._client is not None:
            await self._client.aclose()
            self._client = None
        await self.directory.close()


_forwarding: Optional[OwnerForwarding] = None
_env_checked = False


def configure_owner_forwarding(forwarding: Optional[OwnerForwarding]) -> None:
    """넘겨주기를 켜거나(인스턴스) 끈다(None). 시험과 직접 구성하는 서버가 쓴다."""
    global _forwarding, _env_checked
    _forwarding = forwarding
    _env_checked = True


def get_owner_forwarding() -> Optional[OwnerForwarding]:
    return _forwarding


def owner_address_from_env() -> str:
    explicit = (os.getenv("CHAT_OWNER_ADDRESS") or "").strip()
    if explicit:
        return explicit.rstrip("/")
    pod_ip = (os.getenv("POD_IP") or "").strip()
    port = (os.getenv("PORT") or "").strip()
    if pod_ip and port:
        return f"http://{pod_ip}:{port}"
    return ""


def owner_forwarding_from_env() -> Optional[OwnerForwarding]:
    """환경변수로 넘겨주기를 구성한다. 꺼져 있거나 값이 모자라면 None."""
    global _forwarding, _env_checked
    if _env_checked:
        return _forwarding
    _env_checked = True

    mode = (os.getenv("CHAT_OWNER_FORWARDING") or "").strip().lower()
    if mode in ("", "0", "false", "off", "no"):
        return None
    address = owner_address_from_env()
    if not address:
        logger.warning(
            "chat-owner: CHAT_OWNER_FORWARDING=%s 인데 이 파드의 주소가 없다 "
            "(CHAT_OWNER_ADDRESS 또는 POD_IP+PORT) — 넘겨주기를 끈다",
            mode,
        )
        return None
    try:
        ttl = float(os.getenv("CHAT_OWNER_TTL_SECONDS") or DEFAULT_TTL_SECONDS)
    except ValueError:
        ttl = DEFAULT_TTL_SECONDS

    if mode == "memory":
        directory: OwnerDirectory = MemoryOwnerDirectory()
    else:
        url = (os.getenv("CHAT_OWNER_REDIS_URL") or os.getenv("REDIS_URL") or "").strip()
        if not url:
            logger.warning("chat-owner: Redis 주소(CHAT_OWNER_REDIS_URL/REDIS_URL)가 없다 — 넘겨주기를 끈다")
            return None
        directory = RedisOwnerDirectory(url)

    _forwarding = OwnerForwarding(directory=directory, address=address, ttl=ttl)
    logger.info("🔀 채팅 넘겨주기 켬: address=%s ttl=%.0fs (%s)", address, ttl, mode)
    return _forwarding


def hold_ownership(conversation_id: str, task: "asyncio.Task[Any]") -> None:
    """핸들러가 실행을 시작한 직후 부른다. 넘겨주기가 꺼져 있으면 아무것도 안 한다."""
    fw = get_owner_forwarding()
    if fw is not None:
        fw.hold(conversation_id, task)


# -- 미들웨어 ----------------------------------------------------------------


def _header(scope: dict, name: bytes) -> str:
    for k, v in scope.get("headers", []):
        if k.lower() == name:
            return v.decode("latin-1")
    return ""


def _replay(body: bytes, original_receive):
    sent = False

    async def receive():
        nonlocal sent
        if not sent:
            sent = True
            return {"type": "http.request", "body": body, "more_body": False}
        return await original_receive()

    return receive


class ChatOwnerForwardMiddleware:
    """채팅 경로의 요청을 그 대화의 턴을 돌리는 파드로 넘긴다.

    순수 ASGI 로 만든다 — BaseHTTPMiddleware 는 SSE 와 궁합이 나쁘다(tenant_auth 참고).
    """

    def __init__(self, app, *, stream_paths: Iterable[str], other_paths: Iterable[str]) -> None:
        self.app = app
        self.stream_paths = frozenset(p for p in stream_paths if p)
        self.other_paths = frozenset(p for p in other_paths if p)

    async def __call__(self, scope, receive, send):
        path = scope.get("path")
        if (
            scope.get("type") != "http"
            or scope.get("method") != "POST"
            or (path not in self.stream_paths and path not in self.other_paths)
        ):
            await self.app(scope, receive, send)
            return
        fw = get_owner_forwarding()
        if fw is None or _header(scope, FORWARDED_HEADER.encode()):
            await self.app(scope, receive, send)
            return

        body = b""
        more = True
        while more:
            message = await receive()
            if message["type"] == "http.disconnect":
                return
            body += message.get("body", b"")
            if len(body) > _MAX_BODY:
                # 크기 판단은 하위(테넌트 가드 · 핸들러)에 맡긴다 — 여기서는 넘기지 않을 뿐.
                await self.app(scope, _replay(body, receive), send)
                return
            more = message.get("more_body", False)

        data: Dict[str, Any] = {}
        try:
            parsed = json.loads(body) if body else {}
            if isinstance(parsed, dict):
                data = parsed
        except ValueError:
            pass
        cid = (_header(scope, CONVERSATION_HEADER.encode()) or str(data.get("conversation_id") or "")).strip()
        if not cid:
            # 새 대화의 첫 요청 — 핸들러가 id 를 만든다. 아직 아무도 주인이 아니다.
            await self.app(scope, _replay(body, receive), send)
            return

        new_turn = path in self.stream_paths and str(data.get("action") or "").strip().lower() != ACTION_STEER
        try:
            if new_turn:
                owner, claimed = await fw.directory.claim(cid, fw.address, fw.ttl)
            else:
                owner, claimed = await fw.directory.lookup(cid), False
        except Exception:
            logger.warning("chat-owner: 주인 조회 실패 — 직접 처리한다 | conversation_id=%s", cid, exc_info=True)
            await self.app(scope, _replay(body, receive), send)
            return

        if owner and owner != fw.address:
            if await _forward(fw, owner, scope, body, receive, send):
                return
            # 주인에게 닿지 않는다 — 그 값일 때만 지우고 직접 처리한다.
            logger.warning("chat-owner: 주인 파드에 닿지 않는다 — 직접 처리 | conversation_id=%s owner=%s", cid, owner)
            await fw.release_quietly(cid, owner)
            if new_turn:
                try:
                    _owner, claimed = await fw.directory.claim(cid, fw.address, fw.ttl)
                except Exception:
                    claimed = False

        if not (new_turn and claimed):
            await self.app(scope, _replay(body, receive), send)
            return

        # 이번에 새로 잡은 예약 — 핸들러가 실행을 시작하지 못하고 끝나면(4xx·5xx) 바로 푼다.
        status: Dict[str, int] = {}

        async def watch(message):
            if message["type"] == "http.response.start":
                status["code"] = int(message.get("status") or 0)
            await send(message)

        try:
            await self.app(scope, _replay(body, receive), watch)
        finally:
            if status.get("code", 0) >= 300 and not fw.holding(cid):
                await fw.release_quietly(cid, fw.address)


async def _forward(fw: OwnerForwarding, owner: str, scope: dict, body: bytes, receive, send) -> bool:
    """요청을 주인 파드로 그대로 보내고 응답을 그대로 돌려준다. 연결이 안 되면 False."""
    import httpx

    url = owner.rstrip("/") + scope.get("path", "")
    query = scope.get("query_string") or b""
    if query:
        url += "?" + query.decode("latin-1")
    headers = [
        (k.decode("latin-1"), v.decode("latin-1"))
        for k, v in scope.get("headers", [])
        if k.decode("latin-1").lower() not in _HOP_HEADERS
    ]
    headers.append((FORWARDED_HEADER, fw.address))

    client = fw.client()
    try:
        upstream = await client.send(
            client.build_request("POST", url, headers=headers, content=body), stream=True
        )
    except (httpx.ConnectError, httpx.ConnectTimeout):
        return False

    # 브라우저가 끊으면 주인 쪽 연결도 닫는다(주인의 실행은 계속된다 — 재접속하면 이어 받는다).
    gone = asyncio.Event()

    async def watch_disconnect():
        while True:
            message = await receive()
            if message["type"] == "http.disconnect":
                gone.set()
                return

    watcher = asyncio.create_task(watch_disconnect())
    try:
        await send(
            {
                "type": "http.response.start",
                "status": upstream.status_code,
                "headers": [
                    (k.encode("latin-1"), v.encode("latin-1"))
                    for k, v in upstream.headers.multi_items()
                    if k.lower() not in _HOP_HEADERS
                ],
            }
        )
        async for chunk in upstream.aiter_raw():
            if gone.is_set():
                break
            await send({"type": "http.response.body", "body": chunk, "more_body": True})
        if not gone.is_set():
            await send({"type": "http.response.body", "body": b"", "more_body": False})
    except (httpx.ReadError, httpx.RemoteProtocolError):
        # 주인이 스트림 도중 사라졌다. 헤더는 이미 나갔으므로 여기서 닫는 수밖에 없다 —
        # 브라우저는 끊긴 스트림으로 보고 재접속한다.
        logger.warning("chat-owner: 넘긴 스트림이 도중에 끊겼다 | owner=%s", owner)
        try:
            await send({"type": "http.response.body", "body": b"", "more_body": False})
        except Exception:
            pass
    finally:
        watcher.cancel()
        await upstream.aclose()
    return True
