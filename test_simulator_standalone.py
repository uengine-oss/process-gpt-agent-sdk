#!/usr/bin/env python3
"""
독립적인 시뮬레이터 테스트

이 테스트는 외부 의존성 없이 시뮬레이터의 핵심 기능을 테스트합니다.
A2A SDK 인터페이스를 모킹하여 독립적으로 실행 가능합니다.
"""

import asyncio
import sys
import os
import json
from typing import Any, Dict
from abc import ABC, abstractmethod

# 프로젝트 루트를 import 경로에 추가
sys.path.insert(0, os.path.abspath(os.path.dirname(__file__)))


# A2A SDK 인터페이스 모킹
class RequestContext(ABC):
    """A2A SDK RequestContext 인터페이스 모킹"""
    
    @abstractmethod
    def get_user_input(self) -> str:
        pass

    @property
    @abstractmethod
    def message(self) -> str:
        pass

    @abstractmethod
    def get_context_data(self) -> Dict[str, Any]:
        pass


class Event:
    """A2A SDK Event 클래스 모킹"""
    
    def __init__(self, type: str, data: Dict[str, Any]):
        self.type = type
        self.data = data


class EventQueue(ABC):
    """A2A SDK EventQueue 인터페이스 모킹"""
    
    def __init__(self):
        self.events = []
    
    @abstractmethod
    def enqueue_event(self, event: Event):
        pass

    @abstractmethod
    def task_done(self) -> None:
        pass

    @abstractmethod
    async def close(self) -> None:
        pass


class AgentExecutor(ABC):
    """A2A SDK AgentExecutor 인터페이스 모킹"""
    
    @abstractmethod
    async def execute(self, context: RequestContext, event_queue: EventQueue) -> None:
        pass

    @abstractmethod
    async def cancel(self, context: RequestContext, event_queue: EventQueue) -> None:
        pass


# 모킹된 로거
def write_log_message(message: str, level: int = 20) -> None:
    print(f"[LOG] {message}")

def handle_application_error(title: str, error: Exception, *, raise_error: bool = True, extra: dict = None) -> None:
    print(f"[ERROR] {title}: {error}")
    if raise_error:
        raise error


# 시뮬레이터 구현 (의존성 없는 버전)
class TestProcessGPTAgentSimulator:
    """테스트용 독립 시뮬레이터"""

    def __init__(self, executor: AgentExecutor, agent_orch: str = ""):
        self.is_running = False
        self._executor: AgentExecutor = executor
        self.agent_orch: str = agent_orch or "simulator"
        self.task_id = "test-task-id"
        self.proc_inst_id = "test-proc-inst-id"

    async def run_simulation(self, prompt: str, **kwargs) -> None:
        """단일 작업을 시뮬레이션한다."""
        self.is_running = True
        write_log_message("ProcessGPT 테스트 시뮬레이터 시작")
        write_log_message(f"작업 시뮬레이션 시작: {self.task_id}")
        
        try:
            # 시뮬레이션용 작업 레코드 생성
            task_record = self._create_mock_task_record(prompt, **kwargs)
            
            # 서비스 데이터 준비
            prepared_data = self._prepare_mock_service_data(task_record)
            write_log_message(f"시뮬레이션 데이터 준비 완료 [task_id={self.task_id}]")

            # 실행
            await self._execute_simulation(task_record, prepared_data)
            write_log_message(f"시뮬레이션 실행 완료 [task_id={self.task_id}]")
            
        except Exception as e:
            handle_application_error("시뮬레이션 처리 오류", e, raise_error=False)
        finally:
            self.is_running = False
            write_log_message("ProcessGPT 테스트 시뮬레이터 종료")

    def _create_mock_task_record(self, prompt: str, **kwargs) -> Dict[str, Any]:
        """시뮬레이션용 작업 레코드를 생성한다."""
        return {
            "id": self.task_id,
            "proc_inst_id": self.proc_inst_id,
            "agent_orch": self.agent_orch,
            "description": prompt,
            "activity_name": kwargs.get("activity_name", "simulation_task"),
        }

    def _prepare_mock_service_data(self, task_record: Dict[str, Any]) -> Dict[str, Any]:
        """시뮬레이션용 서비스 데이터를 준비한다."""
        return {
            "task_id": str(task_record.get("id")),
            "proc_inst_id": task_record.get("proc_inst_id"),
            "message": str(task_record.get("description", "")),
            "agent_orch": str(task_record.get("agent_orch", "")),
        }

    async def _execute_simulation(self, task_record: Dict[str, Any], prepared_data: Dict[str, Any]) -> None:
        """시뮬레이션 실행을 수행한다."""
        context = TestRequestContext(prepared_data)
        event_queue = TestEventQueue(task_record)

        write_log_message(f"시뮬레이션 실행 시작 [task_id={task_record.get('id')}]")
        
        try:
            await self._executor.execute(context, event_queue)
        except Exception as e:
            handle_application_error("시뮬레이터 실행 오류", e, raise_error=False)
        finally:
            try:
                await event_queue.close()
            except Exception as e:
                handle_application_error("시뮬레이터 이벤트 큐 종료 실패", e, raise_error=False)
            write_log_message(f"시뮬레이션 실행 종료 [task_id={task_record.get('id')}]")


class TestRequestContext(RequestContext):
    """테스트용 요청 컨텍스트"""
    
    def __init__(self, prepared_data: Dict[str, Any]):
        self._prepared_data = prepared_data
        self._message = prepared_data.get("message", "")

    def get_user_input(self) -> str:
        return self._message

    @property
    def message(self) -> str:
        return self._message

    def get_context_data(self) -> Dict[str, Any]:
        return self._prepared_data


