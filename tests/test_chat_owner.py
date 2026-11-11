"""채팅 넘겨주기(chat_owner) — 진행 중인 턴이 있는 대화의 요청을 그 턴을 돌리는 파드로.

지키려는 것:
  - 새 턴은 주인 자리를 잡고 직접 처리하거나, 이미 다른 파드가 돌리고 있으면 그 파드로 넘긴다.
  - 재접속 · 중지 · 방향 바꾸기는 주인이 다른 파드면 넘기고, 주인이 없으면 직접 처리한다.
  - 넘겨받은 요청은 다시 넘기지 않는다(맴돌기 방지).
  - 주인 파드에 닿지 않으면 그 기록만 지우고 직접 처리한다.
  - 실행을 시작하지 못한 요청(4xx)은 예약을 바로 푼다.
  - 같은 대화의 앞 턴과 새 턴이 겹쳐도, 마지막 턴이 끝날 때만 기록을 지운다.

프로세스 둘이 Redis 하나를 같이 쓰는 경우는 test_chat_owner_processes.py 가 본다.
"""

import asyncio
import json
import unittest

from processgpt_agent_sdk import chat_owner
from processgpt_agent_sdk.chat_owner import (
    ChatOwnerForwardMiddleware,
    MemoryOwnerDirectory,
    OwnerForwarding,
    configure_owner_forwarding,
)

ME = "http://10.0.0.1:8888"
OTHER = "http://10.0.0.2:8888"
ROOM = "room-1"


def _scope(path, headers=None):
    return {
        "type": "http",
        "method": "POST",
        "path": path,
        "query_string": b"",
        "headers": [(k.encode(), v.encode()) for k, v in (headers or {}).items()],
    }


def _receive(body: dict):
    raw = json.dumps(body).encode()
    sent = False

    async def receive():
        nonlocal sent
        if not sent:
            sent = True
            return {"type": "http.request", "body": raw, "more_body": False}
        await asyncio.sleep(3600)

    return receive


class _App:
    """하위 앱(테넌트 가드 · 핸들러 자리). 받은 본문과 응답 코드를 남긴다."""

    def __init__(self, status=200):
        self.status = status
        self.calls = []

    async def __call__(self, scope, receive, send):
        message = await receive()
        self.calls.append(json.loads(message["body"] or b"{}"))
        await send({"type": "http.response.start", "status": self.status, "headers": []})
        await send({"type": "http.response.body", "body": b"{}", "more_body": False})


async def _sent(_message):
    return None


class TestMemoryOwnerDirectory(unittest.IsolatedAsyncioTestCase):
    async def test_먼저_잡은_쪽이_주인이고_다른_쪽은_주인을_돌려받는다(self):
        d = MemoryOwnerDirectory()
        self.assertEqual(await d.claim(ROOM, ME, 30), (ME, True))
        self.assertEqual(await d.claim(ROOM, OTHER, 30), (ME, False))
        self.assertEqual(await d.claim(ROOM, ME, 30), (ME, False), "이미 내 것이면 새로 잡은 것이 아니다")
        self.assertEqual(await d.lookup(ROOM), ME)

    async def test_주인이_아니면_갱신하지도_지우지도_못한다(self):
        d = MemoryOwnerDirectory()
        await d.claim(ROOM, ME, 30)
        self.assertFalse(await d.renew(ROOM, OTHER, 30))
        await d.release(ROOM, OTHER)
        self.assertEqual(await d.lookup(ROOM), ME)
        await d.release(ROOM, ME)
        self.assertIsNone(await d.lookup(ROOM))

    async def test_만료되면_다른_파드가_잡는다(self):
        d = MemoryOwnerDirectory()
        await d.claim(ROOM, ME, 0.05)
        await asyncio.sleep(0.08)
        self.assertIsNone(await d.lookup(ROOM))
        self.assertEqual(await d.claim(ROOM, OTHER, 30), (OTHER, True))


