"""작업 점유의 만료 시한(lease)을 유지한다.

폴링 워커가 todolist 한 건을 집으면 그 행은 `draft_status='STARTED'` 가 된다.
예전에는 그게 영구적이었다 — 워커가 `kill -9` 로 죽으면 그 행은 STARTED 로 남고
선택 조건에 다시 걸리지 않아 아무도 집지 않았다. 레플리카를 늘리거나 KEDA 로
줄이면 그만큼 작업이 조용히 사라진다.

그래서 점유에 시한을 둔다. 집을 때 RPC 가 `lease_until` 을 적고, 살아 있는 워커가
수행 중 주기적으로 연장한다. 연장이 끊기면 RPC 가 그 행을 다시 집어 간다.

## 왜 별도 스레드인가

연장을 asyncio 태스크로 두면, 익스큐터가 동기 호출로 이벤트 루프를 붙잡는 동안
heartbeat 이 함께 멈춘다. 그 사이 lease 가 만료되면 **살아서 일하는 중인 작업이**
회수되어 두 번 수행된다. 에이전트 코드가 이벤트 루프를 막지 않는다는 보장은 없으므로
(LLM SDK·서브프로세스·파일 IO 가 섞여 들어온다) heartbeat 은 자기 OS 스레드에서
돈다. GIL 때문에 CPU 를 오래 쥐는 구간에서는 이것도 지연될 수 있지만, 대기가
대부분인 호출에서는 스레드가 정상적으로 깨어난다.

## 연장이 거절되면

`renew_task_lease` 는 거절 이유를 돌려준다.

- `not_owner`: 다른 워커가 이미 회수했다. 지금 하는 일을 **버려야 한다**.
  그러지 않으면 같은 작업이 둘에서 동시에 끝까지 수행된다(펜싱).
- `not_started`: COMPLETED/HUMAN_ASKED/CANCELLED 등으로 넘어갔다. 연장할 점유가
  없을 뿐이고 버릴 일은 아니다 — heartbeat 만 멈춘다.
"""

from __future__ import annotations

import logging
import os
import threading
from typing import Callable, Optional

logger = logging.getLogger(__name__)

# ------------------------------ 기본값과 그 근거 ------------------------------
#
# lease 길이와 갱신 주기는 "얼마나 빨리 회수되는가" 와 "살아 있는 워커를 잘못
# 회수할 위험" 의 거래다.
#
# - LEASE_SECONDS(120): 워커가 죽은 뒤 다른 워커가 그 작업을 집기까지의 상한은
#   남은 lease + 폴링 주기(10초)다. 2분은 파드가 죽고 KEDA/Deployment 가 새 파드를
#   띄우는 시간과 같은 자릿수여서, 회수가 복구보다 먼저 일어나 쓸데없이 중복
#   클레임을 만들지 않는다.
# - HEARTBEAT_SECONDS(30): lease 의 1/4. 연속 세 번 실패해도(일시적 DB 오류,
#   네트워크 재시도) lease 가 남아 있다. 1/2 로 두면 한 번 놓치는 것만으로
#   만료에 닿는다.
# - MAX_CLAIMS(3): 최초 점유 + 회수 2회. 같은 지점에서 매번 죽는 작업이 영원히
#   재집행되며 자원을 태우는 것을 막는다. 상한에 닿으면 RPC 가 FAILED 로 종결한다.
#
# 벤치에서는 짧게(20초/5초) 줄여 같은 비율로 돌린다 — 재클레임 실측을 분 단위로
# 기다리지 않기 위한 것이고, 비율(1/4)은 운영과 같다.
DEFAULT_LEASE_SECONDS = 120
DEFAULT_MAX_CLAIMS = 3
_LEASE_TO_HEARTBEAT = 4


def _positive_int(env: str, default: int) -> int:
    raw = (os.getenv(env) or "").strip()
    if not raw:
        return default
    try:
        value = int(raw)
    except ValueError:
        logger.warning("%s=%r 를 정수로 읽을 수 없다. 기본값 %d 을 쓴다.", env, raw, default)
        return default
    if value <= 0:
        logger.warning("%s=%d 는 0 이하다. 기본값 %d 을 쓴다.", env, value, default)
        return default
    return value


def lease_seconds() -> int:
    """점유가 유지되는 시간(초)."""
    return _positive_int("TASK_LEASE_SECONDS", DEFAULT_LEASE_SECONDS)


