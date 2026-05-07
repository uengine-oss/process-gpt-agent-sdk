import asyncio
import unittest

from a2a.helpers import new_text_message
from a2a.helpers import new_text_status_update_event
from a2a.types import Role
from a2a.types import TaskState

from processgpt_agent_sdk.chat_mode import ChatEventQueue, ChatRequest, ChatRequestContext, ChatStreamer


class TestChatEventQueue(unittest.IsolatedAsyncioTestCase):
    async def test_accepts_single_message(self):
        out_q: asyncio.Queue[dict] = asyncio.Queue()
        q = ChatEventQueue(out_q, request=ChatRequest(message="user"))

        msg = new_text_message("hello", role=Role.ROLE_AGENT)
        await q.enqueue_event(msg)

        item = await out_q.get()
        self.assertEqual(item["event"], "message")
        self.assertEqual(item["data"]["type"], "done")
        self.assertEqual(item["data"]["content"], "hello")

    async def test_rejects_multiple_messages(self):
        out_q: asyncio.Queue[dict] = asyncio.Queue()
        q = ChatEventQueue(out_q, request=ChatRequest(message="user"))

        msg1 = new_text_message("one", role=Role.ROLE_AGENT)
        msg2 = new_text_message("two", role=Role.ROLE_AGENT)

        await q.enqueue_event(msg1)
        with self.assertRaises(RuntimeError):
            await q.enqueue_event(msg2)

    async def test_accepts_status_update_metadata_as_message_data(self):
        out_q: asyncio.Queue[dict] = asyncio.Queue()
        q = ChatEventQueue(out_q, request=ChatRequest(message="user"))

        evt = new_text_status_update_event(
            task_id="t1",
            context_id="c1",
            state=TaskState.TASK_STATE_WORKING,
            text="",
        )
        evt.metadata.update({"type": "token", "content": "우리"})
        await q.enqueue_event(evt)

        item = await out_q.get()
        self.assertEqual(item["event"], "message")
        self.assertEqual(item["data"]["type"], "token")
        self.assertEqual(item["data"]["content"], "우리")

    async def test_message_calls_persist(self):
        out_q: asyncio.Queue[dict] = asyncio.Queue()
        calls = []

        async def _persist(req: ChatRequest, message, response_text: str):
            calls.append((req.message, response_text, dict(message.metadata)))

        req = ChatRequest(message="user")
        q = ChatEventQueue(out_q, request=req, persist=_persist)

        msg = new_text_message("hi", role=Role.ROLE_AGENT)
        msg.metadata.update({"chat_payload": {"x": 1}})
        await q.enqueue_event(msg)

        self.assertEqual(calls[0][0], "user")
        self.assertEqual(calls[0][1], "hi")
        self.assertIn("chat_payload", calls[0][2])

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


if __name__ == "__main__":
    unittest.main()

