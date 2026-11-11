"""넘겨주기 통합 — 서버 프로세스 둘이 Redis 하나를 같이 쓴다(파드 둘).

A 에서 시작한 턴에 B 로 들어온 재접속 · 방향 바꾸기 · 같은 방의 새 질문 · 중지가 A 에서
처리되는지, A 가 죽으면 B 가 직접 처리하는지 본다. Redis 가 필요하다:

    docker run -d --rm -p 127.0.0.1:6391:6379 redis:7-alpine
    CHAT_OWNER_TEST_REDIS_URL=redis://127.0.0.1:6391/0 pytest tests/test_chat_owner_processes.py
"""

import asyncio
import json
import os
import socket
import subprocess
import sys
import time
import unittest
import uuid

import httpx

REDIS_URL = os.getenv("CHAT_OWNER_TEST_REDIS_URL", "")
TENANT = "acme"


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _start(port: int) -> subprocess.Popen:
    env = dict(os.environ)
    env.update(
        {
            "CHAT_OWNER_FORWARDING": "redis",
            "CHAT_OWNER_REDIS_URL": REDIS_URL,
            "CHAT_OWNER_ADDRESS": f"http://127.0.0.1:{port}",
            "CHAT_OWNER_TTL_SECONDS": "3",
            "SSE_HEARTBEAT_SECONDS": "1",
            "SUPABASE_URL": env.get("SUPABASE_URL", "http://127.0.0.1:9"),
            "SUPABASE_KEY": env.get("SUPABASE_KEY", "dummy"),
        }
    )
    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    return subprocess.Popen([sys.executable, "-m", "tests.owner_forward_app", str(port)], cwd=root, env=env)


def _wait_up(port: int) -> None:
    deadline = time.time() + 20
    while time.time() < deadline:
        try:
            if httpx.get(f"http://127.0.0.1:{port}/health", timeout=1).status_code == 200:
                return
        except httpx.HTTPError:
            pass
        time.sleep(0.2)
    raise AssertionError(f"서버가 뜨지 않았다: {port}")


class _Stream:
    """SSE 스트림을 백그라운드로 읽어 토큰 본문과 이벤트를 모은다."""

    def __init__(self, client: httpx.AsyncClient, url: str, body: dict):
        self.text = ""
        self.events = []
        self.closed = asyncio.Event()
        self._task = asyncio.create_task(self._read(client, url, body))

    async def _read(self, client, url, body):
        try:
            async with client.stream("POST", url, json=body) as r:
                async for line in r.aiter_lines():
                    if not line.startswith("data:"):
                        continue
                    try:
                        ev = json.loads(line[5:].strip())
                    except ValueError:
                        continue
                    self.events.append(ev)
                    if ev.get("type") in ("token", "snapshot"):
                        self.text += ev.get("content") or ""
        finally:
            self.closed.set()

    async def until(self, pred, timeout=10.0):
        deadline = time.time() + timeout
        while time.time() < deadline:
            if pred(self):
                return
            await asyncio.sleep(0.05)
        raise AssertionError(f"기다린 조건이 오지 않았다 — 본문 {self.text!r} 이벤트 {self.events[-5:]!r}")

    async def close(self):
        self._task.cancel()
        try:
            await self._task
        except BaseException:
            pass


