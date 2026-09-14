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