class TestHold(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.d = MemoryOwnerDirectory()
        self.fw = OwnerForwarding(directory=self.d, address=ME, ttl=0.3)

    async def asyncTearDown(self):
        await self.fw.close()

    async def test_턴이_도는_동안_만료를_넘겨도_주인으로_남고_끝나면_지운다(self):
        done = asyncio.Event()
        task = asyncio.create_task(done.wait())
        self.fw.hold(ROOM, task)
        await asyncio.sleep(0.6)  # ttl 0.3 의 두 배 — 갱신이 없으면 사라졌을 시간
        self.assertEqual(await self.d.lookup(ROOM), ME)
        done.set()
        await asyncio.sleep(0.05)
        self.assertIsNone(await self.d.lookup(ROOM))

    async def test_앞_턴이_새_턴보다_먼저_끝나도_기록은_남는다(self):
        first, second = asyncio.Event(), asyncio.Event()
        t1 = asyncio.create_task(first.wait())
        t2 = asyncio.create_task(second.wait())
        self.fw.hold(ROOM, t1)
        self.fw.hold(ROOM, t2)
        first.set()
        await asyncio.sleep(0.05)
        self.assertEqual(await self.d.lookup(ROOM), ME, "새 턴이 아직 돈다")
        second.set()
        await asyncio.sleep(0.05)
        self.assertIsNone(await self.d.lookup(ROOM))


class TestMiddleware(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.d = MemoryOwnerDirectory()
        self.fw = OwnerForwarding(directory=self.d, address=ME, ttl=30)
        configure_owner_forwarding(self.fw)
        self.forwarded = []
        self.reachable = True
        self._orig_forward = chat_owner._forward

        async def fake_forward(fw, owner, scope, body, receive, send):
            if not self.reachable:
                return False
            self.forwarded.append((owner, scope["path"], json.loads(body)))
            return True

        chat_owner._forward = fake_forward

    async def asyncTearDown(self):
        chat_owner._forward = self._orig_forward
        configure_owner_forwarding(None)
        await self.fw.close()

    def _mw(self, app):
        return ChatOwnerForwardMiddleware(
            app,
            stream_paths=("/chat/stream",),
            other_paths=("/chat/stream/attach", "/chat/stop", "/chat/steer"),
        )

    async def test_새_턴은_자리를_잡고_직접_처리한다(self):
        app = _App()
        await self._mw(app)(_scope("/chat/stream"), _receive({"conversation_id": ROOM, "message": "안녕"}), _sent)
        self.assertEqual(len(app.calls), 1)
        self.assertEqual(self.forwarded, [])
        self.assertEqual(await self.d.lookup(ROOM), ME)

    async def test_다른_파드가_돌리는_방의_새_질문은_그_파드로_넘긴다(self):
        await self.d.claim(ROOM, OTHER, 30)
        app = _App()
        await self._mw(app)(_scope("/chat/stream"), _receive({"conversation_id": ROOM, "message": "다음"}), _sent)
        self.assertEqual(app.calls, [])
        self.assertEqual(self.forwarded, [(OTHER, "/chat/stream", {"conversation_id": ROOM, "message": "다음"})])

    async def test_재접속_중지_방향_바꾸기는_주인에게_넘긴다(self):
        await self.d.claim(ROOM, OTHER, 30)
        app = _App()
        for path in ("/chat/stream/attach", "/chat/stop", "/chat/steer"):
            await self._mw(app)(_scope(path), _receive({"conversation_id": ROOM}), _sent)
        # 스트림 경로로 온 방향 바꾸기(action=steer)도 새 턴이 아니라 넘길 요청이다.
        await self._mw(app)(
            _scope("/chat/stream"), _receive({"conversation_id": ROOM, "action": "steer", "message": "그게 아니라"}), _sent
        )
        self.assertEqual(app.calls, [])
        self.assertEqual(
            [p for _o, p, _b in self.forwarded],
            ["/chat/stream/attach", "/chat/stop", "/chat/steer", "/chat/stream"],
        )
        self.assertEqual(await self.d.lookup(ROOM), OTHER, "넘기기만 했을 뿐 주인은 그대로다")

    async def test_주인이_없으면_재접속은_직접_처리하고_자리를_잡지_않는다(self):
        app = _App()
        await self._mw(app)(_scope("/chat/stream/attach"), _receive({"conversation_id": ROOM}), _sent)
        self.assertEqual(len(app.calls), 1)
        self.assertIsNone(await self.d.lookup(ROOM))

    async def test_넘겨받은_요청은_다시_넘기지_않는다(self):
        await self.d.claim(ROOM, OTHER, 30)
        app = _App()
        await self._mw(app)(
            _scope("/chat/stop", {"X-Chat-Forwarded-By": OTHER}), _receive({"conversation_id": ROOM}), _sent
        )
        self.assertEqual(len(app.calls), 1)
        self.assertEqual(self.forwarded, [])

    async def test_주인에게_닿지_않으면_기록을_지우고_직접_처리한다(self):
        await self.d.claim(ROOM, OTHER, 30)
        self.reachable = False
        app = _App()
        await self._mw(app)(_scope("/chat/stream"), _receive({"conversation_id": ROOM, "message": "다시"}), _sent)
        self.assertEqual(len(app.calls), 1)
        self.assertEqual(await self.d.lookup(ROOM), ME, "새 턴이므로 이 파드가 자리를 잡는다")

    async def test_실행을_시작하지_못한_요청은_예약을_바로_푼다(self):
        app = _App(status=401)
        await self._mw(app)(_scope("/chat/stream"), _receive({"conversation_id": ROOM, "message": "안녕"}), _sent)
        self.assertIsNone(await self.d.lookup(ROOM))

    async def test_대화_id_가_없으면_직접_처리한다(self):
        app = _App()
        await self._mw(app)(_scope("/chat/stream"), _receive({"message": "새 대화"}), _sent)
        self.assertEqual(len(app.calls), 1)
        self.assertEqual(self.forwarded, [])

    async def test_기록소가_응답하지_않으면_직접_처리한다(self):
        class Broken(MemoryOwnerDirectory):
            async def claim(self, *a, **k):
                raise ConnectionError("redis down")

            async def lookup(self, *a, **k):
                raise ConnectionError("redis down")

        self.fw.directory = Broken()
        app = _App()
        await self._mw(app)(_scope("/chat/stream"), _receive({"conversation_id": ROOM, "message": "안녕"}), _sent)
        await self._mw(app)(_scope("/chat/stop"), _receive({"conversation_id": ROOM}), _sent)
        self.assertEqual(len(app.calls), 2)

    async def test_넘겨주기가_꺼져_있으면_그대로_통과한다(self):
        configure_owner_forwarding(None)
        await self.d.claim(ROOM, OTHER, 30)
        app = _App()
        await self._mw(app)(_scope("/chat/stop"), _receive({"conversation_id": ROOM}), _sent)
        self.assertEqual(len(app.calls), 1)
        self.assertEqual(self.forwarded, [])


class TestFromEnv(unittest.TestCase):
    def setUp(self):
        import os

        self._env = dict(os.environ)
        configure_owner_forwarding(None)
        chat_owner._env_checked = False

    def tearDown(self):
        import os

        os.environ.clear()
        os.environ.update(self._env)
        configure_owner_forwarding(None)

    def test_꺼져_있으면_None(self):
        import os

        os.environ.pop("CHAT_OWNER_FORWARDING", None)
        self.assertIsNone(chat_owner.owner_forwarding_from_env())

    def test_주소가_없으면_켜지_않는다(self):
        import os

        os.environ.update({"CHAT_OWNER_FORWARDING": "redis", "REDIS_URL": "redis://127.0.0.1:1/0"})
        for k in ("CHAT_OWNER_ADDRESS", "POD_IP"):
            os.environ.pop(k, None)
        self.assertIsNone(chat_owner.owner_forwarding_from_env())

    def test_POD_IP_와_PORT_로_주소를_만든다(self):
        import os

        os.environ.update({"CHAT_OWNER_FORWARDING": "memory", "POD_IP": "10.1.2.3", "PORT": "8888"})
        os.environ.pop("CHAT_OWNER_ADDRESS", None)
        fw = chat_owner.owner_forwarding_from_env()
        self.assertIsNotNone(fw)
        self.assertEqual(fw.address, "http://10.1.2.3:8888")


if __name__ == "__main__":
    unittest.main()
