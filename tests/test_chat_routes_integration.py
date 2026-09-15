"""mount_chat_routes() 통합 — 스트림 · 재접속 · 중지가 한 벌로 동작하는지 본다.

단위 테스트가 각 조각을 따로 보는 반면, 여기서는 실제 마운트된 라우트에 요청을
넣어 "진행 중인 턴에 다른 요청이 붙거나 그 턴을 중지시키는" 흐름을 확인한다.
"""

import asyncio
import json
import unittest

from a2a.helpers import new_text_artifact_update_event
from a2a.server.agent_execution import AgentExecutor, RequestContext
from a2a.server.events import EventQueue
from starlette.applications import Starlette

from processgpt_agent_sdk import chat_sse
from processgpt_agent_sdk.chat_registry import (
    ChatRunRegistry,
    InflightRegistry,
    set_inflight_registry,
    set_run_registry,
)
from processgpt_agent_sdk.context_api import get_streamer
from processgpt_agent_sdk.processgpt_agent_framework import ProcessGPTAgentServer

from tests.test_chat_sse import json_body, make_request


ROOM = "room-1"
TENANT = "acme"


class _ScriptedExecutor(AgentExecutor):
    """토큰 몇 개를 흘리고 신호를 기다렸다가 끝나는 Executor."""

    def __init__(self):
        self.streamed = asyncio.Event()
        self.release = asyncio.Event()
        self.cancelled = False

    async def execute(self, context: RequestContext, event_queue: EventQueue) -> None:
        streamer = get_streamer(context)
        await streamer.send_text("첫 ")
        await streamer.send_text("토큰")
        self.streamed.set()
        try:
            await self.release.wait()
        except asyncio.CancelledError:
            self.cancelled = True
            raise
        await event_queue.enqueue_event(
            new_text_artifact_update_event(
                task_id=str(context.task_id),
                context_id=str(context.context_id),
                text="첫 토큰 그리고 끝",
                last_chunk=True,
            )
        )

    async def cancel(self, context: RequestContext, event_queue: EventQueue) -> None:
        self.cancelled = True


