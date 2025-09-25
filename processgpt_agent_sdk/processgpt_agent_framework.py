import asyncio
import logging
import json
import os
import signal
import uuid
from typing import Dict, Any, Optional
from dataclasses import dataclass

from dotenv import load_dotenv

from a2a.server.agent_execution import AgentExecutor, RequestContext
from a2a.server.events import EventQueue, Event
from a2a.types import TaskArtifactUpdateEvent, TaskState, TaskStatusUpdateEvent

from .database import (
    initialize_db,
    polling_pending_todos,
    record_events_bulk,
    record_event,          # 단건 이벤트 기록
    save_task_result,
    update_task_error,
    get_consumer_id,
    fetch_context_bundle,
)
from .utils import summarize_error_to_user, summarize_feedback

load_dotenv()
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# ------------------------------ 커스텀 예외 ------------------------------
class ContextPreparationError(Exception):
    """컨텍스트 준비 실패를 상위 경계에서 단일 처리하기 위한 래퍼 예외."""
    def __init__(self, original: Exception, friendly: Optional[str] = None):
        super().__init__(f"{type(original).__name__}: {str(original)}")
        self.original = original
        self.friendly = friendly

# ------------------------------ Event Coalescing (env tunable) ------------------------------
COALESCE_DELAY = float(os.getenv("EVENT_COALESCE_DELAY_SEC", "1.0"))  # 최대 지연
COALESCE_BATCH = int(os.getenv("EVENT_COALESCE_BATCH", "3"))          # 즉시 flush 임계치

_EVENT_BUF: list[Dict[str, Any]] = []
_EVENT_TIMER: Optional[asyncio.TimerHandle] = None
_EVENT_LOCK = asyncio.Lock()

async def _flush_events_now():
    """버퍼된 이벤트를 bulk RPC로 즉시 저장"""
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
    # 실제 성공/실패 로깅은 record_events_bulk 내부에서 수행
    await record_events_bulk(buf)
    # 여기서는 시도 사실만 남김(성공처럼 보이는 'flushed' 오해 방지)
    logger.info("🔄 이벤트 버퍼 플러시 시도 완료 - %d개 이벤트", len(buf))

def _schedule_delayed_flush():
    global _EVENT_TIMER
    if _EVENT_TIMER is None:
        loop = asyncio.get_running_loop()
        _EVENT_TIMER = loop.call_later(COALESCE_DELAY, lambda: asyncio.create_task(_flush_events_now()))

async def enqueue_ui_event_coalesced(payload: Dict[str, Any]):
    """1초 코얼레싱 / COALESCE_BATCH개 모이면 즉시 플러시 (환경변수로 조절 가능)"""
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
        await _flush_events_now()

# ------------------------------ Request Context ------------------------------
@dataclass
class TodoListRowContext:
    row: Dict[str, Any]

class ProcessGPTRequestContext(RequestContext):
    def __init__(self, row: Dict[str, Any]):
        self.row = row
        self._user_input = (row.get("query") or "").strip()
        self._message = self._user_input
        self._current_task = None
        self._task_state = row.get("draft_status") or ""
        self._extra_context: Dict[str, Any] = {}

    async def prepare_context(self) -> None:
        """
        컨텍스트 준비.
        - 실패 시: 더 이상 진행하지 않고 ContextPreparationError를 발생시켜
          상위 경계에서 FAILED 처리(이벤트 기록 포함)를 단일 경로로 수행.
        """
        logger.info("\n🔧 컨텍스트 준비 시작...")
        
        # 1단계: 기본 정보 추출
        effective_proc_inst_id = self.row.get("root_proc_inst_id") or self.row.get("proc_inst_id")
        tool_val = self.row.get("tool") or ""
        tenant_id = self.row.get("tenant_id") or ""
        user_ids = self.row.get("user_id") or ""
        
        logger.info("📋 기본 정보 추출 완료 - proc_inst_id: %s, tool: %s, tenant: %s", 
                   effective_proc_inst_id, tool_val, tenant_id)

        try:
            # 2단계: 컨텍스트 번들 조회
            logger.info("🔍 컨텍스트 번들 조회 중...")
            notify_emails, tenant_mcp, form_tuple, agents = await fetch_context_bundle(
                effective_proc_inst_id, tenant_id, tool_val, user_ids
            )
            form_id, form_fields, form_html = form_tuple
            
            logger.info("📦 컨텍스트 번들 조회 완료 - agents: %d개, notify_emails: %s, form_type: %s", 
                       len(agents) if isinstance(agents, list) else 0, 
                       "있음" if notify_emails else "없음",
                       "자유형식" if form_id == "freeform" else "정의된 폼")
            
        except Exception as e:
            logger.error("❌ 컨텍스트 번들 조회 실패: %s", str(e))
            # 사용자 친화 요약은 상위 경계에서 한 번만 기록하도록 넘김
            raise ContextPreparationError(e)

        # 3단계: 피드백 요약 처리
        logger.info("📝 피드백 요약 처리 중...")
        feedback_str = self.row.get("feedback", "")
        contents_str = self.row.get("output", "") or self.row.get("draft", "")
        summarized_feedback = ""

        if feedback_str.strip():
            summarized_feedback = await summarize_feedback(feedback_str, contents_str)
            logger.info("✅ 피드백 요약 완료 - 원본: %d자 → 요약: %d자", len(feedback_str), len(summarized_feedback))

        # 4단계: 컨텍스트 구성
        logger.info("🏗️ 컨텍스트 구성 중...")
        self._extra_context = {
            "id": self.row.get("id"),
            "proc_inst_id": effective_proc_inst_id,
            "root_proc_inst_id": self.row.get("root_proc_inst_id"),
            "activity_name": self.row.get("activity_name"),
            "agents": agents,
            "tenant_mcp": tenant_mcp,
            "form_fields": form_fields,
            "form_html": form_html,
            "form_id": form_id,
            "notify_user_emails": notify_emails,
            "summarized_feedback": summarized_feedback,
        }
        
        logger.info("✅ 컨텍스트 준비 완료! (agents=%d개)", 
                   len(agents) if isinstance(agents, list) else 0)

    def get_user_input(self) -> str:
        return self._user_input

    @property
    def message(self) -> str:
        return self._message

    @property
    def current_task(self):
        return self._current_task

    @current_task.setter
    def current_task(self, task):
        self._current_task = task

    @property
    def task_state(self) -> str:
        return self._task_state

    def get_context_data(self) -> Dict[str, Any]:
        return {"row": self.row, "extras": self._extra_context}