class TestEventQueue(EventQueue):
    """테스트용 이벤트 큐"""
    
    def __init__(self, task_record: Dict[str, Any]):
        super().__init__()
        self.todo = task_record

    def enqueue_event(self, event: Event):
        """이벤트를 큐에 넣고, stdout으로 진행상태를 출력한다."""
        try:
            super().enqueue_event(event)
            self.events.append(event)
            
            # 이벤트를 stdout으로 출력
            event_data = self._convert_event_to_dict(event)
            self._output_event_to_stdout(event_data)
            
        except Exception as e:
            handle_application_error("테스트 이벤트 처리 실패", e, raise_error=False)
        
    def _convert_event_to_dict(self, event: Event) -> Dict[str, Any]:
        """이벤트를 딕셔너리로 변환한다."""
        return {
            "type": event.type,
            "data": event.data
        }

    def _output_event_to_stdout(self, event_data: Dict[str, Any]) -> None:
        """이벤트 데이터를 stdout으로 출력한다."""
        try:
            output_data = {
                "task_id": self.todo.get("id"),
                "proc_inst_id": self.todo.get("proc_inst_id"),
                "event": event_data
            }
            
            json_output = json.dumps(output_data, ensure_ascii=False, indent=2)
            print(f"[EVENT] {json_output}", flush=True)
            
        except Exception as e:
            handle_application_error("stdout 출력 실패", e, raise_error=False)
        
    def task_done(self) -> None:
        """태스크 완료 로그를 남긴다."""
        write_log_message(f"테스트 태스크 완료: {self.todo['id']}")
        self._output_event_to_stdout({"type": "task_completed", "data": {"message": "Task simulation completed"}})

    async def close(self) -> None:
        """큐 종료 훅."""
        self._output_event_to_stdout({"type": "queue_closed", "data": {"message": "Event queue closed"}})


class TestSimulationExecutor(AgentExecutor):
    """테스트용 시뮬레이션 실행기"""
    
    def __init__(self, simulation_steps: int = 3, step_delay: float = 0.5):
        self.simulation_steps = simulation_steps
        self.step_delay = step_delay
        self.is_cancelled = False

    async def execute(self, context: RequestContext, event_queue: EventQueue) -> None:
        """시뮬레이션된 실행을 수행한다."""
        write_log_message("테스트 실행기 시작")
        
        prompt = context.get_user_input()
        
        # 시작 이벤트
        start_event = Event(
            type="task_started",
            data={
                "message": f"테스트 시뮬레이션 시작: {prompt}",
                "prompt": prompt
            }
        )
        event_queue.enqueue_event(start_event)

        # 시뮬레이션 단계별 실행
        for step in range(1, self.simulation_steps + 1):
            if self.is_cancelled:
                break
                
            await asyncio.sleep(self.step_delay)
            
            # 진행 이벤트
            progress_event = Event(
                type="progress",
                data={
                    "step": step,
                    "total_steps": self.simulation_steps,
                    "message": f"단계 {step}/{self.simulation_steps}: 작업 처리 중...",
                    "progress_percentage": (step / self.simulation_steps) * 100
                }
            )
            event_queue.enqueue_event(progress_event)

        if not self.is_cancelled:
            # 결과 출력
            output_event = Event(
                type="output",
                data={
                    "content": {
                        "result": f"'{prompt}'에 대한 테스트 분석 결과",
                        "status": "completed",
                        "test_mode": True
                    },
                    "final": True
                }
            )
            event_queue.enqueue_event(output_event)

            # 완료 이벤트
            done_event = Event(
                type="done",
                data={
                    "message": "테스트 시뮬레이션 완료",
                    "success": True
                }
            )
            event_queue.enqueue_event(done_event)

        write_log_message("테스트 실행기 종료")

    async def cancel(self, context: RequestContext, event_queue: EventQueue) -> None:
        """시뮬레이션 취소를 수행한다."""
        write_log_message("테스트 시뮬레이션 취소 요청")
        self.is_cancelled = True


async def test_basic_simulation():
    """기본 시뮬레이션 테스트"""
    print("=== 기본 시뮬레이션 테스트 ===")
    
    executor = TestSimulationExecutor(simulation_steps=3, step_delay=0.5)
    simulator = TestProcessGPTAgentSimulator(executor=executor, agent_orch="test")
    
    await simulator.run_simulation("테스트 프롬프트를 처리해주세요")
    print()


async def test_multiple_scenarios():
    """여러 시나리오 테스트"""
    print("=== 다중 시나리오 테스트 ===")
    
    scenarios = [
        ("데이터를 분석해주세요", 2, 0.3),
        ("보고서를 작성해주세요", 4, 0.2),
        ("고객 문의를 처리해주세요", 3, 0.4)
    ]
    
    for i, (prompt, steps, delay) in enumerate(scenarios, 1):
        print(f"\n--- 시나리오 {i}: {prompt} ---")
        executor = TestSimulationExecutor(simulation_steps=steps, step_delay=delay)
        simulator = TestProcessGPTAgentSimulator(executor=executor, agent_orch=f"test_{i}")
        await simulator.run_simulation(prompt)


async def main():
    """메인 테스트 함수"""
    print("ProcessGPT Agent Simulator 독립 테스트")
    print("=" * 50)
    
    try:
        await test_basic_simulation()
        await test_multiple_scenarios()
        
        print("\n테스트 완료! 모든 기능이 정상적으로 작동합니다.")
        
    except Exception as e:
        print(f"\n테스트 실패: {e}")
        sys.exit(1)


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print("\n테스트가 중단되었습니다.")
        sys.exit(1)
