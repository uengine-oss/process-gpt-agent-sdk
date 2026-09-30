"""processgpt_agent_sdk/steering.py — 실행 중인 턴의 방향을 바꾸는 표준 동작.

## 무엇을 푸는가

턴이 도는 동안 사용자가 "아, 그게 아니라" 를 발견했을 때, 지금까지의 맥락을
버리고 처음부터 다시 시키는 것 말고는 방법이 없었다. 중지(`/chat/stop`)는 실행을
죽이고, 새 메시지는 그 턴을 대체(supersede)하므로 진행 중이던 작업이 사라진다.

스티어링은 **맥락을 유지한 채 지시만 바꾸는** 동작이다. 사용자는 긴 작업을
자율적으로 돌려 놓고, 필요할 때만 개입한다.

## 왜 SDK 가 소유하나

특정 에이전트의 기능이 아니라 표준 동작이어야, 어떤 에이전트를 붙여도 화면과
프로토콜이 같아진다. 그래서 SDK 는

  - 요청 형태(`action: "steer"` 또는 `/chat/steer`),
  - 접수 대기열(`SteeringInbox`),
  - 이벤트 두 개(`steer_accepted` / `steer_applied`)

까지를 소유한다. **실제 실행 전환은 에이전트별 어댑터가 맡는다** — 지시를 언제
집어넣어야 안전한지는 런타임마다 다르고, SDK 는 그 사정을 알 수 없다. 그래서
공개 계약에는 특정 런타임 용어(체크포인트·그래프·인터럽트 같은 말)가 하나도
나오지 않는다.

## 접수와 반영은 다른 이벤트다

`steer_accepted` 는 "받았다" 일 뿐이다. 그 시점의 에이전트는 아직 원래 지시대로
도구를 돌리고 있다. 지시가 실제로 다음 판단에 들어간 순간에 비로소
`steer_applied` 가 나간다. 이 둘을 한 이벤트로 합치면 화면은 접수만으로 "반영
완료" 를 표시하게 되고, 사용자는 반영되지 않은 결과를 반영된 것으로 읽는다.

## 상황별 처리

도구 실행 중
    접수만 하고 대기열에 넣는다. 도구를 중간에 끊지 않는다 — 쓰다 만 파일이나
    반쯤 끝난 외부 호출을 남기는 편이 방향이 조금 늦게 바뀌는 것보다 나쁘다.
    어댑터가 다음 안전 지점(도구 종료 후, 다음 모델 호출 전)에서 집어 간다.

완료 직전
    턴이 마무리에 들어가면 어댑터가 `close()` 로 대기열을 닫는다. 그 뒤에 온
    지시는 `no_active_turn` 으로 거절된다 — 받아 두고 아무 데도 반영하지 않는
    것(조용한 유실)보다 거절이 정직하다. 닫기 직전에 이미 들어와 있던 지시는
    어댑터가 마지막으로 한 번 더 집어 가므로 유실되지 않는다.

중복 연속 수신
    같은 문장이 연속으로 오면(더블 클릭 등) 두 번째부터는 대기열에 쌓지 않고
    `duplicate: true` 와 **처음 접수의 id** 를 그대로 돌려준다. 이벤트도 다시
    내보내지 않는다. 다른 문장이 오면 연속이 끊긴 것으로 보고 다시 접수한다.

사람 확인(HITL) 대기 중
    돌고 있는 실행이 없으므로 방향을 바꿔 넣을 곳이 없다. 어댑터가
    `awaiting_human_input` 으로 거절하고, 사용자는 그 질문에 답하는 기존 경로로
    방향을 바꾼다(답변 자체가 이미 방향 전환이다).

재접속
    접수·반영 이벤트는 다른 이벤트와 같은 큐로 나가므로 레지스트리에 남고,
    재접속한 클라이언트도 똑같이 받는다. 접수만 되고 아직 반영되지 않은 지시는
    재접속 스냅샷에 `pending_steers` 로 함께 실린다.

## 미지원 에이전트

`steer()` 를 구현하지 않은 Executor 에 보내면 `unsupported`(501) 다. 표준 동작을
정의하는 것과 모든 에이전트가 그것을 할 수 있다고 주장하는 것은 다르다.
"""

from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Tuple
from uuid import uuid4

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# 공개 계약: 동작 유형 · 이벤트 · 거절 사유
# ---------------------------------------------------------------------------

# 채팅 요청 본문의 `action`. 없거나 아래 "평범한" 값이면 종전대로 새 턴을 돌린다.
ACTION_STEER = "steer"
NORMAL_ACTIONS = frozenset({"", "message", "send", "start", "chat"})

# 이벤트 두 개를 반드시 구분한다 — 위 "접수와 반영은 다른 이벤트다" 참고.
EVENT_STEER_ACCEPTED = "steer_accepted"
EVENT_STEER_APPLIED = "steer_applied"

