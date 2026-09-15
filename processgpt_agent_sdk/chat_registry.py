"""processgpt_agent_sdk/chat_registry.py — 진행 중인 채팅 턴 레지스트리.

## 왜 SDK 에 있나

`mount_chat_sse()` 는 요청마다 새 `asyncio.Queue` 와 실행 task 를 만들 뿐,
"이 방에서 지금 돌고 있는 턴" 이라는 개념이 없었다. 그래서

  - 새로고침·방 재진입하면 진행 중인 턴의 결과를 영영 못 받는다(재접속 불가).
  - 프론트의 중지 버튼은 자기 쪽 fetch 만 끊을 뿐, 서버의 실행은 계속 돈다.

두 에이전트 저장소가 각자 이 레지스트리를 다시 만들었다. codex 는
`app/chat/service.py::LiveRun`, deepagents 는 `core/chat/stream_registry.py` 다.
계약(누적 본문 스냅샷 먼저 → 이후 실시간)이 양쪽 동일해서 프레임워크로 올린다.

## 두 레지스트리

`ChatRunRegistry`
    턴의 이벤트를 받아 구독자에게 흘리고, 누적 본문 스냅샷을 들고 있는다.
    이벤트를 전부 버퍼링하지는 않는다 — 긴 턴이면 수만 건이다. 누적 텍스트만
    들고 있다가 재접속 시 스냅샷 1건으로 보낸다.

`InflightRegistry`
    conversation_id 별 실행 task 를 들고 있다가 중지 요청에 취소한다.
    `asyncio.Task.cancel()` 은 그 task 를 만든 프로세스 안에서만 가능하다.

    새 턴이 시작될 때 이전 턴을 어떻게 할지는 두 갈래다 — `supersede()` 로 끊거나,
    `wait_for_previous()` 로 끝나기를 기다리거나. 어느 쪽인지는 Executor 가
    `should_supersede()` 로 알린다(`processgpt_agent_framework.mount_chat_sse` 참고).

## 다중 파드

기본 구현은 프로세스 로컬 dict 다. 한 `conversation_id` 의 요청이 항상 같은
프로세스로 오는 배포(세션당 파드 + thread_id 라우팅)에서는 이게 정확한 구현이다 —
공유 상태가 필요 없고, 취소는 정의상 같은 프로세스 안에서 일어난다.

요청이 아무 파드에나 떨어지는 배포에서는 Redis 등으로 공유하는 구현을 만들어
`set_run_registry()` / `set_inflight_registry()` 로 갈아끼운다 — SDK 는 계약만
소유하고 공유 상태 백엔드는 애플리케이션이 고른다.
"""

from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Tuple

logger = logging.getLogger(__name__)

# 종료된 run 을 바로 지우지 않는 이유: done 이벤트가 나간 직후 재접속한 클라이언트가
# "활성 없음" 대신 마지막 상태를 받을 수 있게 짧게 남겨 둔다.
GRACE_PERIOD_SECONDS = 30.0
# 완료 신호 없이 방치된 run 에 대한 TTL 안전장치(누수 방지).
MAX_RUN_AGE_SECONDS = 30 * 60

DONE_TYPES = ("done", "error")

# 이전 턴을 취소하지 않고 기다릴 때의 상한. HITL 응답이 대표적인 경우인데, 프론트는
# request_human_input **도구 호출 이벤트** 시점에 이미 패널을 그리므로 사용자가 곧바로
# 답하면 이전 턴은 아직 LLM/도구를 돌리는 중일 수 있다. 그 턴이 interrupt 를 체크포인트에
# 남겨야 재개가 되므로 넉넉히 기다린다.
WAIT_PREVIOUS_TIMEOUT_SECONDS = 180.0


@dataclass
class ActiveRun:
    conversation_id: str
    accumulated_text: str = ""
    subscribers: List[asyncio.Queue] = field(default_factory=list)
    done: bool = False
    created_at: float = field(default_factory=time.monotonic)
    done_at: Optional[float] = None


