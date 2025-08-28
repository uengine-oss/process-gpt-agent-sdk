import asyncio
import json
import uuid
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional
import sys

from a2a.server.agent_execution import AgentExecutor, RequestContext
from a2a.server.events import EventQueue, Event

from .utils.logger import handle_application_error, write_log_message


# =============================================================================
# 시뮬레이터: ProcessGPTAgentSimulator
# 설명: 데이터베이스 연결 없이 작업을 시뮬레이션하는 핵심 시뮬레이터
# =============================================================================
class ProcessGPTAgentSimulator:
    """ProcessGPT 시뮬레이터
    
    - 데이터베이스 연결 없이 에이전트 실행을 시뮬레이션
    - CLI로 프롬프트를 받아 실행하고 진행상태를 stdout으로 출력
    """

    def __init__(self, executor: AgentExecutor, agent_orch: str = ""):
        """시뮬레이터 실행기/오케스트레이션 값을 초기화한다."""
        self.is_running = False
        self._executor: AgentExecutor = executor
        self.agent_orch: str = agent_orch or "simulator"
        self.task_id = str(uuid.uuid4())
        self.proc_inst_id = str(uuid.uuid4())

    async def run_simulation(self, prompt: str, **kwargs) -> None:
        """단일 작업을 시뮬레이션한다."""
        self.is_running = True
        write_log_message("ProcessGPT 시뮬레이터 시작")
        write_log_message(f"작업 시뮬레이션 시작: {self.task_id}")
        
        try:
            # 시뮬레이션용 작업 레코드 생성
            task_record = self._create_mock_task_record(prompt, **kwargs)
            
            # 서비스 데이터 준비 (모든 것을 모킹)
            prepared_data = self._prepare_mock_service_data(task_record)
            write_log_message(f"시뮬레이션 데이터 준비 완료 [task_id={self.task_id} agent={prepared_data.get('agent_orch','')}]")

            # 실행
            await self._execute_simulation(task_record, prepared_data)
            write_log_message(f"시뮬레이션 실행 완료 [task_id={self.task_id}]")
            
        except Exception as e:
            handle_application_error("시뮬레이션 처리 오류", e, raise_error=False)
        finally:
            self.is_running = False
            write_log_message("ProcessGPT 시뮬레이터 종료")

    def _create_mock_task_record(self, prompt: str, **kwargs) -> Dict[str, Any]:
        """시뮬레이션용 작업 레코드를 생성한다."""
        return {
            "id": self.task_id,
            "proc_inst_id": self.proc_inst_id,
            "agent_orch": self.agent_orch,
            "description": prompt,
            "activity_name": kwargs.get("activity_name", "simulation_task"),
            "user_id": kwargs.get("user_id", str(uuid.uuid4())),
            "tenant_id": kwargs.get("tenant_id", str(uuid.uuid4())),
            "tool": kwargs.get("tool", "default"),
            "feedback": kwargs.get("feedback", ""),
            "created_at": datetime.now(timezone.utc).isoformat(),
        }

    def _prepare_mock_service_data(self, task_record: Dict[str, Any]) -> Dict[str, Any]:
        """시뮬레이션용 서비스 데이터를 준비한다."""
        # 모킹된 에이전트 데이터
        mock_agents = [
            {
                "id": str(uuid.uuid4()),
                "name": "simulation_agent",
                "role": "AI Assistant",
                "goal": "Simulate task execution",
                "persona": "A helpful AI assistant for simulation",
                "tools": "mem0",
                "profile": "Simulation Agent",
                "model": "gpt-4",
                "tenant_id": task_record.get("tenant_id"),
            }
        ]

        # 모킹된 폼 데이터
        mock_form_types = [
            {"key": "default", "type": "text", "text": "Default form field"}
        ]

        # 모킹된 MCP 설정
        mock_mcp_config = {
            "enabled": True,
            "tools": ["mem0", "search"],
            "config": {}
        }

        prepared: Dict[str, Any] = {
            "task_id": str(task_record.get("id")),
            "proc_inst_id": task_record.get("proc_inst_id"),
            "agent_list": mock_agents,
            "mcp_config": mock_mcp_config,
            "form_id": "default",
            "form_types": mock_form_types,
            "form_html": "<form>Default simulation form</form>",
            "activity_name": str(task_record.get("activity_name", "")),
            "message": str(task_record.get("description", "")),
            "agent_orch": str(task_record.get("agent_orch", "")),
            "done_outputs": [],
            "output_summary": "",
            "feedback_summary": "",
            "all_users": "simulation@example.com",
        }

        return prepared

    async def _execute_simulation(self, task_record: Dict[str, Any], prepared_data: Dict[str, Any]) -> None:
        """시뮬레이션 실행을 수행한다."""
        executor = self._executor

        context = SimulatorRequestContext(prepared_data)
        event_queue = SimulatorEventQueue(task_record)

        write_log_message(f"시뮬레이션 실행 시작 [task_id={task_record.get('id')} agent={prepared_data.get('agent_orch','')}]")
        
        try:
            await executor.execute(context, event_queue)
        except Exception as e:
            handle_application_error("시뮬레이터 실행 오류", e, raise_error=False)
        finally:
            try:
                await event_queue.close()
            except Exception as e:
                handle_application_error("시뮬레이터 이벤트 큐 종료 실패", e, raise_error=False)
            write_log_message(f"시뮬레이션 실행 종료 [task_id={task_record.get('id')}]")


