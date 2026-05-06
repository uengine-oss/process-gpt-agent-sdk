import asyncio
import json
import uuid
import logging
from typing import Any, Dict, Optional

from a2a.server.events import EventQueue, Event
from a2a.types import TaskArtifactUpdateEvent, TaskState, TaskStatusUpdateEvent
from google.protobuf.json_format import MessageToDict
from google.protobuf.message import Message as ProtobufMessage

from .database import save_task_result
from .event_coalescer import enqueue_ui_event_coalesced

logger = logging.getLogger(__name__)


class ProcessEventQueue(EventQueue):
    """프로세스 모드(Task lifecycle) 전용 EventQueue.

    - TaskStatusUpdateEvent: 코얼레싱 후 bulk 저장 경로로 전달
    - TaskArtifactUpdateEvent: todolist 결과 저장
    - Task/Message 등은 저장 대상이 아니므로 무시
    """

    def __init__(self, todolist_id: str, agent_orch: str, proc_inst_id: Optional[str]):
        self.todolist_id = todolist_id
        self.agent_orch = agent_orch
        self.proc_inst_id = proc_inst_id
        super().__init__()

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
                logger.info("💾 아티팩트 저장 중... (final=%s)", is_final)
                asyncio.create_task(save_task_result(self.todolist_id, artifact_content, is_final))
                logger.info("✅ 아티팩트 저장 완료")
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
                event_type_val = {TaskState.TASK_STATE_INPUT_REQUIRED: "human_asked"}.get(state_val) or metadata.get("event_type")
                status_val = metadata.get("status")
                job_id_val = metadata.get("job_id")

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
        try:
            logger.info("🏁 작업 완료 이벤트 생성 중...")
            payload: Dict[str, Any] = {
                "id": str(uuid.uuid4()),
                "job_id": "CREW_FINISHED",
                "todo_id": str(self.todolist_id),
                "proc_inst_id": self.proc_inst_id,
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

