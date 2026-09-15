"""대화가 파드보다 오래 사는가.

세션당 파드 배포에서 파드는 유휴 TTL 이 지나면 회수되고 디스크도 같이 사라진다.
대화를 다시 열려면 상태(그 대화를 여는 데 필요한 값)와 파일(transcript·산출물)이
파드 밖에 있어야 한다.

에이전트마다 자기 테이블·자기 버킷을 만들지 않는 것이 요점이다 — 그러면 새
에이전트를 붙일 때마다 채팅 인프라를 다시 만드는 것과 다르지 않다.
"""

import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

from processgpt_agent_sdk.session import (
    SESSION_BUCKET,
    SESSION_TABLE,
    AgentSessionStore,
    ArchivePart,
    SessionArchive,
    session_prefix,
)


class _Table:
    """supabase 클라이언트의 `table(...)` 체인을 흉내낸다."""

    def __init__(self, rows: dict, calls: list):
        self._rows = rows
        self._calls = calls
        self._filters = {}
        self._payload = None
        self._conflict = None

    def select(self, *_a, **_k):
        return self

    def eq(self, column, value):
        self._filters[column] = value
        return self

    def limit(self, _n):
        return self

    def upsert(self, payload, on_conflict=None):
        self._payload = payload
        self._conflict = on_conflict
        return self

    def execute(self):
        if self._payload is not None:
            self._calls.append(("upsert", self._payload, self._conflict))
            key = (
                self._payload["agent_type"],
                self._payload["tenant_id"],
                self._payload["conversation_id"],
            )
            self._rows[key] = self._payload["state"]
            return type("R", (), {"data": [self._payload]})()
        self._calls.append(("select", dict(self._filters)))
        key = (
            self._filters.get("agent_type"),
            self._filters.get("tenant_id"),
            self._filters.get("conversation_id"),
        )
        state = self._rows.get(key)
        data = [{"state": state}] if state is not None else []
        return type("R", (), {"data": data})()


class _Bucket:
    def __init__(self, objects: dict):
        self.objects = objects

    def upload(self, path, body, _options=None):
        self.objects[path] = body

    def download(self, path):
        if path not in self.objects:
            raise FileNotFoundError(path)
        return self.objects[path]

    def list(self, prefix):
        """Storage 의 list 는 비재귀다 — 바로 아래 한 겹만, 디렉터리는 id 없이."""
        head = prefix.rstrip("/") + "/"
        seen = {}
        for key in self.objects:
            if not key.startswith(head):
                continue
            rest = key[len(head):]
            name = rest.split("/", 1)[0]
            seen[name] = "/" in rest
        return [
            {"name": n, "id": None if is_dir else f"id-{n}"}
            for n, is_dir in sorted(seen.items())
        ]


class _Client:
    def __init__(self, rows, calls, objects):
        self._rows, self._calls, self._objects = rows, calls, objects
        self.storage = self

    def table(self, name):
        self._calls.append(("table", name))
        return _Table(self._rows, self._calls)

    def from_(self, bucket):
        self._calls.append(("bucket", bucket))
        return _Bucket(self._objects)


class _Harness:
    def __init__(self):
        self.rows, self.calls, self.objects = {}, [], {}
        self.client = _Client(self.rows, self.calls, self.objects)

    def patches(self):
        return (
            patch("processgpt_agent_sdk.database.get_db_client", return_value=self.client),
            patch("processgpt_agent_sdk.database.initialize_db", return_value=None),
        )


class SessionStoreTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.h = _Harness()
        for p in self.h.patches():
            p.start()
            self.addCleanup(p.stop)

    async def test_에이전트마다_테이블을_만들지_않는다(self):
        """agent_type 은 열 하나다. 새 에이전트가 스키마를 추가하지 않아야 한다."""
        await AgentSessionStore("codex").put("acme", "room-1", {"thread_id": "t1"})
        await AgentSessionStore("deepagents").put("acme", "room-1", {"checkpoint": "c1"})

        tables = {name for kind, name in
                  [(c[0], c[1]) for c in self.h.calls if c[0] == "table"]}
        self.assertEqual(tables, {SESSION_TABLE})

    async def test_담을_내용은_에이전트가_정한다(self):
        await AgentSessionStore("codex").put("acme", "room-1", {"thread_id": "t1", "workspace": "/w"})
        state = await AgentSessionStore("codex").get("acme", "room-1")
        self.assertEqual(state, {"thread_id": "t1", "workspace": "/w"})

    async def test_같은_대화라도_에이전트가_다르면_다른_세션이다(self):
        await AgentSessionStore("codex").put("acme", "room-1", {"thread_id": "t1"})
        self.assertIsNone(await AgentSessionStore("deepagents").get("acme", "room-1"))

    async def test_없는_대화는_None(self):
        self.assertIsNone(await AgentSessionStore("codex").get("acme", "처음"))

    async def test_복합키로_upsert_한다(self):
        """같은 대화를 두 번 저장해도 행이 늘면 안 된다."""
        store = AgentSessionStore("codex")
        await store.put("acme", "room-1", {"thread_id": "t1"})
        await store.put("acme", "room-1", {"thread_id": "t2"})
        conflicts = [c[2] for c in self.h.calls if c[0] == "upsert"]
        self.assertEqual(conflicts, ["agent_type,tenant_id,conversation_id"] * 2)
        self.assertEqual((await store.get("acme", "room-1")), {"thread_id": "t2"})

    async def test_조회_실패는_없음으로_삼지_않는다(self):
        """None 으로 돌려주면 호출부가 새 세션을 시작하고 덮어써, 이어지던 대화가
        조용히 끊긴다."""
        with patch.object(_Table, "execute", side_effect=RuntimeError("boom")):
            with self.assertRaises(RuntimeError):
                await AgentSessionStore("codex").get("acme", "room-1")

    def test_agent_type_없이는_만들_수_없다(self):
        with self.assertRaises(ValueError):
            AgentSessionStore("")


class SessionArchiveTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.h = _Harness()
        for p in self.h.patches():
            p.start()
            self.addCleanup(p.stop)
        self.tmp = TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)

    def _seed(self, where: Path):
        home = where / "home"
        (home / "sessions").mkdir(parents=True)
        (home / "sessions" / "rollout.jsonl").write_text("전사", encoding="utf-8")
        (home / "skills" / "docx").mkdir(parents=True)
        (home / "skills" / "docx" / "SKILL.md").write_text("옛 스킬", encoding="utf-8")
        workspace = where / "workspace"
        workspace.mkdir(parents=True)
        (workspace / "report.txt").write_text("문서", encoding="utf-8")
        return [
            ArchivePart("home", home, exclude=("skills",)),
            ArchivePart("workspace", workspace),
        ]

    async def test_버킷도_에이전트마다_만들지_않는다(self):
        await SessionArchive("codex").save("acme", "room-1", self._seed(self.root / "a"))
        buckets = {c[1] for c in self.h.calls if c[0] == "bucket"}
        self.assertEqual(buckets, {SESSION_BUCKET})
        # 접두사로 갈린다.
        self.assertTrue(
            all(k.startswith(session_prefix("codex", "acme", "room-1")) for k in self.h.objects),
            self.h.objects,
        )

    async def test_기동_때_재생성되는_것은_보관하지_않는다(self):
        await SessionArchive("codex").save("acme", "room-1", self._seed(self.root / "a"))
        self.assertNotIn("skills", "\n".join(self.h.objects))
        self.assertTrue(any("rollout.jsonl" in k for k in self.h.objects))

    async def test_회수된_대화를_새_파드가_되살린다(self):
        await SessionArchive("codex").save("acme", "room-1", self._seed(self.root / "old"))

        new_pod = self.root / "new"
        parts = [
            ArchivePart("home", new_pod / "home", exclude=("skills",)),
            ArchivePart("workspace", new_pod / "workspace"),
        ]
        restored = await SessionArchive("codex").restore("acme", "room-1", parts)

        self.assertEqual(restored, 2)
        self.assertEqual(
            (new_pod / "home" / "sessions" / "rollout.jsonl").read_text(encoding="utf-8"), "전사",
        )
        self.assertEqual((new_pod / "workspace" / "report.txt").read_text(encoding="utf-8"), "문서")

    async def test_이미_있으면_다시_받지_않는다(self):
        parts = self._seed(self.root / "a")
        await SessionArchive("codex").save("acme", "room-1", parts)
        self.assertEqual(await SessionArchive("codex").restore("acme", "room-1", parts), 0)

    async def test_보관_실패는_예외를_내지_않는다(self):
        """답변은 이미 사용자에게 갔다. 여기서 터뜨리면 성공한 턴이 실패로 보인다."""
        parts = self._seed(self.root / "a")
        with patch.object(_Bucket, "upload", side_effect=RuntimeError("boom")):
            self.assertEqual(await SessionArchive("codex").save("acme", "room-1", parts), 0)

    async def test_상한은_대화_전체에_걸린다(self):
        """부분마다 따로 세면 실제 상한이 부분 수만큼 곱해진다."""
        parts = self._seed(self.root / "a")
        await SessionArchive("codex", max_bytes=8).save("acme", "room-1", parts)
        total = sum(len(v) for v in self.h.objects.values())
        self.assertLessEqual(total, 8, self.h.objects)

    async def test_다른_에이전트의_파일을_섞지_않는다(self):
        await SessionArchive("codex").save("acme", "room-1", self._seed(self.root / "a"))

        other = self.root / "b"
        parts = [
            ArchivePart("home", other / "home"),
            ArchivePart("workspace", other / "workspace"),
        ]
        self.assertEqual(await SessionArchive("deepagents").restore("acme", "room-1", parts), 0)


class SafeRelativeTests(unittest.TestCase):
    def test_스토리지_키를_대상_디렉터리_안에_가둔다(self):
        from processgpt_agent_sdk.session import _safe_relative

        self.assertEqual(_safe_relative("a/b.txt"), Path("a/b.txt"))
        for bad in ("../../탈출.txt", "a/../../b", "/etc/passwd", "", "."):
            self.assertIsNone(_safe_relative(bad), bad)
