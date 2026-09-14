"""채팅 SSE 전송 계층 — 하트비트 · 재접속 레지스트리 · 중지."""

import asyncio
import json
import unittest

from starlette.applications import Starlette
from starlette.requests import Request

from processgpt_agent_sdk import chat_sse
from processgpt_agent_sdk.chat_registry import (
    ChatRunRegistry,
    InflightRegistry,
    RunRecordingQueue,
    set_inflight_registry,
    set_run_registry,
)
from processgpt_agent_sdk.chat_sse import (
    HEARTBEAT_CHUNK,
    add_route,
    format_sse_message,
    make_attach_handler,
    make_stop_handler,
    with_heartbeat,
)


# ---------------------------------------------------------------------------
# 하트비트
# ---------------------------------------------------------------------------

class TestHeartbeat(unittest.IsolatedAsyncioTestCase):
    async def test_청크가_있으면_그대로_통과시킨다(self):
        async def source():
            yield b"a"
            yield b"b"

        out = [c async for c in with_heartbeat(source(), interval=5.0)]
        self.assertEqual(out, [b"a", b"b"])

    async def test_유휴_구간에_하트비트를_끼워_넣는다(self):
        async def source():
            await asyncio.sleep(0.15)
            yield b"late"

        out = [c async for c in with_heartbeat(source(), interval=0.05)]
        self.assertIn(HEARTBEAT_CHUNK, out)
        # 하트비트가 끼어도 실제 청크는 유실되지 않는다.
        self.assertEqual(out[-1], b"late")

    async def test_하트비트가_상류_제너레이터를_닫지_않는다(self):
        """타임아웃마다 펜딩 task 를 취소하면 상류가 닫혀 릴레이가 멈춘다."""
        emitted = []

        async def source():
            for i in range(3):
                await asyncio.sleep(0.06)
                emitted.append(i)
                yield str(i).encode()

        out = [c async for c in with_heartbeat(source(), interval=0.02)]
        # 세 청크가 모두 살아 나와야 한다.
        self.assertEqual([c for c in out if c != HEARTBEAT_CHUNK], [b"0", b"1", b"2"])
        self.assertEqual(emitted, [0, 1, 2])

    def test_sse_포맷은_chat_mode_와_같다(self):
        self.assertEqual(
            format_sse_message({"type": "token", "content": "가"}),
            b'event: message\ndata: {"type": "token", "content": "\xea\xb0\x80"}\n\n',
        )


# ---------------------------------------------------------------------------
# 런 레지스트리
# ---------------------------------------------------------------------------

