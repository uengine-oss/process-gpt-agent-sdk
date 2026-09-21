"""산출물 레코드 — 서버가 만들고 화면이 읽는 파일 한 건의 표현.

에이전트마다 다른 모양으로 파일을 넘기던 것을 하나로 모은 계약이다. 레코드는 두 벌이다.

  * 아티팩트 레코드(`build_artifact`) — 서버 안에서 돌아다니는 정본. 식별자와 해시를 갖는다.
  * 다운로드 레코드(`download_file`) — `chats.messages.pdfFiles` 에 저장돼 화면이 읽는 모양.

둘을 나누는 이유는 화면 계약이 오래된 이름(`fileUrl`·`pdfFiles`)을 쓰고 있어서다. 서버 쪽
이름을 그 모양에 맞추면 새 필드를 넣을 때마다 화면 사정을 따라가야 한다.
"""

from __future__ import annotations

from typing import Any


CONTENT_TYPES: dict[str, str] = {
    "hwp": "application/x-hwp",
    "hwpx": "application/hwp+zip",
    "doc": "application/msword",
    "docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    "pdf": "application/pdf",
    "xlsx": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    "pptx": "application/vnd.openxmlformats-officedocument.presentationml.presentation",
    "csv": "text/csv",
    "json": "application/json",
    "txt": "text/plain",
    "md": "text/markdown",
    "png": "image/png",
    "jpg": "image/jpeg",
    "jpeg": "image/jpeg",
    "markdown": "text/markdown",
}

# 뷰어는 미리보기가 있어야 열린다. HWP·HWPX 는 파일 자체가 미리보기다(브라우저가 직접 그린다).
NATIVE_PREVIEW_KINDS = frozenset({"hwp", "hwpx"})

# 다운로드 레코드로 함께 넘기는 값들. 화이트리스트인 이유는 서버 내부 사정(로컬 경로,
# 업로드 재시도 횟수 같은 것)이 화면까지 새지 않게 하기 위해서다. 여기 없는 키는 버려진다.
_PASS_THROUGH = (
    "file_id", "artifact_id", "sha256", "derived_from", "derived_from_sha256",
    "doc_version", "turn_id", "size_bytes",
    # 표시 계약. 빠져 있으면 서버가 만든 미리보기·판정이 화면까지 못 가서,
    # 초안이 '작성 중' 표시 없이 완성본처럼 보인다.
    "view", "status", "draft", "quality_gate", "quality_gate_detail",
    # 서명 URL 은 만료된다. 화면이 다시 발급받을 수 있게 만료 시각을 함께 넘긴다.
    "url_expires_at",
)


def preview_file() -> dict[str, str]:
    return {"kind": "file"}


def build_artifact(
    kind: str,
    file_name: str,
    file_url: str,
    *,
    content_type: str = "",
    **metadata: Any,
) -> dict[str, Any]:
    """산출물 한 건의 아티팩트 레코드를 만든다."""
    if kind not in CONTENT_TYPES:
        raise ValueError(f"unknown output kind: {kind}")
    result: dict[str, Any] = {
        "artifact_type": kind,
        "file_name": file_name,
        "content_type": content_type or CONTENT_TYPES[kind],
        "file_url": file_url,
    }
    result.update({key: value for key, value in metadata.items() if value not in (None, "")})
    if kind in NATIVE_PREVIEW_KINDS and file_url and not result.get("preview"):
        result["preview"] = preview_file()
    return result


def download_file(item: dict[str, Any]) -> dict[str, Any]:
    """아티팩트 레코드를 화면의 `done.files` 모양으로 옮긴다."""
    url = str(item.get("file_url") or item.get("fileUrl") or "")
    name = str(item.get("file_name") or item.get("fileName") or "")
    result: dict[str, Any] = {
        "url": url,
        "fileUrl": url,
        "name": name,
        "fileName": name,
        "contentType": str(item.get("content_type") or item.get("contentType") or ""),
    }
    for key in _PASS_THROUGH:
        if item.get(key) not in (None, ""):
            result[key] = item[key]
    preview = item.get("preview")
    if isinstance(preview, dict) and preview.get("kind"):
        result["preview"] = dict(preview)
    return result


__all__ = [
    "CONTENT_TYPES",
    "NATIVE_PREVIEW_KINDS",
    "build_artifact",
    "download_file",
    "preview_file",
]
