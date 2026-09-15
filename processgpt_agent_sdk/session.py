"""processgpt_agent_sdk/session.py — 대화 하나가 파드보다 오래 살게 한다.

## 왜 SDK 에 있나

세션당 파드 배포에서 파드는 유휴 TTL 이 지나면 회수되고, 그때 디스크도 같이
사라진다. 대화가 이어지려면 두 가지가 파드 밖에 있어야 한다.

- **세션 상태** — 그 대화를 다시 열 때 필요한 값. 에이전트마다 다르다(codex 는
  thread id 와 워크스페이스 경로, 다른 에이전트는 체크포인트 id 일 수 있다).
- **세션 파일** — transcript, 턴이 만든 산출물.

둘 다 "이 에이전트만의 문제" 가 아니다. 새 에이전트를 붙일 때마다 자기 테이블과
자기 버킷과 자기 동기화 코드를 만들어야 한다면, 채팅 인프라를 다시 만드는 것과
다르지 않다. 그래서 여기에 둔다 — 에이전트는 `agent_type` 만 주고 자기 상태를
dict 로 넣는다.

## 저장 위치

| 무엇 | 어디 |
|---|---|
| 세션 상태 | `agent_sessions` 테이블, 키 `(agent_type, tenant_id, conversation_id)` |
| 세션 파일 | `agent-sessions` 버킷, 접두사 `<agent_type>/<tenant>/<conversation>/<part>/` |

버킷은 **비공개여야 한다.** transcript 는 대화 전문을 담는다. 첨부가 쓰는 공개
버킷에 두면 URL 만 아는 사람이 받아 갈 수 있다. DDL 은 `database_schema.sql` 참고.
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, Optional, Sequence

logger = logging.getLogger(__name__)

SESSION_TABLE = "agent_sessions"
SESSION_BUCKET = "agent-sessions"

# 한 대화를 보관할 때의 상한. 넘으면 거기서 멈추고 경고를 남긴다 — 큰 산출물 하나
# 때문에 턴 종료가 몇 분씩 늘어지면 안 된다.
DEFAULT_MAX_ARCHIVE_BYTES = 256 * 1024 * 1024


def session_prefix(agent_type: str, tenant_id: str, conversation_id: str) -> str:
    return f"{agent_type}/{tenant_id or 'default'}/{conversation_id}"


# ---------------------------------------------------------------------------
# 세션 상태
# ---------------------------------------------------------------------------

class AgentSessionStore:
    """대화를 다시 열 때 필요한 값을 파드 밖에 둔다.

    담는 내용은 에이전트가 정한다 — SDK 는 `state` dict 를 그대로 넣고 뺀다.
    """

    def __init__(self, agent_type: str, *, table: str = SESSION_TABLE) -> None:
        if not agent_type:
            raise ValueError("AgentSessionStore requires an agent_type")
        self.agent_type = agent_type
        self.table = table

    async def get(self, tenant_id: str, conversation_id: str) -> Optional[Dict[str, Any]]:
        """이 대화의 상태. 없으면 None.

        조회에 실패하면 예외를 올린다 — "없음" 으로 돌려주면 호출부가 새 세션을
        시작하고 그 값으로 덮어써, 이어지던 대화가 조용히 끊긴다.
        """
        if not conversation_id:
            return None

        def _call():
            from .database import get_db_client, initialize_db

            initialize_db()
            return (
                get_db_client()
                .table(self.table)
                .select("state")
                .eq("agent_type", self.agent_type)
                .eq("tenant_id", tenant_id or "default")
                .eq("conversation_id", conversation_id)
                .limit(1)
                .execute()
            )

        response = await asyncio.to_thread(_call)
        rows = getattr(response, "data", None) or []
        if not rows or not isinstance(rows[0], dict):
            return None
        state = rows[0].get("state")
        return state if isinstance(state, dict) else {}

    async def put(self, tenant_id: str, conversation_id: str, state: Dict[str, Any]) -> None:
        if not conversation_id:
            return
        payload = {
            "agent_type": self.agent_type,
            "tenant_id": tenant_id or "default",
            "conversation_id": conversation_id,
            "state": state or {},
        }

        def _call():
            from .database import get_db_client, initialize_db

            initialize_db()
            return (
                get_db_client()
                .table(self.table)
                .upsert(payload, on_conflict="agent_type,tenant_id,conversation_id")
                .execute()
            )

        await asyncio.to_thread(_call)


# ---------------------------------------------------------------------------
# 세션 파일
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class ArchivePart:
    """보관할 디렉터리 하나.

    `exclude` 는 최상위 이름으로 거른다. 파드가 기동할 때마다 이미지에서 다시
    만들어지는 것(스킬 사본, 생성된 설정 파일)을 같이 보관하면, 되살아난 옛 사본이
    대화를 옛 이미지에 묶는다.
    """

    name: str
    path: Path
    exclude: tuple = field(default_factory=tuple)


class SessionArchive:
    """대화의 파일을 파드 밖에 둔다. 디스크는 캐시이고 스토리지가 원본이다."""

    def __init__(
        self,
        agent_type: str,
        *,
        bucket: str = SESSION_BUCKET,
        max_bytes: int = DEFAULT_MAX_ARCHIVE_BYTES,
    ) -> None:
        if not agent_type:
            raise ValueError("SessionArchive requires an agent_type")
        self.agent_type = agent_type
        self.bucket = bucket
        self.max_bytes = max_bytes

    def _store(self):
        from .database import get_db_client, initialize_db

        initialize_db()
        return get_db_client().storage.from_(self.bucket)

    # -- 내려받기 ---------------------------------------------------------

    async def restore(
        self, tenant_id: str, conversation_id: str, parts: Sequence[ArchivePart],
    ) -> int:
        """파드에 없는 부분만 되살린다. 되살린 파일 수.

        이미 파일이 있는 부분은 건드리지 않는다 — 같은 파드가 이어서 처리하는 흔한
        경우에 매 턴 다시 받으면 턴 시작이 그만큼 늦어진다. 부분별로 판정하는 이유는
        한쪽만 있는 상태가 실제로 생기기 때문이다(회수 직후 업로드가 먼저 온 경우).
        """
        if not conversation_id:
            return 0
        base = session_prefix(self.agent_type, tenant_id, conversation_id)
        restored = 0
        for part in parts:
            if _has_files(part.path):
                continue
            restored += await asyncio.to_thread(
                self._restore_part, f"{base}/{part.name}", part.path,
            )
        if restored:
            logger.info(
                "세션 복원 %d개 파일 | agent=%s conversation=%s",
                restored, self.agent_type, conversation_id,
            )
        return restored

    def _restore_part(self, prefix: str, target: Path) -> int:
        store = self._store()
        keys = _walk(store, prefix)
        if not keys:
            return 0
        target.mkdir(parents=True, exist_ok=True)
        restored = 0
        for key in keys:
            relative = _safe_relative(key[len(prefix) + 1:])
            if relative is None:
                # 스토리지 키는 신뢰 대상이 아니다. 대상 디렉터리 밖으로 쓰면 안 된다.
                logger.warning("세션 복원에서 거부된 키: %s", key)
                continue
            try:
                body = store.download(key)
            except Exception:
                logger.warning("세션 파일 복원 실패: %s", key, exc_info=True)
                continue
            path = target / relative
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(body)
            restored += 1
        return restored

    # -- 올리기 -----------------------------------------------------------

    async def save(
        self, tenant_id: str, conversation_id: str, parts: Sequence[ArchivePart],
    ) -> int:
        """이 대화의 파일을 올린다. 올린 파일 수.

        실패해도 예외를 내지 않는다 — 방금 끝난 턴의 답변은 이미 사용자에게 갔고
        저장도 됐다. 여기서 터뜨리면 성공한 턴이 실패로 보인다.
        """
        if not conversation_id:
            return 0
        try:
            return await asyncio.to_thread(self._save, tenant_id, conversation_id, parts)
        except Exception:
            logger.warning(
                "세션 보관 실패(무시) | agent=%s conversation=%s",
                self.agent_type, conversation_id, exc_info=True,
            )
            return 0

    def _save(self, tenant_id: str, conversation_id: str, parts: Sequence[ArchivePart]) -> int:
        store = self._store()
        base = session_prefix(self.agent_type, tenant_id, conversation_id)
        # 상한은 대화 하나 전체에 건다. 부분마다 따로 세면 실제 상한이 부분 수만큼
        # 곱해진다.
        budget = self.max_bytes
        uploaded = 0
        for part in parts:
            for relative, body in _collect(part, budget):
                try:
                    store.upload(
                        f"{base}/{part.name}/{relative}",
                        body,
                        {"content-type": "application/octet-stream", "x-upsert": "true"},
                    )
                    uploaded += 1
                    budget -= len(body)
                except Exception:
                    logger.warning(
                        "세션 파일 보관 실패: %s/%s/%s", base, part.name, relative, exc_info=True,
                    )
        if uploaded:
            logger.info(
                "세션 보관 %d개 파일 | agent=%s conversation=%s",
                uploaded, self.agent_type, conversation_id,
            )
        return uploaded


# ---------------------------------------------------------------------------
# 내부
# ---------------------------------------------------------------------------

def _collect(part: ArchivePart, budget: int):
    """올릴 (상대경로, 내용). 상한을 넘으면 거기서 멈춘다."""
    if not part.path.is_dir() or budget <= 0:
        return
    total = 0
    # 정렬해 두면 상한에 걸려도 매번 같은 것이 올라간다.
    for path in sorted(part.path.rglob("*")):
        if not path.is_file() or path.is_symlink():
            continue
        relative = path.relative_to(part.path)
        if relative.parts and relative.parts[0] in part.exclude:
            continue
        size = path.stat().st_size
        if total + size > budget:
            logger.warning(
                "세션 보관 상한(%d bytes) 초과 — %s 이후를 건너뛴다", budget, relative,
            )
            return
        total += size
        yield relative.as_posix(), path.read_bytes()


def _walk(store: Any, prefix: str) -> list:
    """접두사 하위 모든 객체 키. Storage 의 list 는 비재귀라 직접 내려간다."""
    keys: list = []
    pending = [prefix]
    while pending:
        current = pending.pop()
        try:
            entries = store.list(current)
        except Exception:
            logger.warning("세션 목록 조회 실패: %s", current, exc_info=True)
            return keys
        if not isinstance(entries, list):
            continue
        for entry in entries:
            if not isinstance(entry, dict):
                continue
            name = entry.get("name")
            if not name:
                continue
            child = f"{current}/{name}"
            # Storage 의 list 는 디렉터리를 id 없는 항목으로 돌려준다.
            if entry.get("id") is None:
                pending.append(child)
            else:
                keys.append(child)
    return keys


def _has_files(path: Path) -> bool:
    return path.is_dir() and any(p.is_file() for p in path.rglob("*"))


def _safe_relative(relative: str) -> Optional[Path]:
    """스토리지 키에서 온 상대경로를 대상 디렉터리 안에 가둔다."""
    if not relative:
        return None
    parts = []
    for part in Path(relative).parts:
        if part in ("", ".") or part == ".." or Path(part).is_absolute():
            return None
        parts.append(part)
    return Path(*parts) if parts else None
