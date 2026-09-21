"""답변 본문에 남은 서버 내부 경로를 산출물 링크로 바꾼다.

에이전트는 자기가 만든 파일을 자기가 본 경로로 부른다.

    작성했습니다. 파일 경로: /workspace/.bpmn/<방>/안내문.docx
    [보고서.docx](sandbox:/work/outputs/보고서.docx)

사용자에게 이 경로는 아무 뜻이 없다. **만들었다는 말만 있고 받을 길이 없다.** 배포에서는
서버 내부 경로가 그대로 새기도 한다.

그래서 경로를 지우거나 링크로 바꾼다. 무엇으로 바꿀지는 **이번 턴이 실제로 등록한 산출물**
에서만 찾는다 — 파일 이름으로 맞춘다. 경로 문자열을 믿지 않는 이유는, 그 경로를 쓰는 주체가
에이전트라서다. 형식이 바뀔 때마다 규칙을 고치는 쪽이 아니라, 서버가 아는 사실(무엇을
올렸는가)에 맞추는 쪽이 오래 간다.

짝이 없으면 링크를 만들지 않는다. 눌렀는데 아무것도 안 나오는 것이 경로가 보이는 것보다 나쁘다.
"""

from __future__ import annotations

import re
from pathlib import PurePath
from typing import Any, Iterable

# [라벨](대상) — 대상에 닫는 괄호가 없는 일반적인 경우만 다룬다.
_MD_LINK = re.compile(r"\[([^\]\n]*)\]\(\s*([^)\s]+)\s*\)")

_WINDOWS_DRIVE = re.compile(r"^[A-Za-z]:[\\/]")
# 에이전트가 워크스페이스 파일을 가리킬 때 쓰는 스킴. 실제 대상은 컨테이너 안의 경로다.
_LOCAL_SCHEMES = ("file://", "sandbox:")

# 링크로 감싸이지 않은 맨 경로.
#
# 파일 이름에는 공백이 들어간다("Team Overview.docx"). 공백에서 끊으면 앞토막만 잡아
# 링크가 깨진다 — 실제로 그랬다. 그래서 공백은 허용하고, 줄바꿈과 따옴표·괄호처럼 글 속에서
# 경로가 끝났다고 볼 만한 것에서만 끊는다. 뒤에 글자·숫자·점이 이어지면 아직 끝이 아니다.
# 앞이 글자·`/`·`:`·`.` 면 경로의 시작이 아니다. 이 울타리가 없으면 `https://호스트/a.pdf`
# 의 뒷부분과 `app/main.py` 같은 상대경로까지 경로로 보고 갈아 버린다.
_BARE_PATH = re.compile(
    r"(?<![\w:/.~-])(?:sandbox:)?"
    # 중간 폴더에는 공백을 허용하지 않고, 마지막 파일 이름에만 허용한다. 폴더까지 공백을
    # 허용하면 `/a/b.docx, /a/c.docx` 가 한 덩어리로 잡히고, 아예 막으면 `Team Overview.docx`
    # 가 앞토막만 잡힌다.
    r"/(?:[^\s/\"'`)\]\r\n]+/)*"
    r"[^/\r\n\"'`)\]]*?\.[A-Za-z0-9]{1,5}(?![.\w])"
)


# 코드 표시로 감싼 경로. 안쪽만 갈아 끼우면 링크가 코드 블록 안에 들어가 눌리지 않는다.
_CODE_WRAPPED = re.compile(r"`\s*((?:sandbox:)?/[^`\r\n]+?)\s*`")


def _clean(target: str) -> str:
    value = target.strip().strip("<>").strip('"')
    for scheme in _LOCAL_SCHEMES:
        if value.lower().startswith(scheme):
            return value[len(scheme):]
    return value


def is_local_path(target: str) -> bool:
    """이 대상이 서버 안에서만 뜻이 있는 경로인가."""
    value = target.strip().strip("<>").strip('"')
    if not value:
        return False
    lowered = value.lower()
    if lowered.startswith(_LOCAL_SCHEMES):
        return True
    if value.startswith("\\\\"):
        return True
    if _WINDOWS_DRIVE.match(value):
        return True
    # http(s)/mailto 등 스킴이 있으면 로컬이 아니다.
    if "://" in value or lowered.startswith("mailto:"):
        return False
    return value.startswith("/")


def file_name_of(target: str) -> str:
    """경로 끝의 파일 이름. 산출물과 맞춰 볼 열쇠이자 링크에 보일 글자다."""
    return PurePath(_clean(target).replace("\\", "/")).name


def _download_url(item: Any) -> str:
    if not isinstance(item, dict):
        return ""
    for key in ("file_url", "fileUrl", "url"):
        value = item.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    return ""


def names_to_urls(files: Iterable[Any] | None) -> dict[str, str]:
    """산출물 목록을 {파일이름(소문자): 주소} 로. 같은 이름이 둘이면 먼저 것이 이긴다."""
    mapping: dict[str, str] = {}
    for item in files or []:
        if not isinstance(item, dict):
            continue
        url = _download_url(item)
        if not url:
            continue
        for key in ("file_name", "fileName", "name"):
            value = item.get(key)
            if isinstance(value, str) and value.strip():
                mapping.setdefault(value.strip().casefold(), url)
    return mapping


def strip_local_paths(content: str, files: Iterable[Any] | None = None) -> str:
    """본문의 내부 경로를 산출물 주소로 바꾸거나, 짝이 없으면 이름만 남긴다."""
    if not content:
        return content
    by_name = names_to_urls(files)

    def replace_link(match: re.Match[str]) -> str:
        label, target = match.group(1), match.group(2)
        if not is_local_path(target):
            return match.group(0)
        url = by_name.get(file_name_of(target).casefold())
        if url:
            return f"[{label}]({url})"
        # 대응하는 산출물이 없으면 링크를 풀어 글자만 남긴다 —
        # 죽은 링크를 남기면 사용자가 계속 눌러 본다.
        return label or file_name_of(target)

    def replace_code(match: re.Match[str]) -> str:
        target = match.group(1)
        if not is_local_path(target):
            return match.group(0)
        name = file_name_of(target)
        url = by_name.get(name.casefold())
        return f"[{name}]({url})" if url else f"`{name}`"

    content = _CODE_WRAPPED.sub(replace_code, content)

    # 링크를 먼저 처리한다. 맨 경로를 먼저 바꾸면 링크 안쪽만 갈려서
    # `[이름](주소)` 가 `[이름](https://…)` 이 아니라 링크 안의 링크가 된다.
    out = _MD_LINK.sub(replace_link, content)

    def replace_bare(match: re.Match[str]) -> str:
        target = match.group(0)
        if not is_local_path(target):
            return target
        name = file_name_of(target)
        url = by_name.get(name.casefold())
        return f"[{name}]({url})" if url else name

    return _BARE_PATH.sub(replace_bare, out)


__all__ = [
    "file_name_of",
    "is_local_path",
    "names_to_urls",
    "strip_local_paths",
]