class TestRunRegistry(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.reg = ChatRunRegistry()

    async def test_재접속하면_누적_본문을_스냅샷으로_받는다(self):
        await self.reg.start_run("room-1")
        await self.reg.record("room-1", {"type": "token", "content": "안녕"})
        await self.reg.record("room-1", {"type": "token", "content": "하세요"})

        sub = await self.reg.subscribe("room-1")
        self.assertIsNotNone(sub)
        queue, snapshot = sub
        self.assertEqual(snapshot, "안녕하세요")

        # 구독 이후의 토큰은 실시간으로 들어온다.
        await self.reg.record("room-1", {"type": "token", "content": "!"})
        self.assertEqual(queue.get_nowait(), {"type": "token", "content": "!"})

    async def test_종료된_턴에는_재접속하지_않는다(self):
        await self.reg.start_run("room-1")
        await self.reg.record("room-1", {"type": "done", "content": "끝"})
        self.assertIsNone(await self.reg.subscribe("room-1"))
        self.assertFalse(await self.reg.is_active("room-1"))

    async def test_없는_방은_None(self):
        self.assertIsNone(await self.reg.subscribe("없는방"))

    async def test_새_턴은_이전_누적_본문을_지운다(self):
        await self.reg.start_run("room-1")
        await self.reg.record("room-1", {"type": "token", "content": "이전턴"})
        await self.reg.start_run("room-1")
        await self.reg.record("room-1", {"type": "token", "content": "새턴"})

        _queue, snapshot = await self.reg.subscribe("room-1")
        self.assertEqual(snapshot, "새턴")

    async def test_구독_해지하면_더_받지_않는다(self):
        await self.reg.start_run("room-1")
        queue, _ = await self.reg.subscribe("room-1")
        await self.reg.unsubscribe("room-1", queue)
        await self.reg.record("room-1", {"type": "token", "content": "x"})
        self.assertTrue(queue.empty())

    async def test_put_은_이벤트를_한_번만_기록한다(self):
        """asyncio.Queue.put() 이 내부에서 put_nowait() 를 부르므로, 두 메서드 모두에서
        기록하면 같은 토큰이 두 번 쌓여 재접속 스냅샷 본문이 두 배가 된다."""
        set_run_registry(self.reg)
        try:
            await self.reg.start_run("room-1")
            out_q = RunRecordingQueue("room-1")
            await out_q.put({"event": "message", "data": {"type": "token", "content": "가"}})

            _queue, snapshot = await self.reg.subscribe("room-1")
            self.assertEqual(snapshot, "가")
            # 큐 자체에도 한 건만 들어가 있어야 한다.
            self.assertEqual(out_q.qsize(), 1)
        finally:
            set_run_registry(ChatRunRegistry())

    async def test_큐를_가로채_이벤트를_기록한다(self):
        set_run_registry(self.reg)
        try:
            await self.reg.start_run("room-1")
            out_q = RunRecordingQueue("room-1")
            # mount_chat_sse 가 내보내는 것과 같은 모양
            await out_q.put({"event": "metadata", "data": {"conversation_id": "room-1"}})
            await out_q.put({"event": "message", "data": {"type": "token", "content": "기록됨"}})

            _queue, snapshot = await self.reg.subscribe("room-1")
            # metadata 는 본문이 아니므로 스냅샷에 들어가지 않는다.
            self.assertEqual(snapshot, "기록됨")
        finally:
            set_run_registry(ChatRunRegistry())


class TestInflightRegistry(unittest.IsolatedAsyncioTestCase):
    async def test_진행_중인_턴을_취소한다(self):
        reg = InflightRegistry()
        started = asyncio.Event()

        async def long_turn():
            started.set()
            await asyncio.sleep(60)

        task = asyncio.create_task(long_turn())
        reg.set_inflight("room-1", task)
        await started.wait()

        self.assertTrue(await reg.cancel("room-1"))
        self.assertTrue(task.cancelled())
        self.assertIsNone(reg.get_inflight("room-1"))

    async def test_취소할_턴이_없으면_False(self):
        self.assertFalse(await InflightRegistry().cancel("room-1"))

    async def test_끝난_턴은_자동으로_빠진다(self):
        reg = InflightRegistry()
        task = asyncio.create_task(asyncio.sleep(0))
        reg.set_inflight("room-1", task)
        await task
        await asyncio.sleep(0)
        self.assertIsNone(reg.get_inflight("room-1"))


# ---------------------------------------------------------------------------
# attach / stop 핸들러
# ---------------------------------------------------------------------------

def make_request(path: str, body: dict) -> Request:
    """핸들러에 직접 넘길 Starlette Request 를 만든다.

    TestClient 로 SSE 를 읽으면 스트림이 끝날 때까지 커넥션을 닫지 못해 테스트가
    매달린다(재접속 스트림은 done 이벤트 전까지 열려 있는 게 정상 동작이다).
    핸들러를 직접 부르면 body_iterator 를 원하는 만큼만 소비하고 끊을 수 있다.
    """
    payload = json.dumps(body, ensure_ascii=False).encode("utf-8")
    scope = {
        "type": "http",
        "http_version": "1.1",
        "method": "POST",
        "path": path,
        "raw_path": path.encode(),
        "query_string": b"",
        "root_path": "",
        "scheme": "http",
        "server": ("testserver", 80),
        "client": ("testclient", 123),
        "headers": [
            (b"content-type", b"application/json"),
            (b"content-length", str(len(payload)).encode()),
        ],
    }

    async def receive():
        return {"type": "http.request", "body": payload, "more_body": False}

    return Request(scope, receive=receive)


def json_body(response) -> dict:
    return json.loads(bytes(response.body).decode("utf-8"))


class _HandlerTestBase(unittest.IsolatedAsyncioTestCase):
    ROOM_TENANT = "acme"

    async def asyncSetUp(self):
        self.registry = ChatRunRegistry()
        self.inflight = InflightRegistry()
        set_run_registry(self.registry)
        set_inflight_registry(self.inflight)

        # DB 조회를 대신한다 — room-1 만 존재하는 방으로 둔다.
        self._orig_fetch = chat_sse.fetch_chat_room_tenant_id

        async def fake_fetch(conversation_id: str) -> str:
            return self.ROOM_TENANT if conversation_id == "room-1" else ""

        chat_sse.fetch_chat_room_tenant_id = fake_fetch

        # require_auth=False: 인증 자체는 test_tenant_auth 가 따로 본다.
        self.attach = make_attach_handler(require_auth=False, interval=30)
        self.stop = make_stop_handler(require_auth=False)

    async def asyncTearDown(self):
        chat_sse.fetch_chat_room_tenant_id = self._orig_fetch
        set_run_registry(ChatRunRegistry())
        set_inflight_registry(InflightRegistry())


class TestAttachHandler(_HandlerTestBase):
    async def test_conversation_id_가_없으면_400(self):
        resp = await self.attach(
            make_request("/chat/stream/attach", {"tenant_id": self.ROOM_TENANT})
        )
        self.assertEqual(resp.status_code, 400)

    async def test_활성_턴이_없으면_404가_아니라_200_active_false(self):
        resp = await self.attach(make_request(
            "/chat/stream/attach",
            {"conversation_id": "room-1", "tenant_id": self.ROOM_TENANT},
        ))
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(json_body(resp), {"active": False})

    async def test_다른_테넌트는_남의_스트림에_붙지_못한다(self):
        await self.registry.start_run("room-1")
        resp = await self.attach(make_request(
            "/chat/stream/attach",
            {"conversation_id": "room-1", "tenant_id": "globex"},
        ))
        self.assertEqual(json_body(resp), {"active": False})

    async def test_모르는_방은_거부한다(self):
        await self.registry.start_run("room-x")
        resp = await self.attach(make_request(
            "/chat/stream/attach",
            {"conversation_id": "room-x", "tenant_id": self.ROOM_TENANT},
        ))
        self.assertEqual(json_body(resp), {"active": False})

    async def test_재접속하면_스냅샷부터_받고_이후_토큰을_잇는다(self):
        await self.registry.start_run("room-1")
        await self.registry.record("room-1", {"type": "token", "content": "이미 쓴 본문"})

        resp = await self.attach(make_request(
            "/chat/stream/attach",
            {"conversation_id": "room-1", "tenant_id": self.ROOM_TENANT},
        ))
        self.assertEqual(resp.media_type, "text/event-stream")

        stream = resp.body_iterator
        try:
            first = await stream.__anext__()
            self.assertEqual(
                first, format_sse_message({"type": "snapshot", "content": "이미 쓴 본문"})
            )

            # 재접속 이후 도착한 토큰은 실시간으로 이어진다.
            await self.registry.record("room-1", {"type": "token", "content": "이어서"})
            nxt = await stream.__anext__()
            self.assertEqual(
                nxt, format_sse_message({"type": "token", "content": "이어서"})
            )
        finally:
            await stream.aclose()

        # 연결이 끊기면 구독자가 레지스트리에서 빠진다.
        await asyncio.sleep(0)
        run = self.registry._runs["room-1"]
        self.assertEqual(run.subscribers, [])


class TestStopHandler(_HandlerTestBase):
    async def test_conversation_id_가_없으면_400(self):
        resp = await self.stop(make_request("/chat/stop", {"tenant_id": self.ROOM_TENANT}))
        self.assertEqual(resp.status_code, 400)

    async def test_다른_테넌트는_남의_턴을_중지시키지_못한다(self):
        resp = await self.stop(make_request(
            "/chat/stop", {"conversation_id": "room-1", "tenant_id": "globex"}
        ))
        self.assertEqual(resp.status_code, 403)

    async def test_진행_중인_턴이_없으면_no_active_turn(self):
        resp = await self.stop(make_request(
            "/chat/stop", {"conversation_id": "room-1", "tenant_id": self.ROOM_TENANT}
        ))
        self.assertEqual(json_body(resp), {"stopped": False, "reason": "no_active_turn"})

    async def test_진행_중인_턴을_실제로_중지한다(self):
        await self.registry.start_run("room-1")
        started = asyncio.Event()

        async def long_turn():
            started.set()
            await asyncio.sleep(60)

        task = asyncio.create_task(long_turn())
        self.inflight.set_inflight("room-1", task)
        await started.wait()

        resp = await self.stop(make_request(
            "/chat/stop", {"conversation_id": "room-1", "tenant_id": self.ROOM_TENANT}
        ))
        self.assertEqual(json_body(resp), {"stopped": True})
        self.assertTrue(task.cancelled())
        # 중지된 턴에는 더 이상 재접속할 수 없다.
        self.assertIsNone(await self.registry.subscribe("room-1"))


class TestRouteRegistration(unittest.TestCase):
    def test_starlette_앱에_등록된다(self):
        app = Starlette()
        add_route(app, "/chat/stop", make_stop_handler(require_auth=False))
        self.assertIn("/chat/stop", [r.path for r in app.routes])

    def test_add_route_없는_앱은_명시적으로_거부한다(self):
        with self.assertRaises(TypeError):
            add_route(object(), "/chat/stop", make_stop_handler(require_auth=False))


if __name__ == "__main__":
    unittest.main()
