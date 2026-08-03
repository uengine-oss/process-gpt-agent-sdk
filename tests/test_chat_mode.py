import asyncio
import unittest
from unittest import TestCase

from a2a.helpers import (
    new_task,
    new_text_artifact_update_event,
    new_text_message,
    new_text_status_update_event,
)
from a2a.types import Role
from a2a.types import TaskState

from processgpt_agent_sdk.chat_mode import (
    ChatEventQueue,
    ChatRequest,
    ChatRequestContext,
    ChatStreamer,
    _build_chat_message_payload,
)


class TestChatEventQueue(unittest.IsolatedAsyncioTestCase):
    """A2A 타입 = 라우팅 키:

    - Task: silently ignore
    - Message: SSE 토큰 청크 (반복 허용, 토큰 1개 = SSE message 1개)
    - TaskStatusUpdateEvent: 프로세스 전용. silently ignore.
    - TaskArtifactUpdateEvent(last_chunk=True): 최종 결과 → SSE done + chats 저장
    """

    async def test_silently_ignores_task_lifecycle_event(self):
        out_q: asyncio.Queue[dict] = asyncio.Queue()
        q = ChatEventQueue(out_q, request=ChatRequest(message="user"))

        task = new_task(task_id="t1", context_id="c1", state=TaskState.TASK_STATE_SUBMITTED)
        await q.enqueue_event(task)

        self.assertTrue(out_q.empty())

    async def test_silently_ignores_status_update(self):
        # 프로세스 전용 이벤트는 채팅 큐에서 무시
        out_q: asyncio.Queue[dict] = asyncio.Queue()
        q = ChatEventQueue(out_q, request=ChatRequest(message="user"))

        evt = new_text_status_update_event(
            task_id="t1",
            context_id="c1",
            state=TaskState.TASK_STATE_WORKING,
            text="안녕",
        )
        await q.enqueue_event(evt)

        self.assertTrue(out_q.empty())

    async def test_message_emits_sse_token(self):
        # Message 1개 → SSE token 1개
        out_q: asyncio.Queue[dict] = asyncio.Queue()
        q = ChatEventQueue(out_q, request=ChatRequest(message="user"))

        msg = new_text_message("우리", role=Role.ROLE_AGENT)
        await q.enqueue_event(msg)

        item = await out_q.get()
        self.assertEqual(item["event"], "message")
        self.assertEqual(item["data"]["type"], "token")
        self.assertEqual(item["data"]["content"], "우리")
        self.assertFalse(getattr(q, "_finalized"))

    async def test_multiple_messages_emit_multiple_tokens(self):
        # 토큰 스트리밍: 여러 Message 가 각각 SSE token 1개씩
        out_q: asyncio.Queue[dict] = asyncio.Queue()
        q = ChatEventQueue(out_q, request=ChatRequest(message="user"))

        for tok in ["안", "녕", "하세요"]:
            await q.enqueue_event(new_text_message(tok, role=Role.ROLE_AGENT))

        items = [await out_q.get() for _ in range(3)]
        self.assertEqual([i["data"]["content"] for i in items], ["안", "녕", "하세요"])
        self.assertEqual([i["data"]["type"] for i in items], ["token", "token", "token"])

    async def test_artifact_last_chunk_finalizes(self):
        out_q: asyncio.Queue[dict] = asyncio.Queue()
        q = ChatEventQueue(out_q, request=ChatRequest(message="user"))

        evt = new_text_artifact_update_event(
            task_id="t1",
            context_id="c1",
            name="assistant_response",
            text="안녕하세요",
            last_chunk=True,
        )
        await q.enqueue_event(evt)

        item = await out_q.get()
        self.assertEqual(item["event"], "message")
        self.assertEqual(item["data"]["type"], "done")
        self.assertEqual(item["data"]["content"], "안녕하세요")
        self.assertTrue(getattr(q, "_finalized"))

    async def test_full_streaming_flow(self):
        # 실제 패턴: Task → Messages (토큰들) → 최종 Artifact
        out_q: asyncio.Queue[dict] = asyncio.Queue()
        q = ChatEventQueue(out_q, request=ChatRequest(message="user"))

        await q.enqueue_event(new_task(task_id="t1", context_id="c1", state=TaskState.TASK_STATE_SUBMITTED))
        for tok in ["안", "녕"]:
            await q.enqueue_event(new_text_message(tok, role=Role.ROLE_AGENT))
        await q.enqueue_event(
            new_text_artifact_update_event(
                task_id="t1", context_id="c1",
                name="assistant_response", text="안녕",
                last_chunk=True,
            )
        )

        items = []
        while not out_q.empty():
            items.append(out_q.get_nowait())

        self.assertEqual(len(items), 3)  # 2 토큰 + 1 done (Task는 무시됨)
        self.assertEqual(items[0]["data"]["content"], "안")
        self.assertEqual(items[1]["data"]["content"], "녕")
        self.assertEqual(items[2]["data"]["type"], "done")
        self.assertEqual(items[2]["data"]["content"], "안녕")

    async def test_rejects_multiple_finals(self):
        out_q: asyncio.Queue[dict] = asyncio.Queue()
        q = ChatEventQueue(out_q, request=ChatRequest(message="user"))

        evt1 = new_text_artifact_update_event(
            task_id="t1", context_id="c1", name="r", text="hi", last_chunk=True,
        )
        evt2 = new_text_artifact_update_event(
            task_id="t1", context_id="c1", name="r", text="hi2", last_chunk=True,
        )
        await q.enqueue_event(evt1)
        with self.assertRaises(RuntimeError):
            await q.enqueue_event(evt2)

    async def test_artifact_triggers_persist_with_response_text(self):
        out_q: asyncio.Queue[dict] = asyncio.Queue()
        calls = []

        async def _persist(req: ChatRequest, event, response_text: str):
            calls.append((req.message, response_text, type(event).__name__))

        req = ChatRequest(message="user")
        q = ChatEventQueue(out_q, request=req, persist=_persist)

        evt = new_text_artifact_update_event(
            task_id="t1", context_id="c1", name="r", text="hi", last_chunk=True,
        )
        await q.enqueue_event(evt)

        self.assertEqual(calls[0][0], "user")
        self.assertEqual(calls[0][1], "hi")
        self.assertEqual(calls[0][2], "TaskArtifactUpdateEvent")

    async def test_streamer_emits_chunks(self):
        out_q: asyncio.Queue[dict] = asyncio.Queue()
        streamer = ChatStreamer(out_q)
        req = ChatRequest(message="user")
        ctx = ChatRequestContext(req, streamer=streamer)

        extras = ctx.get_context_data()["extras"]
        self.assertIs(extras["streamer"], streamer)

        await streamer.send_text("a")
        await streamer.send_json({"b": 1})

        item1 = await out_q.get()
        self.assertEqual(item1["event"], "message")
        self.assertEqual(item1["data"]["type"], "token")
        self.assertEqual(item1["data"]["content"], "a")

        item2 = await out_q.get()
        self.assertEqual(item2["event"], "message")
        self.assertEqual(item2["data"], {"b": 1})