def heartbeat_seconds() -> int:
    """연장 주기(초). 기본은 lease 의 1/4 이고, 최소 1초다."""
    explicit = _positive_int("TASK_LEASE_HEARTBEAT_SECONDS", 0)
    if explicit:
        return explicit
    return max(1, lease_seconds() // _LEASE_TO_HEARTBEAT)


def max_claims() -> int:
    """한 작업이 점유될 수 있는 횟수의 상한(최초 + 회수)."""
    return _positive_int("TASK_MAX_CLAIMS", DEFAULT_MAX_CLAIMS)


class LeaseKeeper:
    """작업 하나의 lease 를 수행이 끝날 때까지 연장한다.

    `renew` 는 동기 함수여야 한다 — 이 객체는 자기 스레드에서 그것을 부른다.
    이벤트 루프에 의존하지 않는 것이 요점이므로 코루틴을 받지 않는다.
    """

    def __init__(
        self,
        todo_id: str,
        consumer: str,
        renew: Callable[[str, str, int], dict],
        *,
        on_lost: Optional[Callable[[str], None]] = None,
        lease_sec: Optional[int] = None,
        interval_sec: Optional[int] = None,
    ) -> None:
        self.todo_id = str(todo_id)
        self.consumer = str(consumer)
        self._renew = renew
        self._on_lost = on_lost
        self.lease_sec = lease_sec or lease_seconds()
        self.interval_sec = interval_sec or heartbeat_seconds()
        self._stop = threading.Event()
        self._lost = threading.Event()
        self._thread: Optional[threading.Thread] = None
        self._lost_reason: Optional[str] = None

    # -------------------------- 상태 조회 --------------------------
    @property
    def lost(self) -> bool:
        """다른 워커가 이 작업을 회수해 갔는가."""
        return self._lost.is_set()

    @property
    def lost_reason(self) -> Optional[str]:
        return self._lost_reason

    # -------------------------- 수명 --------------------------
    def start(self) -> "LeaseKeeper":
        if self._thread is not None:
            return self
        self._thread = threading.Thread(
            target=self._loop,
            name=f"lease-{self.todo_id[:8]}",
            daemon=True,
        )
        self._thread.start()
        logger.info(
            "🫀 [lease 연장 시작] todo=%s consumer=%s lease=%ds 주기=%ds",
            self.todo_id, self.consumer, self.lease_sec, self.interval_sec,
        )
        return self

    def stop(self, timeout: float = 2.0) -> None:
        self._stop.set()
        thread, self._thread = self._thread, None
        if thread is not None and thread.is_alive():
            thread.join(timeout=timeout)

    def __enter__(self) -> "LeaseKeeper":
        return self.start()

    def __exit__(self, *_exc) -> None:
        self.stop()

    # -------------------------- 내부 --------------------------
    def _loop(self) -> None:
        while not self._stop.wait(self.interval_sec):
            try:
                result = self._renew(self.todo_id, self.consumer, self.lease_sec) or {}
            except Exception:
                # 일시적 실패는 다음 주기에 다시 시도한다. lease 는 주기의 4배이므로
                # 연속 세 번까지는 여유가 있다. 여기서 작업을 버리면 DB 가 잠깐
                # 흔들릴 때마다 멀쩡한 작업이 중단된다.
                logger.exception("lease 연장 실패(다음 주기에 재시도) todo=%s", self.todo_id)
                continue

            if result.get("renewed"):
                continue

            reason = str(result.get("reason") or "unknown")
            if reason == "not_owner":
                # 회수된 것이다. 계속 일하면 같은 작업이 둘에서 수행된다.
                self._lost_reason = reason
                self._lost.set()
                logger.warning(
                    "🚫 [점유 상실] 다른 워커가 회수했다. 진행 중인 작업을 버린다. "
                    "todo=%s 나=%s 현재점유자=%s",
                    self.todo_id, self.consumer, result.get("consumer"),
                )
                if self._on_lost is not None:
                    try:
                        self._on_lost(reason)
                    except Exception:
                        logger.exception("on_lost 처리 실패 todo=%s", self.todo_id)
                return

            # not_started / missing / bad_request: 연장할 점유가 없다.
            # 끝난 작업이거나 사람 답변을 기다리는 중이다 — 버릴 일은 아니다.
            logger.info(
                "🫀 [lease 연장 중단] 연장할 점유가 없다. todo=%s reason=%s status=%s",
                self.todo_id, reason, result.get("draft_status"),
            )
            return
