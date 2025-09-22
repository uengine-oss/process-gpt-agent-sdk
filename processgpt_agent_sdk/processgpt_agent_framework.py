import asyncio
import logging
import json
from datetime import datetime
from typing import Dict, Any, Optional
from dataclasses import dataclass
import uuid
import os
from dotenv import load_dotenv

from a2a.server.agent_execution import AgentExecutor, RequestContext
from a2a.server.events import EventQueue, Event
from a2a.types import (
    TaskArtifactUpdateEvent,
    TaskState,
    TaskStatusUpdateEvent,
)

# DB 어댑터 사용
from .database import (
    initialize_db,
    polling_pending_todos,
    record_event,
    save_task_result,
    update_task_error,
    get_consumer_id,
    fetch_agent_data,
    fetch_all_agents,
    fetch_form_types,
    fetch_tenant_mcp_config,
    fetch_human_users_by_proc_inst_id,
)

load_dotenv()

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

@dataclass
class TodoListRowContext:
    """fetch_pending_task/… 로부터 받은 todolist 행을 감싼 컨텍스트용 DTO"""
    row: Dict[str, Any]

class ProcessGPTRequestContext(RequestContext):
    """DB row(스키마 준수) 기반 RequestContext 구현"""
    def __init__(self, row: Dict[str, Any]):
        self.row = row
        self._user_input = (row.get('description') or '').strip()
        self._message = self._user_input
        self._current_task = None
        self._task_state = row.get('draft_status') or ''
        self._extra_context: Dict[str, Any] = {}

    async def prepare_context(self) -> None:
        """database.py를 활용해 부가 컨텍스트를 미리 준비한다. 실패해도 기본값으로 계속 진행."""
        logger.info("\n🔧 컨텍스트 준비 중...")
        logger.info("   📋 Task: %s", self.row.get('id'))
        logger.info("   🛠️  Tool: %s", self.row.get('tool') or 'N/A')
        logger.info("   🏢 Tenant: %s", self.row.get('tenant_id') or 'N/A')
        effective_proc_inst_id = self.row.get('root_proc_inst_id') or self.row.get('proc_inst_id')
        tool_val = self.row.get('tool') or ''
        tenant_id = self.row.get('tenant_id') or ''
        user_ids = self.row.get('user_id') or ''

        try:
            notif_task = fetch_human_users_by_proc_inst_id(effective_proc_inst_id)
            mcp_task = fetch_tenant_mcp_config(tenant_id)
            form_task = fetch_form_types(tool_val, tenant_id)
            agents_task = fetch_agent_data(user_ids or '')

            notify_emails, tenant_mcp, form_tuple, agents = await asyncio.gather(
                notif_task, mcp_task, form_task, agents_task
            )

            if not agents:
                agents = await fetch_all_agents()

            form_id, form_fields, form_html = form_tuple
        except Exception as e:
            logger.exception(
                "prepare_context failed (proc_inst_id=%s, todolist_id=%s): %s",
                effective_proc_inst_id,
                self.row.get('id'),
                str(e),
            )
            notify_emails, tenant_mcp = "", None
            form_id, form_fields, form_html = None, [], None
            agents = []

        self._extra_context = {
            'id': self.row.get('id'),
            'proc_inst_id': effective_proc_inst_id,
            'root_proc_inst_id': self.row.get('root_proc_inst_id'),
            'activity_name': self.row.get('activity_name'),
            'agents': agents,
            'tenant_mcp': tenant_mcp,
            'form_fields': form_fields,
            'form_html': form_html,
            'form_id': form_id,
            'notify_user_emails': notify_emails,
        }
        logger.info("\n✅ 컨텍스트 준비 완료!")
        logger.info("   📋 Task: %s", self.row.get('id'))
        logger.info("   🤖 Agents: %d개", len(agents) if isinstance(agents, list) else 0)
        logger.info("   ⚡ Activity: %s", self.row.get('activity_name'))
        logger.info("   🏭 Process: %s", self.row.get('proc_inst_id'))
        logger.info("   🏢 Tenant: %s", self.row.get('tenant_id'))
        logger.info("")

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
        return {
            'row': self.row,
            'extras': self._extra_context,
        }

