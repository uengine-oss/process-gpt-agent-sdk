import asyncio
import logging
import os
from typing import Any, Dict, Optional

from .database import record_events_bulk

logger = logging.getLogger(__name__)

# ------------------------------ Event Coalescing (env tunable) ------------------------------
COALESCE_DELAY = float(os.getenv("EVENT_COALESCE_DELAY_SEC", "1.0"))  # 최대 지연
COALESCE_BATCH = int(os.getenv("EVENT_COALESCE_BATCH", "3"))  # 즉시 flush 임계치

_EVENT_BUF: list[Dict[str, Any]] = []
_EVENT_TIMER: Optional[asyncio.TimerHandle] = None
_EVENT_LOCK = asyncio.Lock()


async def flush_events_now() -> None:
    """버퍼된 이벤트를 bulk RPC로 즉시 저장."""
    global _EVENT_BUF, _EVENT_TIMER
    async with _EVENT_LOCK:
        buf = _EVENT_BUF[:]
        _EVENT_BUF.clear()
        if _EVENT_TIMER and not _EVENT_TIMER.cancelled():
            _EVENT_TIMER.cancel()
        _EVENT_TIMER = None
    if not buf:
        return

    logger.info("📤 이벤트 버퍼 플러시 시작 - %d개 이벤트", len(buf))
    await record_events_bulk(buf)
    logger.info("🔄 이벤트 버퍼 플러시 시도 완료 - %d개 이벤트", len(buf))


def _schedule_delayed_flush() -> None:
    global _EVENT_TIMER
    if _EVENT_TIMER is None:
        loop = asyncio.get_running_loop()
        _EVENT_TIMER = loop.call_later(COALESCE_DELAY, lambda: asyncio.create_task(flush_events_now()))


async def enqueue_ui_event_coalesced(payload: Dict[str, Any]) -> None:
    """1초 코얼레싱 / COALESCE_BATCH개 모이면 즉시 플러시 (환경변수로 조절 가능)."""
    global _EVENT_BUF
    to_flush_now = False
    async with _EVENT_LOCK:
        _EVENT_BUF.append(payload)
        logger.info("📥 이벤트 버퍼에 추가 - 현재 %d개 (임계치: %d개)", len(_EVENT_BUF), COALESCE_BATCH)
        if len(_EVENT_BUF) >= COALESCE_BATCH:
            to_flush_now = True
            logger.info("⚡ 임계치 도달 - 즉시 플러시 예정")
        else:
            _schedule_delayed_flush()
            logger.info("⏰ 지연 플러시 스케줄링")
    if to_flush_now:
        await flush_events_now()

