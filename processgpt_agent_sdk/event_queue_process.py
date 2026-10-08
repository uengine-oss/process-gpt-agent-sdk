import asyncio
import json
import uuid
import logging
from typing import Any, Dict, Optional

from a2a.server.events import EventQueue, Event
from a2a.types import TaskArtifactUpdateEvent, TaskState, TaskStatusUpdateEvent
from google.protobuf.json_format import MessageToDict
from google.protobuf.message import Message as ProtobufMessage

from .database import mark_task_human_asked, save_task_result
from .event_coalescer import enqueue_ui_event_coalesced

logger = logging.getLogger(__name__)


# A2A TaskState → events.event_type enum 자동 매핑 테이블.
# - lifecycle 의미가 1:1 로 명확한 state 만 등록한다.
# - WORKING 은 너무 광범위하고 도메인 sub-event(tool_usage_* 등) 의 베이스로도
#   재사용되므로 자동 매핑하지 않는다 → metadata["event_type"] 없으면 NULL.
_STATE_TO_EVENT_TYPE = {
    TaskState.TASK_STATE_SUBMITTED: "task_started",
    TaskState.TASK_STATE_COMPLETED: "task_completed",
    TaskState.TASK_STATE_FAILED: "error",
    TaskState.TASK_STATE_INPUT_REQUIRED: "human_asked",
}