class ProcessGPTEventQueue(EventQueue):
    """Events 테이블에 이벤트를 저장하는 EventQueue 구현 (database.record_event 사용)"""

    def __init__(self, todolist_id: str, agent_orch: str, proc_inst_id: Optional[str]):
        self.todolist_id = todolist_id
        self.agent_orch = agent_orch
        self.proc_inst_id = proc_inst_id
        super().__init__()

    def enqueue_event(self, event: Event):
        """A2A 이벤트 처리"""
        try:
            # 공식 식별자만 공통 추출 (metadata는 타입별로 필요 시만 읽음)
            proc_inst_id_val = getattr(event, 'contextId', None) or self.proc_inst_id
            todo_id_val = getattr(event, 'taskId', None) or str(self.todolist_id)
            logger.info("\n📨 이벤트 수신: %s", type(event).__name__)
            logger.info("   📋 Task: %s", self.todolist_id)
            logger.info("   🔄 Process: %s", proc_inst_id_val)

            # 1) 기본 매핑: Artifact → todolist 저장 (오직 결과물만). 실패해도 진행
            if isinstance(event, TaskArtifactUpdateEvent):
                try:
                    is_final = bool(
                        getattr(event, 'final', None)
                        or getattr(event, 'lastChunk', None)
                        or getattr(event, 'last_chunk', None)
                        or getattr(event, 'last', None)
                    )
                    artifact_content = self._extract_payload(event)
                    logger.info("\n💾 아티팩트 저장 중...")
                    logger.info("   📋 Task: %s", self.todolist_id)
                    logger.info("   🏁 Final: %s", "예" if is_final else "아니오")
                    asyncio.create_task(save_task_result(self.todolist_id, artifact_content, is_final))
                    logger.info("   ✅ 저장 완료!\n")
                except Exception as e:
                    logger.exception(
                        "enqueue_event artifact save failed (todolist_id=%s, proc_inst_id=%s, event=%s): %s",
                        self.todolist_id,
                        self.proc_inst_id,
                        type(event).__name__,
                        str(e),
                    )
                return

            # 2) 기본 매핑: Status → events 저장 (crew_type/event_type/status/job_id는 metadata). 실패해도 진행
            if isinstance(event, TaskStatusUpdateEvent):
                metadata = getattr(event, 'metadata', None)
                if not isinstance(metadata, dict):
                    metadata = {}
                crew_type_val = metadata.get('crew_type')
                # 상태 기반 event_type 매핑 (input_required -> human_asked)
                status_obj = getattr(event, 'status', None)
                state_val = getattr(status_obj, 'state', None)
                event_type_val = {TaskState.input_required: 'human_asked'}.get(state_val) or metadata.get('event_type')
                status_val = metadata.get('status')
                job_id_val = metadata.get('job_id')
                try:
                    payload: Dict[str, Any] = {
                        'id': str(uuid.uuid4()),
                        'job_id': job_id_val,
                        'todo_id': str(todo_id_val),
                        'proc_inst_id': proc_inst_id_val,
                        'crew_type': crew_type_val,
                        'event_type': event_type_val,
                        'data': self._extract_payload(event),
                        'status': status_val or None,
                    }

                    logger.info("\n📝 상태 이벤트 기록 중...")
                    logger.info("   📋 Task: %s", self.todolist_id)
                    logger.info("   🆔 Job: %s", job_id_val or 'N/A')
                    logger.info("   🏷️  Type: %s", crew_type_val or 'N/A')
                    logger.info("   🔍 Event: %s", event_type_val or 'N/A')
                    asyncio.create_task(record_event(payload))
                    logger.info("   ✅ 기록 완료!\n")
                except Exception as e:
                    logger.exception(
                        "enqueue_event status record failed (todolist_id=%s, proc_inst_id=%s, job_id=%s, crew_type=%s): %s",
                        self.todolist_id,
                        self.proc_inst_id,
                        job_id_val,
                        crew_type_val,
                        str(e),
                    )
                return

        except Exception as e:
            logger.error(f"Failed to enqueue event: {e}")
            raise

    def _extract_payload(self, event: Event) -> Any:
        """이벤트에서 실질 페이로드를 추출한다."""
        try:
            artifact_or_none = getattr(event, 'artifact', None)
            status_or_none = getattr(event, 'status', None)
            message_or_none = getattr(status_or_none, 'message', None)

            source = artifact_or_none if artifact_or_none is not None else message_or_none
            return self._parse_json_or_text(source)
        except Exception:
            return {}

    def _parse_json_or_text(self, value: Any) -> Any:
        """간소화: new_* 유틸 출력(dict)과 문자열만 처리하여 순수 payload 반환."""
        try:
            # 1) None → 빈 구조
            if value is None:
                return {}

            # 2) 문자열이면 JSON 파싱 시도
            if isinstance(value, str):
                text = value.strip()
                if not text:
                    return ""
                try:
                    return json.loads(text)
                except Exception:
                    return text

            # 3) 모델 → dict로 정규화 (있으면만)
            try:
                if hasattr(value, "model_dump") and callable(getattr(value, "model_dump")):
                    value = value.model_dump()
            except Exception:
                pass
            try:
                if not isinstance(value, dict) and hasattr(value, "dict") and callable(getattr(value, "dict")):
                    value = value.dict()
            except Exception:
                pass
            if not isinstance(value, dict) and hasattr(value, "__dict__"):
                value = value.__dict__

            # 4) dict만 대상으로 parts[0].text → parts[0].root.text → top-level text/content/data 순으로 추출
            if isinstance(value, dict):
                parts = value.get("parts")
                if isinstance(parts, list) and parts:
                    first = parts[0] if isinstance(parts[0], dict) else None
                    if isinstance(first, dict):
                        text_candidate = (
                            first.get("text") or first.get("content") or first.get("data")
                        )
                        if not isinstance(text_candidate, str):
                            root = first.get("root") if isinstance(first.get("root"), dict) else None
                            if root:
                                text_candidate = (
                                    root.get("text") or root.get("content") or root.get("data")
                                )
                        if isinstance(text_candidate, str):
                            return self._parse_json_or_text(text_candidate)
                top_text = value.get("text") or value.get("content") or value.get("data")
                if isinstance(top_text, str):
                    return self._parse_json_or_text(top_text)
                return value

            # 5) 그 외 타입은 원형 반환
            return value
        except Exception:
            return {}

    def task_done(self) -> None:
        try:
            payload: Dict[str, Any] = {
                'id': str(uuid.uuid4()),
                'job_id': "CREW_FINISHED",
                'todo_id': str(self.todolist_id),
                'proc_inst_id': self.proc_inst_id,
                'crew_type': "crew",
                'data': "Task completed successfully",
                'event_type': 'crew_completed',
                'status': None,
            }
            asyncio.create_task(record_event(payload))
            logger.info("\n🏁 작업 완료 기록됨: %s\n", self.todolist_id)
        except Exception as e:
            logger.error(f"Failed to record task completion: {e}")
            raise

