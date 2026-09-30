"""수정 지시(스티어링) — 진행 중인 턴의 방향을 바꾸는 표준 동작.

여기서 보는 것은 처리 규칙이다. "지시를 받았다"와 "지시가 반영됐다"가 서로 다른
이벤트로 나가는지, 도구가 도는 중에 온 지시가 어디서 집어지는지, 마무리에 들어간
턴·사람 확인 대기·중복 연속 수신·재접속·미지원 에이전트가 각각 어떻게 답하는지.
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
from processgpt_agent_sdk.steering import (
    EVENT_STEER_ACCEPTED,
    EVENT_STEER_APPLIED,
    REASON_AWAITING_HUMAN_INPUT,
    REASON_EMPTY_MESSAGE,
    REASON_NO_ACTIVE_TURN,
    REASON_UNSUPPORTED,
    SteerDirective,
    SteerResult,
    SteeringInbox,
    get_steering_inbox,
    mark_applied,
    set_steering_inbox,
)

from tests.test_chat_sse import json_body, make_request

ROOM = "room-1"
TENANT = "acme"


class _SteerableExecutor(AgentExecutor):
    """도구 하나를 도는 척하다가, 안전 지점에서 수정 지시를 집어 가는 Executor.

    실제 어댑터(deepagents)가 하는 일의 골격이 이것이다 — 도구를 중간에 끊지 않고,
    다음 판단 직전에 대기열을 비우고, 집어넣은 순간에 반영을 알린다.
    """

    def __init__(self):
        self.in_tool = asyncio.Event()
        self.release_tool = asyncio.Event()
        self.applied: list[str] = []
        self.reject: SteerResult | None = None
        self.seen_turn_active: list[bool] = []

    async def steer(self, directive: SteerDirective):
        self.seen_turn_active.append(directive.turn_active)
        return self.reject

    async def execute(self, context: RequestContext, event_queue: EventQueue) -> None:
        streamer = get_streamer(context)
        cid = str(context.context_id)
        inbox = get_steering_inbox()

        await streamer.send_text("원래지시")
        self.in_tool.set()
        await self.release_tool.wait()          # ← 도구 실행 중

        # 안전 지점: 도구가 끝났고 다음 판단 전이다.
        for directive in await inbox.take(cid):
            self.applied.append(directive.message)
            await mark_applied(directive)
            await streamer.send_text(f"|{directive.message}")

        # 마무리 직전에 대기열을 닫는다 — 이 뒤에 온 지시는 거절된다.
        for directive in await inbox.close(cid):
            self.applied.append(directive.message)
            await mark_applied(directive)
            await streamer.send_text(f"|{directive.message}")

        await event_queue.enqueue_event(
            new_text_artifact_update_event(
                task_id=str(context.task_id),
                context_id=cid,
                name="assistant_response",
                text="끝",
                last_chunk=True,
            )
        )

    async def cancel(self, context: RequestContext, event_queue: EventQueue) -> None:
        pass


class _PlainExecutor(AgentExecutor):
    """`steer()` 를 구현하지 않은(=미지원) 에이전트."""

    def __init__(self):
        self.ran = asyncio.Event()

    async def execute(self, context: RequestContext, event_queue: EventQueue) -> None:
        self.ran.set()
        await event_queue.enqueue_event(
            new_text_artifact_update_event(
                task_id=str(context.task_id),
                context_id=str(context.context_id),
                name="assistant_response",
                text="평범한 응답",
                last_chunk=True,
            )
        )

    async def cancel(self, context: RequestContext, event_queue: EventQueue) -> None:
        pass


class _SteerRouteTestBase(unittest.IsolatedAsyncioTestCase):
    EXECUTOR = _SteerableExecutor

    async def asyncSetUp(self):
        self.registry = ChatRunRegistry()
        self.inflight = InflightRegistry()
        self.inbox = SteeringInbox()
        set_run_registry(self.registry)
        set_inflight_registry(self.inflight)
        set_steering_inbox(self.inbox)

        self._orig_fetch = chat_sse.fetch_chat_room_tenant_id

        async def fake_fetch(conversation_id: str) -> str:
            return TENANT if conversation_id == ROOM else ""

        chat_sse.fetch_chat_room_tenant_id = fake_fetch

        self.executor = self.EXECUTOR()
        self.server = ProcessGPTAgentServer(
            agent_executor=self.executor, agent_type="test", tenant_auth=False,
        )
        self.app = Starlette()
        self.server.mount_chat_routes(self.app)
        self.stream_handler = self._find("/chat/stream")
        self.steer_handler = self._find("/chat/steer")
        self.attach_handler = self._find("/chat/stream/attach")
        self.responses: list = []

    def _find(self, path):
        for route in self.app.routes:
            if getattr(route, "path", None) == path:
                return route.endpoint
        raise AssertionError(f"라우트를 찾지 못했습니다: {path}")

    async def asyncTearDown(self):
        release = getattr(self.executor, "release_tool", None)
        if release is not None:
            release.set()
        for response in self.responses:
            await response.body_iterator.aclose()
        chat_sse.fetch_chat_room_tenant_id = self._orig_fetch
        set_run_registry(ChatRunRegistry())
        set_inflight_registry(InflightRegistry())
        set_steering_inbox(SteeringInbox())

    async def _start_turn(self, **body):
        payload = {"message": "원래 지시", "conversation_id": ROOM, "tenant_id": TENANT}
        payload.update(body)
        response = await self.stream_handler(make_request("/chat/stream", payload))
        self.responses.append(response)
        return response

    async def _steer(self, message="대신 이렇게 해", **body):
        payload = {"conversation_id": ROOM, "tenant_id": TENANT, "message": message}
        payload.update(body)
        return await self.steer_handler(make_request("/chat/steer", payload))

    async def _read_events(self, response, count):
        """SSE 바디에서 이벤트 `count` 건을 읽어 data dict 목록으로 돌려준다.

        첫 `metadata` 이벤트(conversation_id)는 세어 주지 않는다 — 여기서 보는 것은
        턴이 내보내는 메시지들의 순서다.
        """
        out = []
        iterator = response.body_iterator
        while len(out) < count:
            chunk = await asyncio.wait_for(iterator.__anext__(), timeout=5)
            text = chunk.decode("utf-8") if isinstance(chunk, bytes) else str(chunk)
            if text.startswith(":"):      # 하트비트
                continue
            for line in text.splitlines():
                if not line.startswith("data: "):
                    continue
                payload = json.loads(line[6:])
                if payload.get("type"):
                    out.append(payload)
        return out


class TestSteerFlow(_SteerRouteTestBase):
    async def test_접수와_반영이_서로_다른_이벤트로_나간다(self):
        response = await self._start_turn()
        await asyncio.wait_for(self.executor.in_tool.wait(), timeout=5)

        result = await self._steer("숫자 앞에 포도를 붙여")
        body = json_body(result)
        self.assertTrue(body["accepted"])
        self.assertTrue(body["directive_id"])

        # 접수 시점에는 아직 도구가 돌고 있다 — 반영은 일어나지 않았다.
        self.assertEqual(self.executor.applied, [])

        self.executor.release_tool.set()

        events = await self._read_events(response, 5)
        types = [e.get("type") for e in events]
        # metadata(conversation_id) → 원래 토큰 → 접수 → 반영 → 반영된 토큰 → done
        self.assertEqual(
            types,
            ["token", EVENT_STEER_ACCEPTED, EVENT_STEER_APPLIED, "token", "done"],
            types,
        )
        accepted = events[types.index(EVENT_STEER_ACCEPTED)]
        applied = events[types.index(EVENT_STEER_APPLIED)]
        # 같은 지시의 접수와 반영은 같은 id 로 이어진다.
        self.assertEqual(accepted["directive_id"], applied["directive_id"])
        self.assertEqual(accepted["content"], "숫자 앞에 포도를 붙여")
        # 접수 이벤트만으로는 턴이 끝나지 않는다(완료 표시가 되면 안 된다).
        self.assertNotIn(EVENT_STEER_ACCEPTED, ("done", "error"))
        self.assertEqual(types[-1], "done")
        self.assertEqual(self.executor.applied, ["숫자 앞에 포도를 붙여"])

    async def test_도구_실행_중에_받은_지시는_도구를_끊지_않는다(self):
        await self._start_turn()
        await asyncio.wait_for(self.executor.in_tool.wait(), timeout=5)

        await self._steer("방향을 바꿔")
        # 도구가 도는 동안은 대기열에 있을 뿐이다.
        self.assertEqual([d.message for d in self.inbox.pending(ROOM)], ["방향을 바꿔"])
        self.assertEqual(self.executor.applied, [])

        self.executor.release_tool.set()
        await asyncio.sleep(0.05)
        self.assertEqual(self.executor.applied, ["방향을 바꿔"])
        self.assertEqual(self.inbox.pending(ROOM), [])

    async def test_중복_연속_수신은_한_번만_접수된다(self):
        response = await self._start_turn()
        await asyncio.wait_for(self.executor.in_tool.wait(), timeout=5)

        first = json_body(await self._steer("같은 지시"))
        second = json_body(await self._steer("같은  지시 "))      # 공백만 다른 같은 문장
        third = json_body(await self._steer("다른 지시"))

        self.assertTrue(second["accepted"])
        self.assertTrue(second["duplicate"])
        self.assertEqual(second["directive_id"], first["directive_id"])
        self.assertNotIn("duplicate", third)

        # 대기열에도 한 번만 쌓인다.
        self.assertEqual(
            [d.message for d in self.inbox.pending(ROOM)], ["같은 지시", "다른 지시"]
        )

        self.executor.release_tool.set()
        events = await self._read_events(response, 7)
        # 접수 이벤트도 중복만큼 늘지 않는다.
        self.assertEqual(
            [e.get("type") for e in events].count(EVENT_STEER_ACCEPTED), 2
        )
        self.assertEqual(self.executor.applied, ["같은 지시", "다른 지시"])

    async def test_마무리에_들어간_턴은_더_받지_않는다(self):
        """완료 직전: 대기열이 닫힌 뒤의 지시는 거절된다(조용히 버리지 않는다)."""
        await self._start_turn()
        await asyncio.wait_for(self.executor.in_tool.wait(), timeout=5)
        self.executor.release_tool.set()
        await asyncio.sleep(0.05)          # 턴이 마무리까지 갔다

        result = await self._steer("늦은 지시")
        self.assertEqual(result.status_code, 409)
        self.assertEqual(json_body(result), {"accepted": False, "reason": REASON_NO_ACTIVE_TURN})
        self.assertEqual(self.executor.applied, [])

    async def test_닫히기_직전에_들어온_지시는_유실되지_않는다(self):
        """어댑터가 닫으면서 남은 지시를 집어 가므로 접수된 것은 반드시 반영된다."""
        await self._start_turn()
        await asyncio.wait_for(self.executor.in_tool.wait(), timeout=5)

        # take() 와 close() 사이에 들어온 것처럼, 대기열에 넣고 바로 풀어 준다.
        await self._steer("아슬아슬한 지시")
        self.executor.release_tool.set()
        await asyncio.sleep(0.05)
        self.assertEqual(self.executor.applied, ["아슬아슬한 지시"])

    async def test_진행_중인_턴이_없으면_거절한다(self):
        result = await self._steer("허공에 보내는 지시")
        self.assertEqual(result.status_code, 409)
        self.assertEqual(json_body(result)["reason"], REASON_NO_ACTIVE_TURN)

    async def test_빈_지시는_거절한다(self):
        await self._start_turn()
        await asyncio.wait_for(self.executor.in_tool.wait(), timeout=5)
        result = await self._steer("   ")
        self.assertEqual(result.status_code, 400)
        self.assertEqual(json_body(result)["reason"], REASON_EMPTY_MESSAGE)

    async def test_어댑터가_사람_확인_대기를_이유로_거절할_수_있다(self):
        """HITL 대기 중: 돌고 있는 실행이 없으므로 방향을 넣을 곳이 없다."""
        await self._start_turn()
        await asyncio.wait_for(self.executor.in_tool.wait(), timeout=5)
        self.executor.reject = SteerResult.reject(REASON_AWAITING_HUMAN_INPUT)

        result = await self._steer("지금은 안 되는 지시")
        self.assertEqual(result.status_code, 409)
        self.assertEqual(json_body(result)["reason"], REASON_AWAITING_HUMAN_INPUT)
        # 거절된 지시는 대기열에 남지 않는다.
        self.assertEqual(self.inbox.pending(ROOM), [])

    async def test_어댑터는_턴이_돌고_있는지를_함께_받는다(self):
        await self._start_turn()
        await asyncio.wait_for(self.executor.in_tool.wait(), timeout=5)
        await self._steer("지시")
        self.assertEqual(self.executor.seen_turn_active, [True])

    async def test_다른_테넌트는_남의_턴을_돌릴_수_없다(self):
        await self._start_turn()
        await asyncio.wait_for(self.executor.in_tool.wait(), timeout=5)
        result = await self._steer("남의 방 지시", tenant_id="other")
        self.assertEqual(result.status_code, 403)
        self.assertEqual(self.inbox.pending(ROOM), [])

    async def test_재접속하면_아직_반영되지_않은_지시가_스냅샷에_실린다(self):
        await self._start_turn()
        await asyncio.wait_for(self.executor.in_tool.wait(), timeout=5)
        await self._steer("반영 대기 중인 지시")

        response = await self.attach_handler(make_request(
            "/chat/stream/attach", {"conversation_id": ROOM, "tenant_id": TENANT},
        ))
        stream = response.body_iterator
        try:
            first = await asyncio.wait_for(stream.__anext__(), timeout=5)
            payload = json.loads(first.decode("utf-8").split("data: ", 1)[1])
            self.assertEqual(payload["type"], "snapshot")
            self.assertEqual(
                [p["content"] for p in payload["pending_steers"]], ["반영 대기 중인 지시"]
            )
        finally:
            await stream.aclose()

    async def test_재접속한_클라이언트도_접수와_반영을_받는다(self):
        await self._start_turn()
        await asyncio.wait_for(self.executor.in_tool.wait(), timeout=5)

        response = await self.attach_handler(make_request(
            "/chat/stream/attach", {"conversation_id": ROOM, "tenant_id": TENANT},
        ))
        self.responses.append(response)
        stream = response.body_iterator
        await asyncio.wait_for(stream.__anext__(), timeout=5)      # 스냅샷

        await self._steer("재접속 중에 보낸 지시")
        self.executor.release_tool.set()
        events = await self._read_events(response, 4)
        types = [e.get("type") for e in events]
        self.assertIn(EVENT_STEER_ACCEPTED, types)
        self.assertIn(EVENT_STEER_APPLIED, types)
        self.assertLess(types.index(EVENT_STEER_ACCEPTED), types.index(EVENT_STEER_APPLIED))


class TestActionDispatch(_SteerRouteTestBase):
    """동작 유형(`action`) — 없으면 종전대로 실행된다."""

    async def test_동작_유형이_없으면_종전대로_새_턴을_돌린다(self):
        response = await self._start_turn()
        self.assertEqual(response.media_type, "text/event-stream")
        await asyncio.wait_for(self.executor.in_tool.wait(), timeout=5)

    async def test_스트림_경로로_보낸_steer_도_같이_처리된다(self):
        """프론트가 엔드포인트 하나만 알고 있어도 방향을 바꿀 수 있다."""
        await self._start_turn()
        await asyncio.wait_for(self.executor.in_tool.wait(), timeout=5)

        response = await self.stream_handler(make_request("/chat/stream", {
            "action": "steer", "conversation_id": ROOM, "tenant_id": TENANT,
            "message": "스트림 경로로 보낸 지시",
        }))
        self.assertTrue(json_body(response)["accepted"])
        self.assertEqual(
            [d.message for d in self.inbox.pending(ROOM)], ["스트림 경로로 보낸 지시"]
        )

    async def test_모르는_동작_유형은_거절한다(self):
        """오타를 평범한 메시지로 흘리면 방향을 바꾸려던 요청이 턴을 날린다."""
        await self._start_turn()
        await asyncio.wait_for(self.executor.in_tool.wait(), timeout=5)
        task = self.inflight.get_inflight(ROOM)

        response = await self.stream_handler(make_request("/chat/stream", {
            "action": "steeer", "conversation_id": ROOM, "tenant_id": TENANT,
            "message": "오타 난 지시",
        }))
        self.assertEqual(response.status_code, 400)
        # 진행 중인 턴은 그대로 살아 있다.
        self.assertIs(self.inflight.get_inflight(ROOM), task)
        self.assertFalse(task.cancelled())


class TestUnsupportedAgent(_SteerRouteTestBase):
    """`steer()` 를 구현하지 않은 에이전트."""

    EXECUTOR = _PlainExecutor

    async def test_미지원_에이전트는_미지원_오류를_돌려준다(self):
        self.assertFalse(self.server.supports_steering())
        result = await self._steer("지원하지 않는 지시")
        self.assertEqual(result.status_code, 501)
        self.assertEqual(json_body(result)["reason"], REASON_UNSUPPORTED)

    async def test_동작_유형이_없는_기존_요청은_종전대로_실행된다(self):
        response = await self._start_turn()
        self.assertEqual(response.media_type, "text/event-stream")
        events = await self._read_events(response, 1)
        self.assertEqual(events[-1], {"type": "done", "content": "평범한 응답"}, events)
        self.assertTrue(self.executor.ran.is_set())


class TestSteeringInbox(unittest.IsolatedAsyncioTestCase):
    """대기열 자체의 규칙."""

    def _d(self, message, cid=ROOM):
        return SteerDirective(conversation_id=cid, message=message)

    async def asyncSetUp(self):
        self.inbox = SteeringInbox()

    async def test_집어_간_뒤에_같은_문장이_또_오면_중복이다(self):
        await self.inbox.accept(self._d("같은 지시"))
        await self.inbox.take(ROOM)
        accepted, effective = await self.inbox.accept(self._d("같은 지시"))
        self.assertFalse(accepted)
        self.assertEqual(self.inbox.pending(ROOM), [])
        self.assertTrue(effective.directive_id)

    async def test_사이에_다른_문장이_끼면_연속이_끊긴다(self):
        await self.inbox.accept(self._d("A"))
        await self.inbox.take(ROOM)
        await self.inbox.accept(self._d("B"))
        await self.inbox.take(ROOM)
        accepted, _ = await self.inbox.accept(self._d("A"))
        self.assertTrue(accepted)

    async def test_닫으면_남은_지시를_돌려주고_더는_열리지_않는다(self):
        await self.inbox.accept(self._d("남은 지시"))
        left = await self.inbox.close(ROOM)
        self.assertEqual([d.message for d in left], ["남은 지시"])
        self.assertTrue(self.inbox.is_closed(ROOM))

    async def test_이전_턴의_늦은_닫기가_다음_턴의_대기열을_닫지_않는다(self):
        await self.inbox.open(ROOM, token="turn-1")
        await self.inbox.open(ROOM, token="turn-2")
        await self.inbox.accept(self._d("두_번째_턴의_지시"))

        left = await self.inbox.close(ROOM, token="turn-1")      # 늦게 도착한 닫기
        self.assertEqual(left, [])
        self.assertFalse(self.inbox.is_closed(ROOM))
        self.assertEqual([d.message for d in self.inbox.pending(ROOM)], ["두_번째_턴의_지시"])

    async def test_새_턴을_열면_이전_턴의_잔여_지시는_버려진다(self):
        await self.inbox.accept(self._d("이전 턴의 지시"))
        await self.inbox.open(ROOM, token="new")
        self.assertEqual(self.inbox.pending(ROOM), [])

    async def test_닫았다_다시_열면_또_받는다(self):
        """마무리 직전에 닫았는데 남은 지시가 있어 한 라운드 더 도는 경우."""
        await self.inbox.open(ROOM, token="turn-1")
        await self.inbox.accept(self._d("마지막에 들어온 지시"))
        left = await self.inbox.close(ROOM)
        self.assertEqual([d.message for d in left], ["마지막에 들어온 지시"])

        await self.inbox.reopen(ROOM)
        self.assertFalse(self.inbox.is_closed(ROOM))
        accepted, _ = await self.inbox.accept(self._d("한 라운드 더 도는 중에 온 지시"))
        self.assertTrue(accepted)
        # 표식은 그대로라, 턴이 끝날 때 프레임워크가 하는 닫기가 여전히 먹는다.
        self.assertTrue(await self.inbox.close(ROOM, token="turn-1"))
        self.assertTrue(self.inbox.is_closed(ROOM))

    async def test_닫힌_대기열은_더_받지_않는다(self):
        """판정과 접수 사이에 턴이 마무리에 들어간 경우 — 받아 두고 버리면 안 된다."""
        await self.inbox.close(ROOM)
        accepted, effective = await self.inbox.accept(self._d("늦은 지시"))
        self.assertFalse(accepted)
        self.assertIsNone(effective)
        self.assertEqual(self.inbox.pending(ROOM), [])

    async def test_방마다_따로_쌓인다(self):
        await self.inbox.accept(self._d("이 방", cid="a"))
        await self.inbox.accept(self._d("저 방", cid="b"))
        self.assertEqual([d.message for d in self.inbox.pending("a")], ["이 방"])
        self.assertEqual([d.message for d in self.inbox.pending("b")], ["저 방"])


if __name__ == "__main__":
    unittest.main()