# ------------------------------ Event Queue ------------------------------
class ProcessGPTEventQueue(EventQueue):
    def __init__(self, todolist_id: str, agent_orch: str, proc_inst_id: Optional[str]):
        self.todolist_id = todolist_id
        self.agent_orch = agent_orch
        self.proc_inst_id = proc_inst_id
        super().__init__()

    def enqueue_event(self, event: Event):
        try:
            proc_inst_id_val = getattr(event, "contextId", None) or self.proc_inst_id
            todo_id_val = getattr(event, "taskId", None) or str(self.todolist_id)
            logger.info("\n📨 이벤트 수신: %s (task=%s)", type(event).__name__, self.todolist_id)

            # 1) 결과물 저장
            if isinstance(event, TaskArtifactUpdateEvent):
                logger.info("📄 아티팩트 업데이트 이벤트 처리 중...")
                try:
                    is_final = bool(
                        getattr(event, "final", None)
                        or getattr(event, "lastChunk", None)
                        or getattr(event, "last_chunk", None)
                        or getattr(event, "last", None)
                    )
                    artifact_content = self._extract_payload(event)
                    logger.info("💾 아티팩트 저장 중... (final=%s)", is_final)
                    asyncio.create_task(save_task_result(self.todolist_id, artifact_content, is_final))
                    logger.info("✅ 아티팩트 저장 완료")
                except Exception as e:
                    logger.exception("❌ 아티팩트 저장 실패: %s", str(e))
                return

            # 2) 상태 이벤트 저장(코얼레싱 → bulk)
            if isinstance(event, TaskStatusUpdateEvent):
                logger.info("📊 상태 업데이트 이벤트 처리 중...")
                metadata = getattr(event, "metadata", None) or {}
                crew_type_val = metadata.get("crew_type")
                status_obj = getattr(event, "status", None)
                state_val = getattr(status_obj, "state", None)
                event_type_val = {TaskState.input_required: "human_asked"}.get(state_val) or metadata.get("event_type")
                status_val = metadata.get("status")
                job_id_val = metadata.get("job_id")
                
                logger.info("🔍 이벤트 메타데이터 분석 - event_type: %s, status: %s", event_type_val, status_val)
                
                try:
                    payload: Dict[str, Any] = {
                        "id": str(uuid.uuid4()),
                        "job_id": job_id_val,
                        "todo_id": str(todo_id_val),
                        "proc_inst_id": proc_inst_id_val,
                        "crew_type": crew_type_val,
                        "event_type": event_type_val,
                        "data": self._extract_payload(event),
                        "status": status_val or None,
                    }
                    logger.info("📤 상태 이벤트 큐에 추가 중...")
                    asyncio.create_task(enqueue_ui_event_coalesced(payload))
                    logger.info("✅ 상태 이벤트 큐 추가 완료")
                except Exception as e:
                    logger.exception("❌ 상태 이벤트 기록 실패: %s", str(e))
                return

        except Exception as e:
            logger.error("❌ 이벤트 처리 실패: %s", str(e))
            raise

    def _extract_payload(self, event: Event) -> Any:
        try:
            artifact_or_none = getattr(event, "artifact", None)
            status_or_none = getattr(event, "status", None)
            message_or_none = getattr(status_or_none, "message", None)
            source = artifact_or_none if artifact_or_none is not None else message_or_none
            return self._parse_json_or_text(source)
        except Exception:
            return {}

    def _parse_json_or_text(self, value: Any) -> Any:
        try:
            if value is None:
                return {}
            if isinstance(value, str):
                text = value.strip()
                if not text:
                    return ""
                try:
                    return json.loads(text)
                except Exception:
                    return text
            if hasattr(value, "model_dump") and callable(getattr(value, "model_dump")):
                value = value.model_dump()
            elif not isinstance(value, dict) and hasattr(value, "dict") and callable(getattr(value, "dict")):
                value = value.dict()
            elif not isinstance(value, dict) and hasattr(value, "__dict__"):
                value = value.__dict__
            if isinstance(value, dict):
                parts = value.get("parts")
                if isinstance(parts, list) and parts:
                    first = parts[0] if isinstance(parts[0], dict) else None
                    if first and isinstance(first, dict):
                        txt = first.get("text") or first.get("content") or first.get("data")
                        if isinstance(txt, str):
                            try:
                                return json.loads(txt)
                            except Exception:
                                return txt
                top_text = value.get("text") or value.get("content") or value.get("data")
                if isinstance(top_text, str):
                    try:
                        return json.loads(top_text)
                    except Exception:
                        return top_text
                return value
            return value
        except Exception:
            return {}

    def task_done(self) -> None:
        try:
            logger.info("🏁 작업 완료 이벤트 생성 중...")
            payload: Dict[str, Any] = {
                "id": str(uuid.uuid4()),
                "job_id": "CREW_FINISHED",
                "todo_id": str(self.todolist_id),
                "proc_inst_id": self.proc_inst_id,
                "crew_type": "agent",
                "data": "Task completed successfully",
                "event_type": "crew_completed",
                "status": None,
            }
            logger.info("📤 작업 완료 이벤트 큐에 추가 중...")
            asyncio.create_task(enqueue_ui_event_coalesced(payload))
            logger.info("✅ 작업 완료 이벤트 기록 완료")
        except Exception as e:
            logger.error("❌ 작업 완료 이벤트 기록 실패: %s", str(e))
            raise