class TestMountChatRoutes(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.registry = ChatRunRegistry()
        self.inflight = InflightRegistry()
        set_run_registry(self.registry)
        set_inflight_registry(self.inflight)

        self._orig_fetch = chat_sse.fetch_chat_room_tenant_id

        async def fake_fetch(conversation_id: str) -> str:
            return TENANT if conversation_id == ROOM else ""

        chat_sse.fetch_chat_room_tenant_id = fake_fetch

        self.executor = _ScriptedExecutor()
        # tenant_auth=False: 인증은 test_tenant_auth 가 따로 본다.
        self.server = ProcessGPTAgentServer(
            agent_executor=self.executor, agent_type="test", tenant_auth=False,
        )
        self.app = Starlette()
        self.server.mount_chat_routes(self.app)

        # chats 저장은 DB 를 타므로 막는다 — 여기서 보는 것은 전송 계층이다.
        self.stream_handler = self._find("/chat/stream")
        self.attach_handler = self._find("/chat/stream/attach")
        self.stop_handler = self._find("/chat/stop")

    def _find(self, path):
        for route in self.app.routes:
            if getattr(route, "path", None) == path:
                return route.endpoint
        raise AssertionError(f"라우트를 찾지 못했습니다: {path}")

    async def asyncTearDown(self):
        self.executor.release.set()
        chat_sse.fetch_chat_room_tenant_id = self._orig_fetch
        set_run_registry(ChatRunRegistry())
        set_inflight_registry(InflightRegistry())

    def test_세_라우트가_모두_등록된다(self):
        paths = {getattr(r, "path", None) for r in self.app.routes}
        self.assertEqual(
            {"/chat/stream", "/chat/stream/attach", "/chat/stop"} & paths,
            {"/chat/stream", "/chat/stream/attach", "/chat/stop"},
        )

    async def _start_turn(self):
        """채팅 스트림을 시작하고, 토큰 두 개가 나갈 때까지 기다린다."""
        request = make_request("/chat/stream", {
            "message": "안녕", "conversation_id": ROOM, "tenant_id": TENANT,
        })
        response = await self.stream_handler(request)
        await asyncio.wait_for(self.executor.streamed.wait(), timeout=5)
        return response

    async def test_진행_중인_턴에_재접속하면_지금까지의_본문을_받는다(self):
        await self._start_turn()

        response = await self.attach_handler(make_request(
            "/chat/stream/attach", {"conversation_id": ROOM, "tenant_id": TENANT},
        ))
        self.assertEqual(response.media_type, "text/event-stream")

        stream = response.body_iterator
        try:
            first = await asyncio.wait_for(stream.__anext__(), timeout=5)
            payload = json.loads(first.decode("utf-8").split("data: ", 1)[1])
            # 원래 클라이언트가 이미 받은 토큰이 그대로 스냅샷으로 재생된다.
            self.assertEqual(payload, {"type": "snapshot", "content": "첫 토큰"})
        finally:
            await stream.aclose()

    async def test_중지하면_실행이_실제로_취소된다(self):
        await self._start_turn()

        response = await self.stop_handler(make_request(
            "/chat/stop", {"conversation_id": ROOM, "tenant_id": TENANT},
        ))
        self.assertEqual(json_body(response), {"stopped": True})
        self.assertTrue(self.executor.cancelled)
        # 중지된 턴에는 더 이상 재접속할 수 없다.
        self.assertIsNone(await self.registry.subscribe(ROOM))

    async def test_같은_방에_새_턴이_오면_이전_턴을_끊는다(self):
        await self._start_turn()
        first_task = self.inflight.get_inflight(ROOM)
        self.assertIsNotNone(first_task)

        self.executor.streamed.clear()
        await self._start_turn()

        self.assertTrue(first_task.cancelled())
        self.assertIsNot(self.inflight.get_inflight(ROOM), first_task)

    async def test_supersede_를_막으면_이전_턴이_살아_있다(self):
        """HITL 응답처럼 이전 턴을 이어가야 하는 서버는 supersede 를 재정의한다."""

        class KeepPrevious(InflightRegistry):
            async def supersede(self, conversation_id):
                return False   # 새 턴이 와도 이전 턴을 끊지 않는다

        keeper = KeepPrevious()
        set_inflight_registry(keeper)
        self.inflight = keeper

        await self._start_turn()
        first_task = keeper.get_inflight(ROOM)

        self.executor.streamed.clear()
        await self._start_turn()

        self.assertFalse(first_task.cancelled())
        # 명시적 중지는 그대로 동작해야 한다.
        self.assertTrue(await keeper.cancel(ROOM))
        first_task.cancel()

    async def test_스트림_응답에_하트비트_헤더가_붙는다(self):
        response = await self._start_turn()
        self.assertEqual(response.headers.get("cache-control"), "no-cache")
        self.assertEqual(response.headers.get("x-accel-buffering"), "no")

    async def test_tenant_auth_는_기본값이_켜짐이다(self):
        server = ProcessGPTAgentServer(agent_executor=self.executor, agent_type="test")
        self.assertTrue(server.tenant_auth)


if __name__ == "__main__":
    unittest.main()


class TestShouldSupersedeHook(unittest.IsolatedAsyncioTestCase):
    """Executor 가 `should_supersede()` 로 이전 턴을 끊을지 이어갈지 정한다.

    HITL 응답이 이 훅이 필요한 이유다 — 이전 턴을 취소하면 interrupt 체크포인트가
    남지 않아 재개가 불가능해진다. 예전에는 이 판단을 하려면 레지스트리 전체를
    갈아끼워야 했다(deepagents `core/chat/sdk_registry.py`).
    """

    async def asyncSetUp(self):
        self.registry = ChatRunRegistry()
        self.inflight = InflightRegistry()
        set_run_registry(self.registry)
        set_inflight_registry(self.inflight)

        self._orig_fetch = chat_sse.fetch_chat_room_tenant_id

        async def fake_fetch(conversation_id: str) -> str:
            return TENANT if conversation_id == ROOM else ""

        chat_sse.fetch_chat_room_tenant_id = fake_fetch

    async def asyncTearDown(self):
        chat_sse.fetch_chat_room_tenant_id = self._orig_fetch
        set_run_registry(ChatRunRegistry())
        set_inflight_registry(InflightRegistry())

    def _mount(self, executor):
        server = ProcessGPTAgentServer(
            agent_executor=executor, agent_type="test", tenant_auth=False,
        )
        app = Starlette()
        server.mount_chat_routes(app)
        for route in app.routes:
            if getattr(route, "path", None) == "/chat/stream":
                return route.endpoint
        raise AssertionError("스트림 라우트를 찾지 못했습니다")

    async def _start(self, handler, executor):
        request = make_request("/chat/stream", {
            "message": "안녕", "conversation_id": ROOM, "tenant_id": TENANT,
        })
        response = await handler(request)
        await asyncio.wait_for(executor.streamed.wait(), timeout=5)
        return response

    async def test_훅이_없으면_이전_턴을_끊는다(self):
        """기존 동작 — 훅을 구현하지 않은 Executor 는 달라지는 것이 없다."""
        executor = _ScriptedExecutor()
        handler = self._mount(executor)
        try:
            await self._start(handler, executor)
            first = self.inflight.get_inflight(ROOM)

            executor.streamed.clear()
            await self._start(handler, executor)
            self.assertTrue(first.cancelled())
        finally:
            executor.release.set()

    async def test_훅이_False_면_이전_턴을_기다린다(self):
        """HITL 경로 — 이전 턴은 살아 있고, 새 턴은 그것이 끝난 뒤 시작한다."""

        class KeepPrevious(_ScriptedExecutor):
            def __init__(self):
                super().__init__()
                self.asked_with = []

            def should_supersede(self, context):
                self.asked_with.append(context.context_id)
                return False

        executor = KeepPrevious()
        handler = self._mount(executor)
        try:
            await self._start(handler, executor)
            first = self.inflight.get_inflight(ROOM)

            executor.streamed.clear()
            second = asyncio.create_task(self._start(handler, executor))
            # 이전 턴이 살아 있는 동안 새 턴은 아직 Executor 에 닿지 않는다.
            await asyncio.sleep(0.05)
            self.assertFalse(first.cancelled())
            self.assertFalse(executor.streamed.is_set())

            # 이전 턴이 끝나면 새 턴이 진행된다.
            executor.release.set()
            await asyncio.wait_for(second, timeout=5)
            self.assertEqual(executor.asked_with, [ROOM, ROOM])
        finally:
            executor.release.set()

    async def test_비동기_훅도_받는다(self):
        class AsyncHook(_ScriptedExecutor):
            async def should_supersede(self, context):
                return True

        executor = AsyncHook()
        handler = self._mount(executor)
        try:
            await self._start(handler, executor)
            first = self.inflight.get_inflight(ROOM)
            executor.streamed.clear()
            await self._start(handler, executor)
            self.assertTrue(first.cancelled())
        finally:
            executor.release.set()

    async def test_훅이_예외를_내면_기존_동작으로_진행한다(self):
        class Broken(_ScriptedExecutor):
            def should_supersede(self, context):
                raise RuntimeError("판단 실패")

        executor = Broken()
        handler = self._mount(executor)
        try:
            await self._start(handler, executor)
            first = self.inflight.get_inflight(ROOM)
            executor.streamed.clear()
            await self._start(handler, executor)
            # 판단을 못 했다고 요청을 실패시키지 않는다 — 훅이 없던 시절과 같다.
            self.assertTrue(first.cancelled())
        finally:
            executor.release.set()


class TestHealthRoute(unittest.IsolatedAsyncioTestCase):
    """리버스 프록시가 TTL 회수 여부를 판정하는 근거(`busy`)를 파드가 노출한다."""

    async def asyncSetUp(self):
        self.registry = ChatRunRegistry()
        self.inflight = InflightRegistry()
        set_run_registry(self.registry)
        set_inflight_registry(self.inflight)

        self._orig_fetch = chat_sse.fetch_chat_room_tenant_id

        async def fake_fetch(conversation_id: str) -> str:
            return TENANT if conversation_id == ROOM else ""

        chat_sse.fetch_chat_room_tenant_id = fake_fetch

        self.executor = _ScriptedExecutor()
        self.server = ProcessGPTAgentServer(
            agent_executor=self.executor, agent_type="test", tenant_auth=False,
        )
        self.app = Starlette()
        self.server.mount_chat_routes(
            self.app,
            health_path="/health",
            health_extra=lambda: {"agent_type": "test"},
        )
        self.stream_handler = self._find("/chat/stream")
        self.health_handler = self._find("/health")

    def _find(self, path):
        for route in self.app.routes:
            if getattr(route, "path", None) == path:
                return route.endpoint
        raise AssertionError(f"라우트를 찾지 못했습니다: {path}")

    async def asyncTearDown(self):
        self.executor.release.set()
        chat_sse.fetch_chat_room_tenant_id = self._orig_fetch
        set_run_registry(ChatRunRegistry())
        set_inflight_registry(InflightRegistry())

    async def test_health_path_를_주지_않으면_붙지_않는다(self):
        app = Starlette()
        ProcessGPTAgentServer(
            agent_executor=_ScriptedExecutor(), agent_type="test", tenant_auth=False,
        ).mount_chat_routes(app)
        self.assertNotIn("/health", {getattr(r, "path", None) for r in app.routes})

    async def test_턴이_없으면_busy_는_거짓이다(self):
        body = json_body(await self.health_handler(make_request("/health", {})))
        self.assertEqual(body["status"], "ok")
        self.assertFalse(body["busy"])
        self.assertEqual(body["active_turns"], 0)
        # health_extra 가 얹은 필드도 함께 나온다.
        self.assertEqual(body["agent_type"], "test")

    async def test_턴이_도는_동안_busy_가_참이다(self):
        await self.stream_handler(make_request("/chat/stream", {
            "message": "안녕", "conversation_id": ROOM, "tenant_id": TENANT,
        }))
        await asyncio.wait_for(self.executor.streamed.wait(), timeout=5)

        body = json_body(await self.health_handler(make_request("/health", {})))
        self.assertTrue(body["busy"])
        self.assertEqual(body["active_turns"], 1)

    async def test_health_extra_가_실패해도_헬스체크는_성공한다(self):
        """부가 정보를 못 구한다고 파드를 NotReady 로 만들면 안 된다."""
        app = Starlette()
        server = ProcessGPTAgentServer(
            agent_executor=_ScriptedExecutor(), agent_type="test", tenant_auth=False,
        )

        def broken():
            raise RuntimeError("수집 실패")

        server.mount_chat_routes(app, health_path="/health", health_extra=broken)
        handler = next(
            r.endpoint for r in app.routes if getattr(r, "path", None) == "/health"
        )
        body = json_body(await handler(make_request("/health", {})))
        self.assertEqual(body["status"], "ok")
        self.assertFalse(body["busy"])