@unittest.skipUnless(REDIS_URL, "CHAT_OWNER_TEST_REDIS_URL 이 없다")
class TestTwoPods(unittest.IsolatedAsyncioTestCase):
    @classmethod
    def setUpClass(cls):
        cls.pa, cls.pb = _free_port(), _free_port()
        cls.a, cls.b = _start(cls.pa), _start(cls.pb)
        _wait_up(cls.pa)
        _wait_up(cls.pb)

    @classmethod
    def tearDownClass(cls):
        for p in (cls.a, cls.b):
            if p.poll() is None:
                p.terminate()
                p.wait(timeout=10)

    async def asyncSetUp(self):
        self.client = httpx.AsyncClient(timeout=httpx.Timeout(30.0, read=None))
        self.room = f"room-{uuid.uuid4().hex[:8]}"
        self.A = f"http://127.0.0.1:{self.pa}"
        self.B = f"http://127.0.0.1:{self.pb}"

    async def asyncTearDown(self):
        await self.client.post(f"{self.A}/chat/stop", json={"conversation_id": self.room, "tenant_id": TENANT})
        await self.client.aclose()

    def _turn(self, base, message="안녕"):
        return _Stream(
            self.client,
            f"{base}/chat/stream",
            {"message": message, "conversation_id": self.room, "tenant_id": TENANT},
        )

    async def test_A_의_턴에_B_로_다시_붙으면_A_의_본문을_받는다(self):
        first = self._turn(self.A)
        await first.until(lambda s: f"port={self.pa}" in s.text)
        attached = _Stream(self.client, f"{self.B}/chat/stream/attach", {"conversation_id": self.room, "tenant_id": TENANT})
        try:
            await attached.until(lambda s: f"port={self.pa}" in s.text)
        finally:
            await attached.close()
            await first.close()

    async def test_B_로_온_방향_바꾸기가_A_의_턴에_반영된다(self):
        first = self._turn(self.A)
        try:
            await first.until(lambda s: f"port={self.pa}" in s.text)
            r = await self.client.post(
                f"{self.B}/chat/steer",
                json={"conversation_id": self.room, "tenant_id": TENANT, "message": "표로 정리해"},
            )
            self.assertEqual(r.status_code, 200, r.text)
            self.assertTrue(r.json().get("accepted"), r.text)
            await first.until(lambda s: "applied:표로 정리해" in s.text)
            self.assertIn("steer_accepted", [e.get("type") for e in first.events])
        finally:
            await first.close()

    async def test_B_로_온_같은_방의_새_질문은_A_가_처리한다(self):
        first = self._turn(self.A)
        await first.until(lambda s: f"port={self.pa}" in s.text)
        second = self._turn(self.B, "다음 질문")
        try:
            await second.until(lambda s: f"port={self.pa}" in s.text)
            # 새 질문이 A 의 앞 턴을 대체했다 — 같은 프로세스 안의 판단이 그대로 돌았다.
            await first.until(lambda s: any(e.get("type") == "error" for e in s.events))
        finally:
            await second.close()
            await first.close()

    async def test_B_로_온_중지가_A_의_턴을_멈춘다(self):
        first = self._turn(self.A)
        await first.until(lambda s: f"port={self.pa}" in s.text)
        r = await self.client.post(f"{self.B}/chat/stop", json={"conversation_id": self.room, "tenant_id": TENANT})
        self.assertEqual(r.status_code, 200, r.text)
        self.assertTrue(r.json().get("stopped"), r.text)
        await first.until(lambda s: s.closed.is_set(), timeout=10)
        self.assertIn("turn cancelled", json.dumps(first.events, ensure_ascii=False))

    async def test_턴이_끝나면_주인_기록이_사라진다(self):
        import redis.asyncio as aioredis

        r = aioredis.from_url(REDIS_URL, decode_responses=True)
        try:
            first = self._turn(self.A)
            await first.until(lambda s: f"port={self.pa}" in s.text)
            self.assertEqual(await r.get(f"chat:owner:{self.room}"), self.A)
            await self.client.post(f"{self.A}/chat/stop", json={"conversation_id": self.room, "tenant_id": TENANT})
            await first.until(lambda s: s.closed.is_set())
            deadline = time.time() + 5
            while time.time() < deadline and await r.get(f"chat:owner:{self.room}") is not None:
                await asyncio.sleep(0.1)
            self.assertIsNone(await r.get(f"chat:owner:{self.room}"))
        finally:
            await r.aclose()


@unittest.skipUnless(REDIS_URL, "CHAT_OWNER_TEST_REDIS_URL 이 없다")
class TestOwnerDies(unittest.IsolatedAsyncioTestCase):
    async def test_주인이_죽으면_B_가_직접_처리한다(self):
        pa, pb = _free_port(), _free_port()
        a, b = _start(pa), _start(pb)
        client = httpx.AsyncClient(timeout=httpx.Timeout(30.0, read=None))
        room = f"room-{uuid.uuid4().hex[:8]}"
        try:
            _wait_up(pa)
            _wait_up(pb)
            first = _Stream(client, f"http://127.0.0.1:{pa}/chat/stream", {"message": "안녕", "conversation_id": room, "tenant_id": TENANT})
            await first.until(lambda s: f"port={pa}" in s.text)
            a.kill()
            a.wait(timeout=10)
            await first.close()

            r = await client.post(f"http://127.0.0.1:{pb}/chat/stream/attach", json={"conversation_id": room, "tenant_id": TENANT})
            self.assertEqual(r.json(), {"active": False}, "닿지 않는 주인 대신 B 가 '진행 중인 턴 없음' 으로 답한다")

            second = _Stream(client, f"http://127.0.0.1:{pb}/chat/stream", {"message": "다시", "conversation_id": room, "tenant_id": TENANT})
            try:
                await second.until(lambda s: f"port={pb}" in s.text)
            finally:
                await client.post(f"http://127.0.0.1:{pb}/chat/stop", json={"conversation_id": room, "tenant_id": TENANT})
                await second.close()
        finally:
            await client.aclose()
            for p in (a, b):
                if p.poll() is None:
                    p.terminate()
                    p.wait(timeout=10)


if __name__ == "__main__":
    unittest.main()