# =============================================================================
# 시뮬레이터 요청 컨텍스트: SimulatorRequestContext
# 설명: 시뮬레이터용 요청 데이터/상태를 캡슐화
# =============================================================================
class SimulatorRequestContext(RequestContext):
    def __init__(self, prepared_data: Dict[str, Any]):
        """시뮬레이션 실행에 필요한 데이터 묶음을 보관한다."""
        self._prepared_data = prepared_data
        self._message = prepared_data.get("message", "")
        self._current_task = None

    def get_user_input(self) -> str:
        """사용자 입력 메시지를 반환한다."""
        return self._message

    @property
    def message(self) -> str:
        """현재 메시지(사용자 입력)를 반환한다."""
        return self._message

    @property
    def current_task(self):
        """현재 실행 중 태스크(있다면)를 반환한다."""
        return getattr(self, "_current_task", None)

    def get_context_data(self) -> Dict[str, Any]:
        """실행 컨텍스트 전체 데이터를 dict로 반환한다."""
        return self._prepared_data


# =============================================================================
# 시뮬레이터 이벤트 큐: SimulatorEventQueue
# 설명: 시뮬레이터용 이벤트를 stdout으로 출력
# =============================================================================
class SimulatorEventQueue(EventQueue):
    def __init__(self, task_record: Dict[str, Any]):
        """현재 처리 중인 작업 레코드를 보관한다."""
        self.todo = task_record
        super().__init__()

    def enqueue_event(self, event: Event):
        """이벤트를 큐에 넣고, stdout으로 진행상태를 출력한다."""
        try:
            super().enqueue_event(event)
            
            # 이벤트를 stdout으로 출력
            event_data = self._convert_event_to_dict(event)
            self._output_event_to_stdout(event_data)
            
        except Exception as e:
            handle_application_error("시뮬레이터 이벤트 처리 실패", e, raise_error=False)
        
    def _convert_event_to_dict(self, event: Event) -> Dict[str, Any]:
        """이벤트를 딕셔너리로 변환한다."""
        try:
            if hasattr(event, "__dict__"):
                return {k: v for k, v in event.__dict__.items() if not k.startswith("_")}
            return {"event": str(event)}
        except Exception as e:
            handle_application_error("시뮬레이터 이벤트 변환 실패", e, raise_error=False)
            return {"event": str(event)}

    def _output_event_to_stdout(self, event_data: Dict[str, Any]) -> None:
        """이벤트 데이터를 stdout으로 출력한다."""
        try:
            # 타임스탬프 추가
            output_data = {
                "timestamp": datetime.now(timezone.utc).isoformat(),
                "task_id": self.todo.get("id"),
                "proc_inst_id": self.todo.get("proc_inst_id"),
                "event": event_data
            }
            
            # JSON 형태로 stdout에 출력
            json_output = json.dumps(output_data, ensure_ascii=False, indent=2)
            print(f"[EVENT] {json_output}", file=sys.stdout, flush=True)
            
        except Exception as e:
            handle_application_error("stdout 출력 실패", e, raise_error=False)
        
    def task_done(self) -> None:
        """태스크 완료 로그를 남긴다."""
        try:
            write_log_message(f"시뮬레이션 태스크 완료: {self.todo['id']}")
            self._output_event_to_stdout({"type": "task_completed", "message": "Task simulation completed"})
        except Exception as e:
            handle_application_error("시뮬레이터 태스크 완료 처리 실패", e, raise_error=False)

    async def close(self) -> None:
        """큐 종료 훅."""
        self._output_event_to_stdout({"type": "queue_closed", "message": "Event queue closed"})