class ChatRunRegistry:
    """진행 중인 턴의 이벤트 pub/sub + 누적 본문 스냅샷(프로세스 로컬)."""

    def __init__(self) -> None:
        self._runs: Dict[str, ActiveRun] = {}

    # -- 턴 생산자 쪽 ------------------------------------------------------

    async def start_run(self, conversation_id: str) -> None:
        """새 턴 시작. 같은 conversation_id 에 남아 있던 이전 run 은 교체한다.

        교체하지 않으면 이전 턴들의 누적 텍스트가 스냅샷에 계속 붙어 남는다.
        """
        if not conversation_id:
            return
        self._sweep()
        self._runs[conversation_id] = ActiveRun(conversation_id=conversation_id)

    async def record(self, conversation_id: str, payload: Dict[str, Any]) -> None:
        """턴이 내보낸 이벤트 1건을 기록하고 구독자에게 전달한다."""
        if not conversation_id or not isinstance(payload, dict):
            return
        run = self._runs.get(conversation_id)
        if run is None:
            return

        if payload.get("type") == "token":
            content = payload.get("content")
            if isinstance(content, str) and content and not run.done:
                run.accumulated_text += content

        for q in list(run.subscribers):
            q.put_nowait(payload)

        if payload.get("type") in DONE_TYPES:
            await self.mark_done(conversation_id)

    async def mark_done(self, conversation_id: str) -> None:
        run = self._runs.get(conversation_id)
        if run is None or run.done:
            return
        run.done = True
        run.done_at = time.monotonic()

    # -- 재접속 쪽 ---------------------------------------------------------

    async def subscribe(self, conversation_id: str) -> Optional[Tuple[asyncio.Queue, str]]:
        """활성 run 에 재접속한다.

        없거나 이미 종료됐으면 None. 있으면 (큐, 지금까지의 누적 본문) 을 준다.
        """
        if not conversation_id:
            return None
        self._sweep()
        run = self._runs.get(conversation_id)
        if run is None or run.done:
            return None
        q: asyncio.Queue = asyncio.Queue()
        run.subscribers.append(q)
        return q, run.accumulated_text

    async def unsubscribe(self, conversation_id: str, q: asyncio.Queue) -> None:
        run = self._runs.get(conversation_id)
        if run is None:
            return
        try:
            run.subscribers.remove(q)
        except ValueError:
            pass

    async def is_active(self, conversation_id: str) -> bool:
        run = self._runs.get(conversation_id)
        return run is not None and not run.done

    # -- 내부 -------------------------------------------------------------

    def _sweep(self) -> None:
        now = time.monotonic()
        stale = [
            cid
            for cid, run in self._runs.items()
            if (run.done and run.done_at is not None and (now - run.done_at) > GRACE_PERIOD_SECONDS)
            or (not run.done and (now - run.created_at) > MAX_RUN_AGE_SECONDS)
        ]
        for cid in stale:
            self._runs.pop(cid, None)

    def clear(self) -> None:
        """테스트용."""
        self._runs.clear()


class InflightRegistry:
    """conversation_id 별 실행 task(프로세스 로컬)."""

    def __init__(self) -> None:
        self._tasks: Dict[str, asyncio.Task] = {}

    def set_inflight(self, conversation_id: str, task: asyncio.Task) -> None:
        if not conversation_id:
            return
        self._tasks[conversation_id] = task
        task.add_done_callback(lambda _t, cid=conversation_id: self._clear_if(cid, _t))

    def get_inflight(self, conversation_id: str) -> Optional[asyncio.Task]:
        task = self._tasks.get(conversation_id)
        if task is not None and task.done():
            self._tasks.pop(conversation_id, None)
            return None
        return task

    async def supersede(self, conversation_id: str) -> bool:
        """새 턴이 시작될 때, 같은 방에 남아 있는 이전 턴을 정리한다.

        `cancel()` 과 분리해 둔 이유: 새 요청이 이전 응답을 **대체**하는 경우와
        **이어가는** 경우가 다르다. 대표적으로 HITL(사람 확인) 응답은 이전 턴이
        남긴 interrupt 를 재개하는 것이라, 그 턴을 취소하면 체크포인트가 사라져
        재개가 불가능해진다. 어느 쪽인지는 요청 본문을 해석해야 알 수 있고 그건
        Executor 의 몫이므로, 그런 서버는 이 메서드를 no-op 으로 재정의하고
        Executor 안에서 직접 판단한다.

        기본 동작은 취소다 — 대부분의 에이전트에서 새 요청은 대체를 뜻한다.
        """
        return await self.cancel(conversation_id)

    async def wait_for_previous(
        self, conversation_id: str, *, timeout: float = WAIT_PREVIOUS_TIMEOUT_SECONDS
    ) -> bool:
        """이전 턴을 **취소하지 않고** 끝나기를 기다린다. 기다린 턴이 있었으면 True.

        `supersede()` 와 짝이다. 새 요청이 이전 턴을 대체하는 게 아니라 이어가는
        경우(HITL 응답)에 쓴다 — 취소하면 interrupt 체크포인트가 사라져 재개가
        불가능해지고, 기다리지 않으면 두 실행이 같은 체크포인트를 동시에 쓴다.

        타임아웃은 실패가 아니다. 이전 턴이 비정상적으로 오래 걸리는 경우까지
        새 요청을 영원히 막는 것보다, 로그를 남기고 진행하는 편이 낫다.
        """
        task = self.get_inflight(conversation_id)
        if task is None:
            return False
        try:
            await asyncio.wait_for(asyncio.shield(task), timeout=timeout)
        except asyncio.TimeoutError:
            logger.warning(
                "이전 턴이 %.0f초 안에 끝나지 않아 기다리기를 멈춘다 | conversation_id=%s",
                timeout, conversation_id,
            )
        except asyncio.CancelledError:
            # 기다리는 동안 그 턴이 취소됐다 — 기다리던 목적(겹치지 않기)은 달성됐다.
            pass
        except Exception:
            logger.debug(
                "이전 턴이 예외로 종료 | conversation_id=%s", conversation_id, exc_info=True
            )
        return True

    async def cancel(self, conversation_id: str) -> bool:
        """진행 중인 턴을 취소한다(명시적 중지). 취소할 것이 있었으면 True."""
        task = self.get_inflight(conversation_id)
        if task is None:
            return False
        task.cancel()
        try:
            await asyncio.wait_for(asyncio.shield(task), timeout=10)
        except (asyncio.CancelledError, asyncio.TimeoutError):
            pass
        except Exception:
            # 턴이 자기 예외로 끝난 경우 — 취소 목적은 달성됐다.
            logger.debug("inflight 턴이 예외로 종료 | conversation_id=%s", conversation_id, exc_info=True)
        self._tasks.pop(conversation_id, None)
        return True

    def active_conversation_ids(self) -> List[str]:
        """지금 턴이 돌고 있는 conversation_id 목록.

        세션당 파드 배포에서 리버스 프록시가 TTL 회수 여부를 판정하는 근거다 —
        유휴 시간이 지났어도 턴이 돌고 있는 파드는 죽이면 안 된다. 한 턴은 LLM 이
        생각하거나 도구 하나가 도는 동안 유휴 한도보다 훨씬 오래 걸릴 수 있다.
        """
        return [cid for cid, task in list(self._tasks.items()) if not task.done()]

    def busy(self) -> bool:
        """턴이 하나라도 돌고 있으면 True."""
        return bool(self.active_conversation_ids())

    def _clear_if(self, conversation_id: str, task: asyncio.Task) -> None:
        if self._tasks.get(conversation_id) is task:
            self._tasks.pop(conversation_id, None)

    def clear(self) -> None:
        """테스트용."""
        self._tasks.clear()