# ------------------------------ Agent Server ------------------------------
class ProcessGPTAgentServer:
    def __init__(self, agent_executor: AgentExecutor, agent_type: str):
        self.agent_executor = agent_executor
        self.agent_orch = agent_type
        self.is_running = False
        self._shutdown_event = asyncio.Event()
        self._current_todo_id: Optional[str] = None  # 진행 중 작업 추적(참고용)

    async def _install_signal_handlers(self):
        loop = asyncio.get_running_loop()
        try:
            loop.add_signal_handler(signal.SIGTERM, lambda: self._shutdown_event.set())
            loop.add_signal_handler(signal.SIGINT,  lambda: self._shutdown_event.set())
        except NotImplementedError:
            # Windows 등 일부 환경은 지원 안 됨
            pass

    async def run(self):
        self.is_running = True
        logger.info("\n\n🚀 ProcessGPT Agent Server START (agent=%s)\n", self.agent_orch)
        initialize_db()
        await self._install_signal_handlers()

        while self.is_running and not self._shutdown_event.is_set():
            try:
                logger.info("🔍 Polling for tasks (agent_orch=%s)...", self.agent_orch)
                row = await polling_pending_todos(self.agent_orch, get_consumer_id())

                if row:
                    logger.info("✅ 새 작업: %s (proc=%s, activity=%s)", row.get("id"), row.get("proc_inst_id"), row.get("activity_name"))
                    try:
                        self._current_todo_id = str(row.get("id"))
                        await self.process_todolist_item(row)
                    except Exception as e:
                        # 경계에서 처리(에러 이벤트 + FAILED 마킹) 후 예외 재전달됨.
                        logger.exception("process_todolist_item failed: %s", str(e))
                    finally:
                        self._current_todo_id = None
                    # 작업이 있었으므로 슬립 생략 → 즉시 다음 폴링
                    continue

                # 작업 없을 때만 10초 대기
                await asyncio.sleep(10)

            except Exception as e:
                # 폴링 자체 오류는 특정 작업에 귀속되지 않으므로 상태 마킹 대상 없음
                logger.exception("run loop error: %s", str(e))
                await asyncio.sleep(10)

        # 종료 시 남은 이벤트 강제 flush (오류로 간주하지 않음)
        try:
            await _flush_events_now()
            logger.info("🧹 graceful shutdown: pending events flushed")
        except Exception as e:
            logger.exception("flush on shutdown failed: %s", str(e))

        logger.info("👋 Agent server stopped.")

    async def process_todolist_item(self, row: Dict[str, Any]):
        """
        경계 정책(최종본):
        - 어떤 예외든 여기에서 잡힘
        - 항상 단일 경로로:
          1) 사용자 친화 5줄 설명 생성
          2) event_type='error' 단건 이벤트 기록
          3) todolist를 FAILED로 마킹
          4) 예외 재전달(상위 루프는 죽지 않고 다음 폴링)
        """
        task_id = row.get("id")
        logger.info("\n🎯 작업 처리 시작 - Task ID: %s", task_id)
        logger.info("📝 작업 정보 - proc_inst_id: %s, activity: %s, tool: %s", 
                   row.get("proc_inst_id"), row.get("activity_name"), row.get("tool"))
        
        friendly_text: Optional[str] = None

        try:
            # 1) 컨텍스트 준비 (실패 시 ContextPreparationError로 올라옴)
            logger.info("🔧 컨텍스트 준비 단계 시작...")
            context = ProcessGPTRequestContext(row)
            await context.prepare_context()
            logger.info("✅ 컨텍스트 준비 완료")

            # 2) 실행
            logger.info("🤖 에이전트 실행 단계 시작...")
            event_queue = ProcessGPTEventQueue(str(task_id), self.agent_orch, row.get("proc_inst_id"))
            await self.agent_executor.execute(context, event_queue)
            logger.info("✅ 에이전트 실행 완료")

            # 3) 정상 완료 이벤트
            logger.info("🏁 작업 완료 처리 중...")
            event_queue.task_done()
            logger.info("🎉 작업 완료: %s\n", task_id)

        except Exception as e:
            logger.error("❌ 작업 처리 중 오류 발생: %s", str(e))
            
            # 컨텍스트 실패라면 friendly가 없을 수 있어, 여기서 반드시 생성
            try:
                logger.info("📝 사용자 친화 오류 메시지 생성 중...")
                if isinstance(e, ContextPreparationError) and e.friendly:
                    friendly_text = e.friendly
                else:
                    friendly_text = await summarize_error_to_user(
                        e if not isinstance(e, ContextPreparationError) else e.original,
                        {
                            "task_id": task_id,
                            "proc_inst_id": row.get("proc_inst_id"),
                            "agent_orch": self.agent_orch,
                            "tool": row.get("tool"),
                        },
                    )
                logger.info("✅ 사용자 친화 오류 메시지 생성 완료")
            except Exception:
                logger.warning("⚠️ 사용자 친화 오류 메시지 생성 실패")
                # 요약 생성 실패 시에도 처리 계속
                friendly_text = None

            # 에러 이벤트 기록(단건). 실패해도 로그만 남기고 진행.
            logger.info("📤 오류 이벤트 기록 중...")
            payload: Dict[str, Any] = {
                "id": str(uuid.uuid4()),
                "job_id": "TASK_ERROR",
                "todo_id": str(task_id),
                "proc_inst_id": row.get("proc_inst_id"),
                "crew_type": "agent",
                "event_type": "error",
                "data": {
                    "name": "시스템 오류 알림",
                    "goal": "오류 원인과 대처 안내를 전달합니다.",
                    "agent_profile": "/images/chat-icon.png",
                    "friendly": friendly_text or "처리 중 오류가 발생했습니다. 로그를 확인해 주세요.",
                    "raw_error": f"{type(e).__name__}: {str(e)}" if not isinstance(e, ContextPreparationError) else f"{type(e.original).__name__}: {str(e.original)}",
                }
            }
            try:
                asyncio.create_task(record_event(payload))
                logger.info("✅ 오류 이벤트 기록 완료")
            except Exception:
                logger.exception("❌ 오류 이벤트 기록 실패")

            # 상태 FAILED 마킹
            logger.info("🏷️ 작업 상태 FAILED로 마킹 중...")
            try:
                await update_task_error(str(task_id))
                logger.info("✅ 작업 상태 FAILED 마킹 완료")
            except Exception:
                logger.exception("❌ 작업 상태 FAILED 마킹 실패")

            # 상위로 재전달하여 루프는 계속(죽지 않음)
            logger.error("🔄 오류 처리 완료 - 다음 작업으로 계속 진행")
            raise

    def stop(self):
        self.is_running = False
        self._shutdown_event.set()
        logger.info("ProcessGPT Agent Server stopping...")
