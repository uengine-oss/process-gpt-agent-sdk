"""산출물 규약 — 되돌리면 사용자가 파일을 못 받는 것들만 시험한다."""

import sys
import unittest
from pathlib import Path
from types import ModuleType
from unittest.mock import patch

# 패키지 __init__ 은 litellm 같은 무거운 의존을 끌어온다. 이 모듈은 그것들과 무관하므로
# 파일에서 직접 읽어 시험한다 — 시험이 설치 환경에 매이지 않게.
_ROOT = Path(__file__).resolve().parent.parent / "processgpt_agent_sdk" / "artifacts"


def _load(name: str) -> ModuleType:
    import importlib.util

    full = f"processgpt_agent_sdk.artifacts.{name}"
    if full in sys.modules:
        return sys.modules[full]
    spec = importlib.util.spec_from_file_location(full, _ROOT / f"{name}.py")
    module = importlib.util.module_from_spec(spec)
    sys.modules[full] = module
    spec.loader.exec_module(module)
    return module


links = _load("links")
records = _load("records")
store_mod = _load("store")
collect = _load("collect")


class LinkTests(unittest.TestCase):
    """답변에 남은 내부 경로를 사용자가 누를 수 있는 것으로 바꾼다."""

    files = [
        {"file_name": "Team Overview.docx", "file_url": "https://s/a.docx"},
        {"file_name": "보고서.docx", "file_url": "https://s/b.docx"},
    ]

    def test_bare_path_becomes_a_link(self):
        """실제 사고: 채팅 답변에 컨테이너 경로만 남아 받을 길이 없었다."""
        out = links.strip_local_paths("파일 경로: /workspace/방/Team Overview.docx", self.files)
        self.assertEqual(out, "파일 경로: [Team Overview.docx](https://s/a.docx)")

    def test_file_name_with_spaces_is_not_cut_short(self):
        """공백에서 끊으면 앞토막만 잡아 링크가 깨진다."""
        self.assertIn("Team Overview.docx](", links.strip_local_paths("/out/Team Overview.docx", self.files))

    def test_wrapped_link_is_replaced_whole(self):
        """경로 글자만 갈면 링크 안에 링크가 생겨 눌러도 아무 데도 가지 않는다."""
        out = links.strip_local_paths("[보고서.docx](sandbox:/work/보고서.docx)", self.files)
        self.assertEqual(out, "[보고서.docx](https://s/b.docx)")

    def test_unknown_file_leaves_no_dead_link(self):
        """짝이 없으면 링크를 만들지 않는다 — 눌러도 안 열리는 것이 더 나쁘다."""
        out = links.strip_local_paths("[없음.docx](/work/없음.docx) 와 /work/또없음.pdf", self.files)
        self.assertNotIn("](", out)
        self.assertIn("없음.docx", out)
        self.assertIn("또없음.pdf", out)

    def test_real_urls_are_left_alone(self):
        """이미 주소인 것을 경로로 보고 갈아 버리면 멀쩡한 링크가 깨진다."""
        for text in (
            "자세한 건 https://example.com/a.pdf 참고",
            "이미 https://s/b.docx 로 올라갔습니다",
        ):
            self.assertEqual(links.strip_local_paths(text, self.files), text)

    def test_relative_paths_in_prose_are_left_alone(self):
        """`app/main.py` 는 산출물이 아니라 코드 이야기다."""
        text = "코드에서 app/main.py 를 고쳤습니다"
        self.assertEqual(links.strip_local_paths(text, self.files), text)

    def test_code_wrapped_path_becomes_a_clickable_link(self):
        """코드 표시 안쪽만 갈면 링크가 코드 블록에 갇혀 눌리지 않는다."""
        out = links.strip_local_paths("경로 `/work/보고서.docx` 확인", self.files)
        self.assertEqual(out, "경로 [보고서.docx](https://s/b.docx) 확인")


class RecordTests(unittest.TestCase):
    def test_download_record_keeps_the_display_contract(self):
        """화이트리스트에서 빠지면 서버가 만든 미리보기·판정이 화면까지 못 간다."""
        item = records.build_artifact(
            "docx", "a.docx", "https://s/a", file_id="artifacts/1", sha256="ab",
            view={"renderer": "pdf"}, status={"value": "passed"}, url_expires_at="2026-01-01T00:00:00Z",
        )
        row = records.download_file(item)
        self.assertEqual(row["fileUrl"], "https://s/a")
        self.assertEqual(row["name"], "a.docx")
        for key in ("file_id", "sha256", "view", "status", "url_expires_at"):
            self.assertIn(key, row)

    def test_hwpx_gets_a_preview_so_the_viewer_opens(self):
        item = records.build_artifact("hwpx", "a.hwpx", "https://s/a")
        self.assertEqual(item["preview"], {"kind": "file"})

    def test_unknown_kind_is_refused(self):
        with self.assertRaises(ValueError):
            records.build_artifact("exe", "a.exe", "https://s/a")


class _FakeStore:
    def __init__(self):
        self.puts = []

    async def put(self, *, tenant_id, conversation_id, path, content_type, index=True):
        self.puts.append((path.name, content_type, index))
        return store_mod.StoredArtifact(
            file_id=f"artifacts/{path.name}", url=f"https://s/{path.name}",
            expires_at="2026-01-01T00:00:00Z",
        )

    async def url_for(self, *, tenant_id, file_id):
        return store_mod.StoredArtifact(file_id=file_id, url=f"https://s/re/{file_id}")


class CollectTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        import tempfile

        self._tmp = tempfile.TemporaryDirectory()
        self.outputs = Path(self._tmp.name)
        self.store = _FakeStore()
        self.collector = collect.ArtifactCollector(self.store)

    def tearDown(self):
        self._tmp.cleanup()

    def write(self, name, body=b"x"):
        path = self.outputs / name
        path.write_bytes(body)
        return path

    async def test_only_this_turns_files_are_collected(self):
        """되돌리면 이어지는 턴마다 같은 파일이 다시 올라가 화면에 여러 번 뜬다."""
        self.write("old.docx")
        before = self.collector.snapshot(self.outputs)
        self.write("new.docx")
        files = await self.collector.collect(
            self.outputs, tenant_id="t", conversation_id="c", before=before,
        )
        self.assertEqual([f["file_name"] for f in files], ["new.docx"])

    async def test_record_carries_identity(self):
        self.write("a.docx", b"hello")
        files = await self.collector.collect(self.outputs, tenant_id="t", conversation_id="c", turn_id="turn-1")
        item = files[0]
        self.assertEqual(item["file_id"], "artifacts/a.docx")
        self.assertEqual(item["size_bytes"], 5)
        self.assertEqual(item["turn_id"], "turn-1")
        self.assertEqual(len(item["sha256"]), 64)
        self.assertEqual(item["url_expires_at"], "2026-01-01T00:00:00Z")

    async def test_non_deliverables_are_not_published(self):
        """소스·실행 파일·빈 파일은 결과물이 아니다."""
        self.write("script.py")
        self.write("empty.docx", b"")
        files = await self.collector.collect(self.outputs, tenant_id="t", conversation_id="c")
        self.assertEqual(files, [])

    async def test_oversized_file_is_skipped(self):
        collector = collect.ArtifactCollector(self.store, max_bytes=4)
        self.write("big.docx", b"12345")
        files = await collector.collect(self.outputs, tenant_id="t", conversation_id="c")
        self.assertEqual(files, [])

    async def test_decorate_can_attach_a_preview(self):
        self.write("a.docx")

        async def decorate(item, path):
            item["view"] = {"renderer": "pdf", "file_name": path.name}

        files = await self.collector.collect(
            self.outputs, tenant_id="t", conversation_id="c", decorate=decorate,
        )
        self.assertEqual(files[0]["view"]["renderer"], "pdf")

    async def test_index_flag_reaches_the_store(self):
        """미리보기까지 색인하면 턴이 몇 분씩 멈춘다."""
        self.write("a.pdf")
        await self.collector.collect(self.outputs, tenant_id="t", conversation_id="c", index=False)
        self.assertEqual(self.store.puts[0][2], False)


class MementoStoreTests(unittest.IsolatedAsyncioTestCase):
    """저장 요청이 비공개 버킷을 요구하는지, 주소 없는 응답을 잡는지."""

    def setUp(self):
        import tempfile

        self._tmp = tempfile.TemporaryDirectory()
        self.path = Path(self._tmp.name) / "a.docx"
        self.path.write_bytes(b"x")

    def tearDown(self):
        self._tmp.cleanup()

    async def _put_with_response(self, payload, status=200):
        import httpx

        captured = {}

        def handler(request: httpx.Request) -> httpx.Response:
            captured["url"] = str(request.url)
            captured["body"] = request.content
            return httpx.Response(status, json=payload)

        transport = httpx.MockTransport(handler)
        store = store_mod.MementoArtifactStore("https://memento")
        with patch.object(
            store, "_client",
            lambda: httpx.AsyncClient(base_url="https://memento", transport=transport),
        ):
            result = await store.put(
                tenant_id="t", conversation_id="c", path=self.path,
                content_type="application/vnd.openxmlformats-officedocument.wordprocessingml.document",
            )
        return result, captured

    async def test_artifacts_are_stored_private(self):
        """되돌리면 계약서 산출물이 주소만 알면 누구나 여는 공개 버킷으로 돌아간다."""
        _, captured = await self._put_with_response(
            {"file_id": "artifacts/x.docx", "file_url": "https://s/x", "url_expires_at": "later"}
        )
        self.assertIn(b'"private": true', captured["body"])
        self.assertIn(b'"room_id": "c"', captured["body"])

    async def test_missing_url_is_an_error(self):
        """주소가 없으면 '만들었다'는 말만 남는다. 조용히 넘기지 않는다."""
        with self.assertRaises(RuntimeError):
            await self._put_with_response({"file_id": "artifacts/x.docx"})

    async def test_reissue_returns_a_fresh_url(self):
        import httpx

        def handler(request: httpx.Request) -> httpx.Response:
            self.assertIn("file_id=artifacts%2Fx.docx", str(request.url))
            return httpx.Response(200, json={"file_id": "artifacts/x.docx", "file_url": "https://s/new", "url_expires_at": "later"})

        transport = httpx.MockTransport(handler)
        store = store_mod.MementoArtifactStore("https://memento")
        with patch.object(
            store, "_client",
            lambda: httpx.AsyncClient(base_url="https://memento", transport=transport),
        ):
            result = await store.url_for(tenant_id="t", file_id="artifacts/x.docx")
        self.assertEqual(result.url, "https://s/new")


if __name__ == "__main__":
    unittest.main()
