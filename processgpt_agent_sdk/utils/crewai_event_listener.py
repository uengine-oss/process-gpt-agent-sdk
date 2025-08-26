from __future__ import annotations

import os
import json
import uuid
from datetime import datetime, timezone
from typing import Any, Optional, Dict, List

from crewai.utilities.events import CrewAIEventsBus, ToolUsageStartedEvent, ToolUsageFinishedEvent
from crewai.utilities.events.task_events import TaskStartedEvent, TaskCompletedEvent

from .logger import handle_application_error, write_log_message
from .context_manager import todo_id_var, proc_id_var, crew_type_var, form_id_var, form_key_var
from ..core.database import initialize_db, get_db_client


class CrewAIEventLogger:
    """CrewAI 이벤트 로거 - Supabase 전용"""

    # =============================================================================
    # Initialization
    # =============================================================================
    def __init__(self):
        """Supabase 클라이언트를 초기화한다."""
        initialize_db()
        self.supabase = get_db_client()
        write_log_message("CrewAIEventLogger 초기화 완료")

    # =============================================================================
    # Job ID Generation
    # =============================================================================
    def _generate_job_id(self, event_obj: Any, source: Any = None) -> str:
        """이벤트 객체에서 Job ID 생성"""
        try:
            if hasattr(event_obj, "task") and hasattr(event_obj.task, "id"):
                return str(event_obj.task.id)
            if source and hasattr(source, "task") and hasattr(source.task, "id"):
                return str(source.task.id)
        except Exception:
            pass
        return "unknown"

    # =============================================================================
    # Record Creation
    # =============================================================================
    def _create_event_record(
        self,
        event_type: str,
        data: Dict[str, Any],
        job_id: str,
        crew_type: str,
        todo_id: Optional[str],
        proc_inst_id: Optional[str],
    ) -> Dict[str, Any]:
        """이벤트 레코드 생성"""
        return {
            "id": str(uuid.uuid4()),
            "job_id": job_id,
            "todo_id": todo_id,
            "proc_inst_id": proc_inst_id,
            "event_type": event_type,
            "crew_type": crew_type,
            "data": data,
            "timestamp": datetime.now(timezone.utc).isoformat(),
        }

    # =============================================================================
    # Parsing Helpers
    # =============================================================================
    def _parse_json_text(self, text: str) -> Any:
        """JSON 문자열을 객체로 파싱하거나 원본 반환"""
        try:
            return json.loads(text)
        except:
            return text

    def _parse_output(self, output: Any) -> Any:
        """output 또는 raw 텍스트를 파싱해 반환"""
        if not output:
            return ""
        text = getattr(output, "raw", None) or (output if isinstance(output, str) else "")
        return self._parse_json_text(text)

    def _parse_tool_args(self, args_text: str) -> Optional[str]:
        """tool_args에서 query 키 추출"""
        try:
            args = json.loads(args_text or "{}")
            return args.get("query")
        except Exception:
            return None

    # =============================================================================
    # Formatting Helpers
    # =============================================================================
    def _format_plans_md(self, plans: List[Dict[str, Any]]) -> str:
        """list_of_plans_per_task 형식을 Markdown 문자열로 변환"""
        lines: List[str] = []
        for idx, item in enumerate(plans or [], 1):
            task = item.get("task", "")
            plan = item.get("plan", "")
            lines.append(f"## {idx}. {task}")
            lines.append("")
            if isinstance(plan, list):
                for line in plan:
                    lines.append(str(line))
            elif isinstance(plan, str):
                for line in plan.split("\n"):
                    lines.append(line)
            else:
                lines.append(str(plan))
            lines.append("")
        return "\n".join(lines).strip()

    # =============================================================================
    # Data Extraction
    # =============================================================================
    def _extract_event_data(self, event_obj: Any, source: Any = None) -> Dict[str, Any]:
        """이벤트 타입별 데이터 추출"""
        etype = getattr(event_obj, "type", None) or type(event_obj).__name__
        if etype == "task_started":
            agent = getattr(getattr(event_obj, "task", None), "agent", None)
            return {
                "role": getattr(agent, "role", "Unknown"),
                "goal": getattr(agent, "goal", "Unknown"),
                "agent_profile": getattr(agent, "profile", None) or "/images/chat-icon.png",
                "name": getattr(agent, "name", "Unknown"),
            }
        if etype == "task_completed":
            result = self._parse_output(getattr(event_obj, "output", None))
            if isinstance(result, dict) and "list_of_plans_per_task" in result:
                md = self._format_plans_md(result.get("list_of_plans_per_task") or [])
                return {"plans": md}
            return {"result": result}

        if etype in ("tool_usage_started", "tool_usage_finished") or str(etype).startswith("tool_"):
            return {
                "tool_name": getattr(event_obj, "tool_name", None),
                "query": self._parse_tool_args(getattr(event_obj, "tool_args", "")),
            }
        return {"info": f"Event type: {etype}"}

    # =============================================================================
    # Event Saving
    # =============================================================================
    def _save_event(self, record: Dict[str, Any]) -> None:
        """Supabase에 이벤트 레코드 저장 (간단 재시도 포함)"""
        payload = json.loads(json.dumps(record, default=str))
        for attempt in range(1, 4):
            try:
                self.supabase.table("events").insert(payload).execute()
                return
            except Exception as e:
                if attempt < 3:
                    handle_application_error("이벤트저장오류(재시도)", e, raise_error=False)
                    import time
                    time.sleep(0.3 * attempt)
                    continue
                handle_application_error("이벤트저장오류(최종)", e, raise_error=False)
                return

    # =============================================================================
    # Event Handling
    # =============================================================================
    def on_event(self, event_obj: Any, source: Any = None) -> None:
        """이벤트 수신부터 DB 저장까지 처리"""
        etype = getattr(event_obj, "type", None) or type(event_obj).__name__
        ALLOWED = {"task_started", "task_completed", "tool_usage_started", "tool_usage_finished"}
        if etype not in ALLOWED:
            return
        try:
            job_id = self._generate_job_id(event_obj, source)
            data = self._extract_event_data(event_obj, source)
            crew_type = crew_type_var.get() or "action"
            rec = self._create_event_record(etype, data, job_id, crew_type, todo_id_var.get(), proc_id_var.get())
            self._save_event(rec)
            write_log_message(f"[{etype}] [{job_id[:8]}] 저장 완료")
        except Exception as e:
            handle_application_error("이벤트처리오류", e, raise_error=False)


# =============================================================================
# CrewConfigManager
# 설명: 이벤트 리스너를 프로세스 단위로 1회 등록
# =============================================================================
class CrewConfigManager:
    """글로벌 CrewAI 이벤트 리스너 등록 매니저"""
    _registered_by_pid: set[int] = set()

    def __init__(self) -> None:
        self.logger = CrewAIEventLogger()
        self._register_once_per_process()

    def _register_once_per_process(self) -> None:
        """현재 프로세스에만 한 번 이벤트 리스너를 등록한다."""
        try:
            pid = os.getpid()
            if pid in self._registered_by_pid:
                return
            bus = CrewAIEventsBus()
            for evt in (TaskStartedEvent, TaskCompletedEvent, ToolUsageStartedEvent, ToolUsageFinishedEvent):
                bus.on(evt)(lambda source, event, logger=self.logger: logger.on_event(event, source))
            self._registered_by_pid.add(pid)
            write_log_message("CrewAI event listeners 등록 완료")
        except Exception as e:
            handle_application_error("CrewAI 이벤트 버스 등록 실패", e, raise_error=False)
