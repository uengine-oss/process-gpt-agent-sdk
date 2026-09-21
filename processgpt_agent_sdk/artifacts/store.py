"""산출물을 어디에 두고 어떤 주소로 내주는가.

에이전트는 파일을 만들 뿐이고, 그것을 보관하고 주소를 발급하는 일은 서버가 한다. 그 일을
하는 쪽을 여기서 하나의 계약(`ArtifactStore`)으로 못박는다. 새 에이전트를 붙일 때 다시
정하지 않아도 되는 것이 이 계약의 값이다.

기본 구현은 Memento 를 거친다. 산출물이 색인까지 함께 타야 다음 턴이 그 문서를 이름이 아니라
식별자로 다시 집을 수 있기 때문이다. 주소는 만료되는 서명 주소다 — 산출물은 비공개 버킷에 있다.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

import httpx

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class StoredArtifact:
    """보관된 산출물 하나.

    `url` 은 만료된다. `expires_at` 이 비어 있으면 만료가 없다는 뜻이다(옛 공개 버킷).
    """

    file_id: str
    url: str
    expires_at: str = ""


class ArtifactStore(Protocol):
    """산출물 보관소."""

    async def put(
        self,
        *,
        tenant_id: str,
        conversation_id: str,
        path: Path,
        content_type: str,
        index: bool = True,
    ) -> StoredArtifact:
        """파일 하나를 보관하고 받을 수 있는 주소를 돌려준다.

        `index=False` 는 색인을 건너뛴다. 미리보기처럼 사람이 한 번 보고 마는 부산물에 쓴다 —
        운영에서 미리보기 PDF 를 색인하다가 턴이 5분씩 멈춘 적이 있다.
        """
        ...

    async def url_for(self, *, tenant_id: str, file_id: str) -> StoredArtifact:
        """이미 보관된 산출물의 주소를 다시 발급한다."""
        ...


class MementoArtifactStore:
    """Memento 를 거쳐 비공개 버킷에 보관한다."""

    def __init__(self, base_url: str, *, timeout: float = 300.0) -> None:
        self._base_url = base_url.rstrip("/")
        self._timeout = timeout

    def _client(self) -> httpx.AsyncClient:
        return httpx.AsyncClient(base_url=self._base_url, timeout=self._timeout)

    async def put(
        self,
        *,
        tenant_id: str,
        conversation_id: str,
        path: Path,
        content_type: str,
        index: bool = True,
    ) -> StoredArtifact:
        options = {
            "room_id": conversation_id,
            # 비공개 버킷에 넣고 서명 주소로만 내보내라는 표시.
            "private": True,
        }
        if not index:
            options["raw_only"] = True

        async with self._client() as client:
            response = await client.post(
                "/save-to-storage",
                data={"tenant_id": tenant_id, "options": json.dumps(options, ensure_ascii=False)},
                files={"file": (path.name, path.read_bytes(), content_type)},
            )
            response.raise_for_status()
            data = response.json()

        if not isinstance(data, dict):
            raise RuntimeError(f"unexpected save-to-storage response for {path.name}")
        file_id = str(data.get("file_id") or data.get("file_path") or "")
        url = str(data.get("file_url") or data.get("signed_url") or data.get("public_url") or "")
        if not url:
            # 주소 없는 산출물은 만들어졌다는 말만 남는다. 조용히 넘기지 않는다.
            raise RuntimeError(f"no download url returned for {path.name}")
        return StoredArtifact(file_id=file_id, url=url, expires_at=str(data.get("url_expires_at") or ""))

    async def url_for(self, *, tenant_id: str, file_id: str) -> StoredArtifact:
        async with self._client() as client:
            response = await client.get(
                "/artifact-url", params={"tenant_id": tenant_id, "file_id": file_id},
            )
            response.raise_for_status()
            data = response.json()

        if not isinstance(data, dict) or not data.get("file_url"):
            raise RuntimeError(f"no download url returned for {file_id}")
        return StoredArtifact(
            file_id=str(data.get("file_id") or file_id),
            url=str(data["file_url"]),
            expires_at=str(data.get("url_expires_at") or ""),
        )


__all__ = ["ArtifactStore", "MementoArtifactStore", "StoredArtifact"]