# ---------------------------------------------------------------------------
# 프로세스 기본 인스턴스 + 교체 훅
# ---------------------------------------------------------------------------

_run_registry: ChatRunRegistry = ChatRunRegistry()
_inflight_registry: InflightRegistry = InflightRegistry()


def get_run_registry() -> ChatRunRegistry:
    return _run_registry


def set_run_registry(registry: ChatRunRegistry) -> None:
    """공유 상태 백엔드(Redis 등) 구현으로 교체한다."""
    global _run_registry
    _run_registry = registry


def get_inflight_registry() -> InflightRegistry:
    return _inflight_registry


def set_inflight_registry(registry: InflightRegistry) -> None:
    global _inflight_registry
    _inflight_registry = registry


class RunRecordingQueue(asyncio.Queue):
    """SSE 출력 큐를 가로채 레지스트리에도 같은 이벤트를 기록하는 큐.

    `mount_chat_sse()` 안에서 `ChatStreamer`(토큰)와 `ChatEventQueue`(done) 가 모두
    이 하나의 큐로 들어온다. 그래서 스트리머를 감싸는 대신 큐 한 곳만 가로채면
    턴이 내보내는 이벤트 전부가 레지스트리에 남는다 — 재접속한 클라이언트가 받는
    것과 원래 클라이언트가 받는 것이 정의상 같아진다.
    """

    def __init__(self, conversation_id: str, registry: Optional[ChatRunRegistry] = None) -> None:
        # maxsize=0(무제한) — put() 이 가득 참을 기다릴 일이 없다. 아래에서 put() 이
        # 대기 로직을 건너뛰고 바로 넣는 것이 안전한 이유다.
        super().__init__()
        self._conversation_id = conversation_id
        self._registry = registry

    async def put(self, item: Any) -> None:  # type: ignore[override]
        # `asyncio.Queue.put()` 은 내부에서 `self.put_nowait()` 를 부른다. 그대로
        # super().put() 을 쓰면 아래 오버라이드까지 타서 같은 이벤트가 두 번 기록되고,
        # 재접속 스냅샷의 본문이 두 배로 불어난다. 그래서 기반 클래스의 put_nowait 를
        # 직접 불러 우리 오버라이드를 건너뛴다.
        asyncio.Queue.put_nowait(self, item)
        await self._record(item)

    def put_nowait(self, item: Any) -> None:  # type: ignore[override]
        asyncio.Queue.put_nowait(self, item)
        # 동기 경로에서도 기록이 빠지지 않게 백그라운드로 넘긴다. SDK 자신은 항상
        # `await put()` 을 쓰므로 이 경로는 외부 호출자를 위한 보조다.
        try:
            asyncio.get_running_loop().create_task(self._record(item))
        except RuntimeError:
            pass

    async def _record(self, item: Any) -> None:
        registry = self._registry or get_run_registry()
        cid = self._conversation_id
        if not cid or not isinstance(item, dict):
            return
        if item.get("event") != "message":
            return
        data = item.get("data")
        if not isinstance(data, dict):
            return
        try:
            await registry.record(cid, data)
        except Exception:
            logger.warning("chat_registry: 이벤트 기록 실패 | conversation_id=%s", cid, exc_info=True)