REASON_EMPTY_MESSAGE = "empty_message"
REASON_UNSUPPORTED = "unsupported"
REASON_NO_ACTIVE_TURN = "no_active_turn"
REASON_AWAITING_HUMAN_INPUT = "awaiting_human_input"
REASON_FORBIDDEN = "forbidden"
# 어댑터가 판정 중에 터진 경우. 미지원(`unsupported`)과 구분한다 — 구현은 있는데
# 그 순간 판정을 못 한 것이므로, 화면은 재시도를 권할 수 있다.
REASON_STEER_FAILED = "steer_failed"


def is_normal_action(action: Any) -> bool:
    """이 요청이 `action` 없는(=종전대로 실행되는) 평범한 채팅 요청인가."""
    return str(action or "").strip().lower() in NORMAL_ACTIONS


@dataclass(frozen=True)
class SteerDirective:
    """접수된 수정 지시 한 건.

    `turn_active` 는 SDK 가 판정해 어댑터에게 알려주는 값이다. 어댑터는 이 값과
    자기 런타임 상태를 함께 보고 거절 사유를 더 정확하게 만들 수 있다(대표적으로
    사람 확인 대기 중).
    """

    conversation_id: str
    message: str
    directive_id: str = field(default_factory=lambda: uuid4().hex)
    turn_active: bool = False
    tenant_id: str = ""
    user_uid: str = ""
    metadata: Dict[str, Any] = field(default_factory=dict)
    received_at: float = field(default_factory=time.time)

    def event_payload(self, event_type: str) -> Dict[str, Any]:
        return {
            "type": event_type,
            "directive_id": self.directive_id,
            "content": self.message,
        }


@dataclass(frozen=True)
class SteerResult:
    """`steer()` 의 결과. HTTP 응답 본문과 상태코드가 여기서 나온다."""

    accepted: bool
    reason: str = ""
    directive_id: str = ""
    duplicate: bool = False
    status_code: int = 200
    detail: str = ""

    @classmethod
    def ok(cls, directive: SteerDirective, *, duplicate: bool = False) -> "SteerResult":
        return cls(accepted=True, directive_id=directive.directive_id, duplicate=duplicate)

    @classmethod
    def reject(cls, reason: str, *, status_code: int = 409, detail: str = "") -> "SteerResult":
        return cls(accepted=False, reason=reason, status_code=status_code, detail=detail)

    def payload(self) -> Dict[str, Any]:
        out: Dict[str, Any] = {"accepted": self.accepted}
        if self.directive_id:
            out["directive_id"] = self.directive_id
        if self.duplicate:
            out["duplicate"] = True
        if self.reason:
            out["reason"] = self.reason
        if self.detail:
            out["detail"] = self.detail
        return out


# ---------------------------------------------------------------------------
# 접수 대기열
# ---------------------------------------------------------------------------

def _normalize(text: str) -> str:
    return " ".join((text or "").strip().split()).lower()


@dataclass
class _Box:
    pending: List[SteerDirective] = field(default_factory=list)
    last_text: str = ""
    last_directive_id: str = ""
    closed: bool = False
    # 이 대기열을 연 턴의 표식. 취소가 늦게 끝난 이전 턴이 뒤늦게 close() 를 불러
    # **다음 턴의** 대기열을 닫아 버리는 것을 막는다.
    token: str = ""


