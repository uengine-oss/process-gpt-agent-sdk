"""워크아이템이 왜 다시 실행되는가 — 실행기에 넘기는 재개 정보.

에이전트가 작업 중에 죽거나(lease 만료로 다른 워커가 재점유), 사람에게 묻고
멈췄다가 답을 받아 다시 집히면, 프레임워크는 중단 지점까지의 기록을 이미 갖고
있다(LangGraph 체크포인트, CLI 세션, Codex rollout). 빠진 것은 "지금이 재개이고
왜 재개인가" 라는 신호였다. 그 신호를 SDK 가 정해서 넘기고, 서비스는 사유에 맞는
프레임워크별 재개 방식 하나만 고른다.

판정 순서
- 점유 RPC 가 돌려준 claim_count > 1 이면 ``reclaim`` (feedback 보다 우선)
- feedback 마지막 항목의 ``kind`` 가 ``human_answer`` 면 사람 답변 재개
- ``revision`` 이거나 ``kind`` 가 없으면(화면이 구분 표시를 남기기 전) 반려 재작업
- 그 밖에는 ``fresh``

사람 답변과 반려는 행 모양이 같다(STARTED, claim_count=1, feedback 1건). 그래서
화면이 답을 저장할 때 feedback 항목에 ``kind`` 를 함께 남긴다.

실행기에는 ``extras["resume"]`` 딕셔너리로 간다. 서비스는 이 모듈을 import 하지
않고 딕셔너리만 읽어도 된다 — 그래야 구버전 SDK 와 짝지어 배포돼도 깨지지 않는다.
"""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass
from typing import Any, Dict, Mapping, Optional

RESUME_FRESH = "fresh"
RESUME_RECLAIM = "reclaim"
RESUME_HUMAN_ANSWER = "human_answer"
RESUME_REVISION = "revision"

#: 화면이 feedback 항목에 남기는 구분 표시(``kind``) 값.
FEEDBACK_KIND_HUMAN_ANSWER = RESUME_HUMAN_ANSWER
FEEDBACK_KIND_REVISION = RESUME_REVISION

_KINDS = (RESUME_FRESH, RESUME_RECLAIM, RESUME_HUMAN_ANSWER, RESUME_REVISION)


@dataclass(frozen=True)
class ResumeInfo:
    """이번 실행이 무엇의 재개인가."""

    kind: str = RESUME_FRESH
    #: 이번이 몇 번째 점유인가(1 부터). 재점유면 2, 3.
    attempt: int = 1
    #: 재개 키. 워크아이템(todo) id — LangGraph thread_id, 세션 저장 키로 쓴다.
    key: str = ""
    #: 사람이 남긴 마지막 입력의 원문. 요약본이 아니다.
    answer_raw: str = ""

    @property
    def is_resume(self) -> bool:
        return self.kind != RESUME_FRESH

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> "ResumeInfo":
        kind = str(data.get("kind") or RESUME_FRESH)
        return cls(
            kind=kind if kind in _KINDS else RESUME_FRESH,
            attempt=_attempt(data.get("attempt")),
            key=str(data.get("key") or ""),
            answer_raw=str(data.get("answer_raw") or ""),
        )


def _attempt(value: Any) -> int:
    try:
        n = int(value)
    except (TypeError, ValueError):
        return 1
    return n if n >= 1 else 1


def _feedback_entries(raw: Any) -> list:
    if isinstance(raw, str):
        try:
            raw = json.loads(raw)
        except (TypeError, ValueError):
            return []
    return raw if isinstance(raw, list) else []


def _last_feedback(raw: Any) -> Optional[Dict[str, Any]]:
    entries = _feedback_entries(raw)
    if not entries:
        return None
    last = entries[-1]
    if isinstance(last, dict):
        return last
    if isinstance(last, str) and last.strip():
        return {"content": last}
    return None


def resume_info_from_row(row: Optional[Mapping[str, Any]]) -> ResumeInfo:
    """점유 RPC 가 돌려준 todolist 행에서 재개 정보를 정한다."""
    row = row or {}
    key = str(row.get("id") or "")
    attempt = _attempt(row.get("claim_count"))

    if attempt > 1:
        return ResumeInfo(kind=RESUME_RECLAIM, attempt=attempt, key=key)

    last = _last_feedback(row.get("feedback"))
    if last is None:
        return ResumeInfo(kind=RESUME_FRESH, attempt=attempt, key=key)

    content = last.get("content")
    answer = content if isinstance(content, str) else ("" if content is None else json.dumps(content, ensure_ascii=False))
    kind = RESUME_HUMAN_ANSWER if last.get("kind") == FEEDBACK_KIND_HUMAN_ANSWER else RESUME_REVISION
    return ResumeInfo(kind=kind, attempt=attempt, key=key, answer_raw=answer)


def resume_info_of(source: Any) -> ResumeInfo:
    """실행 문맥·extras·metadata 어디에서든 재개 정보를 꺼낸다.

    ``resume`` 이 실려 있으면 그것을, 없으면(구버전 SDK) 행으로 같은 규칙을
    적용하고, 행도 없으면 ``fresh`` 다.
    """
    if source is None:
        return ResumeInfo()

    if not isinstance(source, Mapping):
        getter = getattr(source, "get_context_data", None)
        data = getter() if callable(getter) else {}
        extras = (data or {}).get("extras") or {}
        row = (data or {}).get("row") or getattr(source, "row", None) or {}
        source = {**extras, "row": row}

    resume = source.get("resume")
    if isinstance(resume, Mapping):
        return ResumeInfo.from_dict(resume)
    if isinstance(resume, ResumeInfo):
        return resume
    row = source.get("row")
    if isinstance(row, Mapping):
        return resume_info_from_row(row)
    return ResumeInfo()


def continuation_prompt(original_task: str = "") -> str:
    """대화 기록이 남은 에이전트를 재개할 때 넣는 표준 지시.

    2026-10-06 실측에서 같은 지시를 다시 넣으면 모델이 완료한 단계를 반복했고
    (codex 1/2), 이 지시로 바꾸면 반복하지 않았다(0/2, Claude Code 0/2).
    원래 지시는 참고로 붙인다 — 기록이 없어 새로 시작되는 경우에도 할 일은 안다.
    """
    text = (
        "직전 실행이 중단되었습니다. 지금까지의 대화와 작업 공간을 확인하고, "
        "이미 완료한 단계는 다시 하지 말고 완료되지 않은 단계부터 이어서 수행하세요."
    )
    original = (original_task or "").strip()
    if original:
        text += f"\n\n[원래 작업 지시 — 참고용]\n{original}"
    return text