class ProcessEventQueue(EventQueue):
    """프로세스 모드(Task lifecycle) 전용 EventQueue.

    A2A 표준 이벤트 타입을 라우팅 키로 사용합니다.

    - TaskStatusUpdateEvent: events 테이블에 저장 (state/text 무관, 받은 대로)
    - TaskArtifactUpdateEvent: todolist 에 저장 (last_chunk 가 is_final 로 전달)
    - Task / Message: 저장 대상 아님. silently ignore.

    사람에게 물어 놓고 끝난 실행(INPUT_REQUIRED)은 완료가 아니다. 그 뒤에 오는
    마지막 아티팩트는 결과가 아니라 질문 본문이므로 todolist 결과로 저장하지 않고
    작업을 HUMAN_ASKED(사람 답변 대기)로 둔다. crew_completed 도 내지 않는다.

    필터링은 Executor 책임이고 SDK 는 받은 대로 라우팅합니다.
    """

    def __init__(self, todolist_id: str, agent_orch: str, proc_inst_id: Optional[str]):
        self.todolist_id = todolist_id
        self.agent_orch = agent_orch
        self.proc_inst_id = proc_inst_id
        # crew_completed 이벤트 중복 발행 방지 플래그.
        # last_chunk=True artifact 와 task_done() 양쪽에서 트리거 가능하지만 한 번만 기록한다.
        self._completion_emitted = False
        # 이번 실행이 사람의 답을 기다리며 멈췄는가. INPUT_REQUIRED 로 켜지고,
        # 그 뒤 실행이 계속되면(WORKING/COMPLETED) 꺼진다 — 같은 실행 안에서
        # 답을 받아 이어 간 경우의 마지막 아티팩트는 진짜 결과다.
        self._awaiting_human = False
        self._human_asked_marked = False
        # 실행이 실패 상태(FAILED)를 보냈는가, 결과(final 아티팩트)를 저장했는가.
        # 실패만 보내고 결과 없이 끝난 실행은 완료가 아니다 — crew_completed 를
        # 내지 않고, 행의 종결(FAILED)은 framework 가 맡는다.
        self._failed = False
        self._result_saved = False
        # 이 큐가 띄운 DB 쓰기(결과 저장·대기 표시). framework 가 실행 뒤 행
        # 상태를 판정하기 전에 drain() 으로 기다린다 — 기다리지 않으면 결과
        # 저장이 끝나기 전의 STARTED 를 보고 "결과 없이 끝났다" 로 오판한다.
        self._pending: set = set()
        super().__init__()

    def _spawn(self, coro) -> None:
        task = asyncio.create_task(coro)
        self._pending.add(task)
        task.add_done_callback(self._pending.discard)

    async def drain(self) -> None:
        """이 큐가 띄운 DB 쓰기가 모두 끝날 때까지 기다린다."""
        while self._pending:
            await asyncio.gather(*list(self._pending), return_exceptions=True)

    @property
    def ended_in_failure(self) -> bool:
        """실패 상태를 보냈고 그 뒤 결과를 저장하지 않은 채 끝났다."""
        return self._failed and not self._result_saved

    async def enqueue_event(self, event: Event):
        try:
            proc_inst_id_val = (
                getattr(event, "context_id", None)
                or getattr(event, "contextId", None)
                or self.proc_inst_id
            )
            todo_id_val = (
                getattr(event, "task_id", None)
                or getattr(event, "taskId", None)
                or str(self.todolist_id)
            )
            logger.info("\n\n📨 이벤트 수신: %s (task=%s)", type(event).__name__, self.todolist_id)

            # Task / Message 등은 저장 대상이 아니므로 무시(필요 시 확장)
            if not isinstance(event, (TaskArtifactUpdateEvent, TaskStatusUpdateEvent)):
                return

            # 1) 결과물 저장
            if isinstance(event, TaskArtifactUpdateEvent):
                logger.info("📄 아티팩트 업데이트 이벤트 처리 중...")
                is_final = bool(
                    getattr(event, "final", None)
                    or getattr(event, "last_chunk", None)
                    or getattr(event, "lastChunk", None)
                    or getattr(event, "last", None)
                )
                artifact_content = self._extract_payload(event)
                if is_final and self._awaiting_human:
                    # 질문 본문이다. 결과로 저장하면 COMPLETE 모드에서 SUBMITTED 되어
                    # 프로세스가 질문을 산출물로 들고 다음 단계로 넘어간다.
                    logger.info("🙋 사람 답변 대기로 종료 — 결과로 저장하지 않음 (task=%s)", self.todolist_id)
                    self._mark_human_asked_once()
                    return
                logger.info("💾 아티팩트 저장 중... (final=%s)", is_final)
                self._spawn(save_task_result(self.todolist_id, artifact_content, is_final))
                if is_final:
                    self._result_saved = True
                logger.info("✅ 아티팩트 저장 완료")
                # last_chunk=True 는 작업 완료 신호. crew_completed 도 함께 자동 발행한다.
                # framework 의 task_done() 도 동일 헬퍼를 호출하지만, 플래그로 중복 방지됨.
                if is_final:
                    self._emit_crew_completed_once(proc_inst_id_val=proc_inst_id_val)
                return

            # 2) 상태 이벤트 저장(코얼레싱 → bulk)
            if isinstance(event, TaskStatusUpdateEvent):
                logger.info("📊 상태 업데이트 이벤트 처리 중...")
                metadata: Any = getattr(event, "metadata", None) or {}
                if isinstance(metadata, ProtobufMessage):
                    metadata = MessageToDict(metadata, preserving_proto_field_name=True)

                crew_type_val = metadata.get("crew_type")
                status_obj = getattr(event, "status", None)
                state_val = getattr(status_obj, "state", None)
                if state_val == TaskState.TASK_STATE_INPUT_REQUIRED:
                    self._awaiting_human = True
                elif state_val in (TaskState.TASK_STATE_WORKING, TaskState.TASK_STATE_COMPLETED):
                    self._awaiting_human = False
                if state_val == TaskState.TASK_STATE_FAILED:
                    self._failed = True
                # 명시적 metadata 가 자동 매핑보다 우선 (explicit > implicit).
                # - metadata["event_type"] 가 있으면 그 값을 그대로 사용 (sub-event override 가능)
                # - 없으면 TaskState 기준 자동 매핑 (_STATE_TO_EVENT_TYPE)
                # - 둘 다 없으면 NULL (events.event_type 은 NULL 허용)
                event_type_val = metadata.get("event_type") or _STATE_TO_EVENT_TYPE.get(state_val)
                status_val = metadata.get("status")
                # A2A 표준에는 job_id 가 없으므로, metadata 에 명시되지 않으면 task_id 를 사용한다.
                # task_id 는 한 번의 Executor 실행 단위를 식별하므로, 그 안에서 발생한
                # 모든 상태 이벤트가 동일한 logical job 으로 묶인다 (events.job_id NOT NULL 충족).
                job_id_val = metadata.get("job_id") or str(todo_id_val)

                logger.info("🔍 이벤트 메타데이터 분석 - event_type: %s, status: %s", event_type_val, status_val)

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
        except Exception as e:
            logger.error("❌ [이벤트 페이로드 추출 실패] %s", str(e), exc_info=e)
            return {}

    def _parse_json_or_text(self, value: Any) -> Any:
        if value is None:
            return {}
        if isinstance(value, ProtobufMessage):
            value = MessageToDict(value, preserving_proto_field_name=True)
        if isinstance(value, str):
            text = value.strip()
            if not text:
                return ""
            if text.startswith(("{", "[")):
                try:
                    return json.loads(text)
                except Exception:
                    return text
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
                        txt_stripped = txt.strip()
                        if txt_stripped.startswith(("{", "[")):
                            try:
                                return json.loads(txt)
                            except Exception:
                                return txt
                        return txt
            top_text = value.get("text") or value.get("content") or value.get("data")
            if isinstance(top_text, str):
                top_text_stripped = top_text.strip()
                if top_text_stripped.startswith(("{", "[")):
                    try:
                        return json.loads(top_text)
                    except Exception:
                        return top_text
                return top_text
            return value
        return value

    def task_done(self) -> None:
        # framework 가 Executor.execute() 정상 종료 후 호출하는 안전망.
        # 일반적으로는 last_chunk=True artifact 처리 시 이미 발행되었으므로 noop이다.
        # Executor 가 final artifact 를 emit 하지 않은 케이스를 대비해 명시 호출도 지원.
        if self._awaiting_human:
            # 질문만 내고 아티팩트 없이 끝난 실행도 대기로 둔다. 안 그러면 STARTED
            # 로 남은 채 점유만 풀려 아무도 다시 집지 않는다.
            self._mark_human_asked_once()
            return
        if self.ended_in_failure:
            # 실패한 실행이다. crew_completed 를 내면 화면은 완료로 표시한다.
            logger.info("❌ 실패 상태로 끝난 실행 — 완료 이벤트를 내지 않음 (task=%s)", self.todolist_id)
            return
        self._emit_crew_completed_once()

    def _mark_human_asked_once(self) -> None:
        if self._human_asked_marked:
            return
        self._human_asked_marked = True
        self._spawn(mark_task_human_asked(self.todolist_id))

    def _emit_crew_completed_once(self, proc_inst_id_val: Optional[str] = None) -> None:
        """crew_completed 이벤트를 멱등(idempotent)하게 발행한다.

        - last_chunk=True artifact 처리 시점, 그리고 framework 의 task_done() 호출 시점
          양쪽에서 호출될 수 있지만, 인스턴스당 한 번만 events 테이블에 기록한다.
        """
        if self._completion_emitted:
            logger.debug("crew_completed 이미 발행됨, 중복 호출 무시")
            return
        self._completion_emitted = True
        try:
            logger.info("🏁 작업 완료 이벤트 생성 중...")
            payload: Dict[str, Any] = {
                "id": str(uuid.uuid4()),
                "job_id": "CREW_FINISHED",
                "todo_id": str(self.todolist_id),
                "proc_inst_id": proc_inst_id_val or self.proc_inst_id,
                "crew_type": "crew",
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


# Backward-compatible alias (external users may import this name)
ProcessGPTEventQueue = ProcessEventQueue

