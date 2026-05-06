import asyncio
import unittest

from a2a.helpers import new_text_message
from a2a.types import Role

from processgpt_agent_sdk.chat_mode import ChatEventQueue, ChatRequest, ChatRequestContext, ChatStreamer


class TestChatEventQueue(unittest.IsolatedAsyncioTestCase):
    async def test_accepts_single_message(self):
        out_q: asyncio.Queue[dict] = asyncio.Queue()
        q = ChatEventQueue(out_q, request=ChatRequest(message="user"))

        msg = new_text_message("hello", role=Role.ROLE_AGENT)
        await q.enqueue_event(msg)

        item = await out_q.get()
        self.assertEqual(item["type"], "message")
        self.assertEqual(item["text"], "hello")

    async def test_rejects_multiple_messages(self):
        out_q: asyncio.Queue[dict] = asyncio.Queue()
        q = ChatEventQueue(out_q, request=ChatRequest(message="user"))

        msg1 = new_text_message("one", role=Role.ROLE_AGENT)
        msg2 = new_text_message("two", role=Role.ROLE_AGENT)

        await q.enqueue_event(msg1)
        with self.assertRaises(RuntimeError):
            await q.enqueue_event(msg2)

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

        self.assertEqual((await out_q.get())["type"], "chunk")
        self.assertEqual((await out_q.get())["type"], "chunk_json")


if __name__ == "__main__":
    unittest.main()

