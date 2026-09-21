"""에이전트는 정해진 자리에 파일을 놓고, 서버가 거둬 간다.

**규약은 디렉터리다.** 이번 턴의 `outputs/` 에 놓인 것만 산출물이다. 답변 본문을 훑어 경로를
찾아내던 방식은 쓰지 않는다 — 그 경로를 적는 주체가 에이전트라서, 문장이 바뀔 때마다 규칙을
고쳐야 하고 못 잡으면 사용자는 파일을 받지 못한다. 디렉터리는 에이전트가 어떻게 말하든 같다.

거둘 때는 **이번 턴에 새로 생기거나 바뀐 것만** 본다(`snapshot`). 그러지 않으면 이어지는 턴마다
같은 파일이 다시 올라가 화면에 같은 것이 여러 번 뜬다.
"""

from __future__ import annotations

import asyncio
import hashlib
import logging
from pathlib import Path
from typing import Any, Awaitable, Callable

from .records import CONTENT_TYPES, build_artifact
from .store import ArtifactStore

logger = logging.getLogger(__name__)

# 내보낼 수 있는 산출물 형식. 여기 없는 것(실행 파일, 소스, 로그)은 결과물이 아니다.
DEFAULT_EXTENSIONS = frozenset({
    "hwp", "hwpx", "doc", "docx", "pdf", "pptx", "xlsx",
    # `markdown` 은 `md` 와 같은 것이다. 둘 다 두는 이유는 에이전트가 어느 쪽으로 저장할지
    # 우리가 정하지 않기 때문이다 — 한쪽만 두면 그 이름으로 저장한 턴의 파일이 조용히 사라진다.
    "csv", "json", "txt", "md", "markdown", "png", "jpg", "jpeg",
})

# 한 건의 상한. 이보다 크면 업로드가 턴을 붙잡고, 화면에서도 받다가 끊긴다.
DEFAULT_MAX_BYTES = 50 * 1024 * 1024

# 파일 한 건의 지문. 바뀌었는지 보는 데 쓴다.
_Snapshot = dict[str, tuple[int, int]]

# 산출물 레코드에 저장소별 사정을 덧붙이는 자리. 미리보기 렌더나 검수 판정처럼
# 에이전트마다 다른 것을 여기서 붙인다.
Decorator = Callable[[dict[str, Any], Path], Awaitable[None]]


def snapshot(outputs_dir: Path) -> _Snapshot:
    """지금 `outputs/` 에 무엇이 어떤 상태로 있는지. 턴을 시작할 때 찍는다."""
    result: _Snapshot = {}
    if not outputs_dir.is_dir():
        return result
    for path in outputs_dir.iterdir():
        if path.is_file():
            stat = path.stat()
            result[str(path.resolve()).casefold()] = (stat.st_size, stat.st_mtime_ns)
    return result


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


class ArtifactCollector:
    """`outputs/` 를 거둬 산출물 레코드로 만든다."""

    def __init__(
        self,
        store: ArtifactStore,
        *,
        extensions: frozenset[str] = DEFAULT_EXTENSIONS,
        max_bytes: int = DEFAULT_MAX_BYTES,
    ) -> None:
        self._store = store
        self._extensions = extensions
        self._max_bytes = max_bytes

    def snapshot(self, outputs_dir: Path) -> _Snapshot:
        return snapshot(outputs_dir)

    def _candidates(self, outputs_dir: Path, before: _Snapshot) -> list[Path]:
        chosen: list[Path] = []
        if not outputs_dir.is_dir():
            return chosen
        for path in sorted(outputs_dir.iterdir()):
            if not path.is_file():
                continue
            stat = path.stat()
            if before.get(str(path.resolve()).casefold()) == (stat.st_size, stat.st_mtime_ns):
                continue  # 이번 턴이 만든 것이 아니다.
            ext = path.suffix.lower().lstrip(".")
            if ext not in self._extensions:
                logger.info("산출물 제외(형식) | %s", path.name)
                continue
            if stat.st_size <= 0:
                logger.info("산출물 제외(빈 파일) | %s", path.name)
                continue
            if stat.st_size > self._max_bytes:
                logger.warning("산출물 제외(크기) | %s | %d bytes", path.name, stat.st_size)
                continue
            chosen.append(path)
        return chosen

    async def collect(
        self,
        outputs_dir: Path,
        *,
        tenant_id: str,
        conversation_id: str,
        before: _Snapshot | None = None,
        turn_id: str = "",
        index: bool = True,
        decorate: Decorator | None = None,
    ) -> list[dict[str, Any]]:
        """이번 턴의 산출물을 보관하고 레코드 목록을 돌려준다.

        Args:
            before:   턴을 시작할 때 찍은 `snapshot`. 없으면 지금 있는 것을 모두 산출물로 본다.
            decorate: 레코드마다 불러 저장소별 표시(미리보기·판정)를 덧붙이는 자리.
        """
        files: list[dict[str, Any]] = []
        for path in self._candidates(outputs_dir, before or {}):
            ext = path.suffix.lower().lstrip(".")
            content_type = CONTENT_TYPES[ext]
            stored = await self._store.put(
                tenant_id=tenant_id,
                conversation_id=conversation_id,
                path=path,
                content_type=content_type,
                index=index,
            )
            item = build_artifact(
                ext, path.name, stored.url, content_type=content_type,
                file_id=stored.file_id,
                url_expires_at=stored.expires_at,
                sha256=await asyncio.to_thread(sha256_file, path),
                size_bytes=path.stat().st_size,
                turn_id=turn_id,
            )
            if decorate is not None:
                await decorate(item, path)
            files.append(item)
        return files


__all__ = [
    "ArtifactCollector",
    "DEFAULT_EXTENSIONS",
    "DEFAULT_MAX_BYTES",
    "Decorator",
    "sha256_file",
    "snapshot",
]