class ProcessGPTAgentServer:
    """DB 폴링 기반 Agent Server (database.py 사용)"""

    def __init__(self, agent_executor: AgentExecutor, agent_type: str):
        self.agent_executor = agent_executor
        self.agent_orch = agent_type
        self.polling_interval = 5  # seconds
        self.is_running = False

    async def run(self):
        """메인 실행 루프"""
        self.is_running = True
        logger.info("\n\n🚀 ===============================================")
        logger.info("   ProcessGPT Agent Server STARTED")
        logger.info(f"   Agent Type: {self.agent_orch}")
        logger.info("===============================================\n")
        initialize_db()
        
        while self.is_running:
            try:
                logger.info("\n🔍 Polling for tasks (agent_orch=%s)...", self.agent_orch)
                row = await polling_pending_todos(self.agent_orch, get_consumer_id())

                if row:
                    logger.info("\n\n✅ 새 작업 발견!")
                    logger.info("📋 Task ID: %s", row.get('id'))
                    logger.info("🔄 Process: %s", row.get('proc_inst_id'))
                    logger.info("⚡ Activity: %s", row.get('activity_name'))
                    logger.info("")
                    try:
                        await self.process_todolist_item(row)
                    except Exception as e:
                        logger.exception("process_todolist_item failed: %s", str(e))
                        try:
                            await self.mark_task_failed(str(row.get('id')), str(e))
                        except Exception as ee:
                            logger.exception("mark_task_failed failed: %s", str(ee))
                await asyncio.sleep(self.polling_interval)

            except Exception as e:
                logger.exception(
                    "run loop error (agent_orch=%s): %s",
                    self.agent_orch,
                    str(e),
                )
                await asyncio.sleep(self.polling_interval)

    async def process_todolist_item(self, row: Dict[str, Any]):
        """개별 todolist 항목을 처리"""
        logger.info("\n\n🎯 ============== 작업 처리 시작 ==============")
        logger.info("📝 Task ID: %s", row.get('id'))
        logger.info("🔧 Tool: %s", row.get('tool'))
        logger.info("🏭 Process: %s", row.get('proc_inst_id'))
        logger.info("=" * 50 + "\n")
        
        try:
            context = ProcessGPTRequestContext(row)
            await context.prepare_context()
            event_queue = ProcessGPTEventQueue(str(row.get('id')), self.agent_orch, row.get('proc_inst_id'))
            await self.agent_executor.execute(context, event_queue)
            event_queue.task_done()
            logger.info("\n\n🎉 ============== 작업 완료 ==============")
            logger.info("✨ Task ID: %s", row.get('id'))
            logger.info("=" * 45 + "\n\n")
            
        except Exception as e:
            logger.exception(
                "process_todolist_item error (todolist_id=%s, proc_inst_id=%s): %s",
                row.get('id'),
                row.get('proc_inst_id'),
                str(e),
            )
            await self.mark_task_failed(str(row.get('id')), str(e))
            raise

    async def mark_task_failed(self, todolist_id: str, error_message: str):
        """태스크 실패 처리 (DB 상태 업데이트)"""
        try:
            await update_task_error(todolist_id)
        except Exception as e:
            logger.exception(
                "mark_task_failed error (todolist_id=%s): %s",
                todolist_id,
                str(e),
            )

    def stop(self):
        """서버 중지"""
        self.is_running = False
        logger.info("ProcessGPT Agent Server stopped")