class TestChatRequestContext(TestCase):
    """SDK 채팅 extras가 executor(HTTP 채팅과 유사)로 식별자를 넘길 수 있게 구성되는지."""

    def test_extras_top_level_and_input_data_match(self):
        req = ChatRequest(
            message="hello",
            tenant_id="ten",
            user_uid="uid",
            user_email="a@b.c",
            user_name="nm",
            user_jwt="tok",
            metadata={"x": 1},
        )
        ctx = ChatRequestContext(req)
        ex = ctx.get_context_data()["extras"]
        self.assertEqual(ex["tenant_id"], "ten")
        self.assertEqual(ex["user_jwt"], "tok")
        self.assertEqual(ex["input_data"]["tenant_id"], "ten")
        self.assertEqual(ex["input_data"]["user_jwt"], "tok")
        self.assertEqual(ex["input_data"]["metadata"], {"x": 1})
        self.assertEqual(ex["notify_user_emails"], ["a@b.c"])


class TestBuildChatMessagePayload(TestCase):
    """agent_profile(id/username) → payload(agentId/name/userName/email) 매핑.

    프론트가 실제 응답 에이전트의 agent_profile을 보내도 필드명이 달라(id vs agentId,
    username vs userName) payload.update()만으로는 반영되지 않고 항상 기본값
    (process-gpt-agent)으로 저장되던 버그의 회귀 테스트.
    """

    def test_agent_profile_overrides_default_identity(self):
        req = ChatRequest(
            message="hello",
            metadata={
                "agent_profile": {
                    "id": "00f64687-fcb2-d6b9-cee2-da75728c1c51",
                    "username": "재고 데이터 조회 에이전트",
                    "role": "inventory",
                }
            },
        )
        msg = new_text_message("답변", role=Role.ROLE_AGENT)

        payload = _build_chat_message_payload(req, msg, "답변")

        self.assertEqual(payload["agentId"], "00f64687-fcb2-d6b9-cee2-da75728c1c51")
        self.assertEqual(payload["email"], "agent:00f64687-fcb2-d6b9-cee2-da75728c1c51")
        self.assertEqual(payload["name"], "재고 데이터 조회 에이전트")
        self.assertEqual(payload["userName"], "재고 데이터 조회 에이전트")
        self.assertEqual(payload["role"], "inventory")

    def test_missing_agent_profile_keeps_default_identity(self):
        req = ChatRequest(message="hello")
        msg = new_text_message("답변", role=Role.ROLE_AGENT)

        payload = _build_chat_message_payload(req, msg, "답변")

        self.assertEqual(payload["agentId"], "process-gpt-agent")
        self.assertEqual(payload["email"], "agent:process-gpt-agent")
        self.assertEqual(payload["name"], "Process GPT Agent")
        self.assertEqual(payload["userName"], "Process GPT Agent")

    def test_agent_profile_explicit_email_is_respected(self):
        req = ChatRequest(
            message="hello",
            metadata={"agent_profile": {"id": "abc-123", "email": "custom@agent.io"}},
        )
        msg = new_text_message("답변", role=Role.ROLE_AGENT)

        payload = _build_chat_message_payload(req, msg, "답변")

        self.assertEqual(payload["agentId"], "abc-123")
        self.assertEqual(payload["email"], "custom@agent.io")


if __name__ == "__main__":
    unittest.main()