class SteeringInbox:
    """conversation_id 별 수정 지시 대기열(프로세스 로컬).

    `chat_registry` 의 두 레지스트리와 같은 이유로 프로세스 로컬 dict 이 기본
    구현이다 — 한 방의 요청이 항상 같은 프로세스로 오는 배포에서는 이게 정확한
    구현이고, 아무 파드에나 떨어지는 배포에서는 `set_steering_inbox()` 로 공유
    상태 구현을 끼운다.
    """

    def __init__(self) -> None:
        self._boxes: Dict[str, _Box] = {}
        self._lock = asyncio.Lock()

    # -- 접수 쪽 -----------------------------------------------------------

    async def accept(self, directive: SteerDirective) -> Tuple[bool, Optional[SteerDirective]]:
        """지시를 대기열에 넣는다.

        Returns:
            (새로 접수했는가, 클라이언트에게 알려줄 지시).
            - 중복 연속 수신이면 (False, 처음 접수한 지시) — id 가 같아야 화면이 같은
              접수로 본다.
            - 대기열이 닫혀 있으면 (False, None). 마무리에 들어간 턴이므로 받을 수 없다.
              호출한 쪽은 이것을 `no_active_turn` 으로 답한다.
        """
        cid = directive.conversation_id
        text = _normalize(directive.message)
        async with self._lock:
            box = self._boxes.setdefault(cid, _Box())
            if box.closed:
                return False, None
            for existing in box.pending:
                if _normalize(existing.message) == text:
                    return False, existing
            if text and text == box.last_text:
                # 대기열에서는 이미 빠져나갔지만 바로 직전에 받은 것과 같은 문장이다.
                # 처음 접수의 id 를 그대로 돌려준다 — 화면이 같은 접수로 보게 해야 한다.
                return False, SteerDirective(
                    conversation_id=cid,
                    message=directive.message,
                    directive_id=box.last_directive_id or directive.directive_id,
                )
            box.pending.append(directive)
            box.last_text = text
            box.last_directive_id = directive.directive_id
            return True, directive

    def is_closed(self, conversation_id: str) -> bool:
        box = self._boxes.get(conversation_id)
        return bool(box and box.closed)

    # -- 소비 쪽(어댑터) ---------------------------------------------------

    async def take(self, conversation_id: str) -> List[SteerDirective]:
        """대기 중인 지시를 모두 집어 간다(대기열에서 제거).

        어댑터가 안전 지점에서 부른다. 집어 갔다는 것만으로는 아직 반영이 아니다 —
        실제로 다음 판단에 넣은 뒤 `mark_applied()` 로 알린다.
        """
        async with self._lock:
            box = self._boxes.get(conversation_id)
            if box is None or not box.pending:
                return []
            taken, box.pending = box.pending, []
            return taken

    def pending(self, conversation_id: str) -> List[SteerDirective]:
        """접수됐지만 아직 집어 가지 않은 지시(재접속 스냅샷용)."""
        box = self._boxes.get(conversation_id)
        return list(box.pending) if box else []

    # -- 턴 경계 ----------------------------------------------------------

    async def open(self, conversation_id: str, *, token: str = "") -> None:
        """새 턴을 시작하며 대기열을 비우고 연다."""
        if not conversation_id:
            return
        async with self._lock:
            self._boxes[conversation_id] = _Box(token=token)

    async def close(self, conversation_id: str, *, token: str = "") -> List[SteerDirective]:
        """대기열을 닫고, 남아 있던 지시를 돌려준다.

        어댑터는 마무리에 들어가기 직전에 이걸 부른다. 닫힌 뒤의 접수 요청은
        `no_active_turn` 으로 거절되므로, 반영할 곳이 없는 지시를 받아 두고
        조용히 버리는 일이 생기지 않는다.

        `token` 을 주면 그 표식으로 열린 대기열만 닫는다 — 취소가 늦게 끝난 이전
        턴이 이미 시작된 다음 턴의 대기열을 닫아 버리지 않게 한다.
        """
        if not conversation_id:
            return []
        async with self._lock:
            box = self._boxes.setdefault(conversation_id, _Box(token=token))
            if token and box.token and box.token != token:
                return []
            box.closed = True
            left, box.pending = box.pending, []
            return left

    async def reopen(self, conversation_id: str) -> None:
        """닫았던 대기열을 다시 연다(표식은 그대로 둔다).

        어댑터가 마무리 직전에 닫았다가, 그때 남아 있던 지시를 반영하러 한 라운드
        더 도는 경우에 쓴다 — 그 라운드 동안에도 새 지시를 받을 수 있어야 한다.
        표식을 유지하므로 턴이 끝날 때 프레임워크가 하는 닫기는 그대로 유효하다.
        """
        if not conversation_id:
            return
        async with self._lock:
            box = self._boxes.get(conversation_id)
            if box is not None:
                box.closed = False

    def clear(self) -> None:
        """테스트용."""
        self._boxes.clear()


_inbox: SteeringInbox = SteeringInbox()


def get_steering_inbox() -> SteeringInbox:
    return _inbox


def set_steering_inbox(inbox: SteeringInbox) -> None:
    """공유 상태 백엔드(Redis 등) 구현으로 교체한다."""
    global _inbox
    _inbox = inbox


# ---------------------------------------------------------------------------
# 이벤트 발행
# ---------------------------------------------------------------------------

async def announce_accepted(directive: SteerDirective) -> bool:
    """`steer_accepted` 를 그 턴의 스트림으로 내보낸다. 내보냈으면 True.

    턴의 출력 큐로 넣기 때문에 원래 클라이언트와 재접속한 클라이언트가 같은
    이벤트를 받는다(`chat_registry.ChatRunRegistry.inject`).

    False 는 알릴 스트림이 사라졌다는 뜻이다 — 판정과 접수 사이에 턴이 끝난 경우다.
    """
    return await _inject(directive, EVENT_STEER_ACCEPTED)


async def mark_applied(directive: SteerDirective) -> bool:
    """`steer_applied` 를 내보낸다 — 지시가 실제로 다음 판단에 들어간 순간.

    어댑터가 부른다. 접수 시점에 부르면 안 된다.
    """
    return await _inject(directive, EVENT_STEER_APPLIED)


async def _inject(directive: SteerDirective, event_type: str) -> bool:
    from .chat_registry import get_run_registry

    try:
        return await get_run_registry().inject(
            directive.conversation_id, directive.event_payload(event_type)
        )
    except Exception:
        logger.warning(
            "steering: %s 발행 실패 | conversation_id=%s",
            event_type, directive.conversation_id, exc_info=True,
        )
        return False


def pending_steers_payload(conversation_id: str) -> List[Dict[str, Any]]:
    """재접속 스냅샷에 실을, 아직 반영되지 않은 지시 목록."""
    return [
        {"directive_id": d.directive_id, "content": d.message}
        for d in get_steering_inbox().pending(conversation_id)
    ]
