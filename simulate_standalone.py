#!/usr/bin/env python3
"""
독립적인 ProcessGPT Agent Simulator CLI Tool

데이터베이스 연결과 외부 의존성 없이 ProcessGPT 에이전트를 시뮬레이션하는 CLI 도구입니다.
프롬프트를 CLI 인자로 받고, 진행 상태 이벤트를 stdout으로 출력합니다.

Usage:
    python simulate_standalone.py "Your prompt here"
    python simulate_standalone.py "Analyze the data" --agent-orch "data_analysis" --steps 3
"""

import asyncio
import argparse
import sys
import json
from typing import Any, Dict, List
from abc import ABC, abstractmethod
from datetime import datetime, timezone
import uuid


# 기본 인터페이스 정의
class RequestContext(ABC):
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
    def __init__(self, type: str, data: Dict[str, Any]):
        self.type = type
        self.data = data


class EventQueue(ABC):
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
    @abstractmethod
    async def execute(self, context: RequestContext, event_queue: EventQueue) -> None:
        pass

    @abstractmethod
    async def cancel(self, context: RequestContext, event_queue: EventQueue) -> None:
        pass


# 로깅 함수
def write_log_message(message: str, verbose: bool = False) -> None:
    if verbose:
        print(f"[LOG] {message}", file=sys.stderr)


def handle_application_error(title: str, error: Exception, *, raise_error: bool = True, verbose: bool = False) -> None:
    if verbose:
        print(f"[ERROR] {title}: {error}", file=sys.stderr)
    if raise_error:
        raise error


# 시뮬레이터 구현
class StandaloneProcessGPTAgentSimulator:
    """독립적인 ProcessGPT 시뮬레이터"""

    def __init__(self, executor: AgentExecutor, agent_orch: str = "", verbose: bool = False):
        self.is_running = False
        self._executor: AgentExecutor = executor
        self.agent_orch: str = agent_orch or "simulator"
        self.task_id = str(uuid.uuid4())
        self.proc_inst_id = str(uuid.uuid4())
        self.verbose = verbose

    async def run_simulation(self, prompt: str, **kwargs) -> None:
        """단일 작업을 시뮬레이션한다."""
        self.is_running = True
        write_log_message("ProcessGPT 시뮬레이터 시작", self.verbose)
        write_log_message(f"작업 시뮬레이션 시작: {self.task_id}", self.verbose)
        
        try:
            # 시뮬레이션용 작업 레코드 생성
            task_record = self._create_mock_task_record(prompt, **kwargs)
            
            # 서비스 데이터 준비
            prepared_data = self._prepare_mock_service_data(task_record)
            write_log_message(f"시뮬레이션 데이터 준비 완료 [task_id={self.task_id}]", self.verbose)

            # 실행
            await self._execute_simulation(task_record, prepared_data)
            write_log_message(f"시뮬레이션 실행 완료 [task_id={self.task_id}]", self.verbose)
            
        except Exception as e:
            handle_application_error("시뮬레이션 처리 오류", e, raise_error=False, verbose=self.verbose)
        finally:
            self.is_running = False
            write_log_message("ProcessGPT 시뮬레이터 종료", self.verbose)

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
        return {
            "task_id": str(task_record.get("id")),
            "proc_inst_id": task_record.get("proc_inst_id"),
            "agent_list": [{"id": str(uuid.uuid4()), "name": "simulation_agent"}],
            "message": str(task_record.get("description", "")),
            "agent_orch": str(task_record.get("agent_orch", "")),
        }

    async def _execute_simulation(self, task_record: Dict[str, Any], prepared_data: Dict[str, Any]) -> None:
        """시뮬레이션 실행을 수행한다."""
        context = StandaloneRequestContext(prepared_data)
        event_queue = StandaloneEventQueue(task_record, self.verbose)

        write_log_message(f"시뮬레이션 실행 시작 [task_id={task_record.get('id')}]", self.verbose)
        
        try:
            await self._executor.execute(context, event_queue)
        except Exception as e:
            handle_application_error("시뮬레이터 실행 오류", e, raise_error=False, verbose=self.verbose)
        finally:
            try:
                await event_queue.close()
            except Exception as e:
                handle_application_error("시뮬레이터 이벤트 큐 종료 실패", e, raise_error=False, verbose=self.verbose)
            write_log_message(f"시뮬레이션 실행 종료 [task_id={task_record.get('id')}]", self.verbose)


class StandaloneRequestContext(RequestContext):
    """독립적인 요청 컨텍스트"""
    
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


class StandaloneEventQueue(EventQueue):
    """독립적인 이벤트 큐"""
    
    def __init__(self, task_record: Dict[str, Any], verbose: bool = False):
        super().__init__()
        self.todo = task_record
        self.verbose = verbose

    def enqueue_event(self, event: Event):
        """이벤트를 큐에 넣고, stdout으로 진행상태를 출력한다."""
        try:
            super().enqueue_event(event)
            self.events.append(event)
            
            # 이벤트를 stdout으로 출력
            event_data = self._convert_event_to_dict(event)
            self._output_event_to_stdout(event_data)
            
        except Exception as e:
            handle_application_error("이벤트 처리 실패", e, raise_error=False, verbose=self.verbose)
        
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
                "timestamp": datetime.now(timezone.utc).isoformat(),
                "task_id": self.todo.get("id"),
                "proc_inst_id": self.todo.get("proc_inst_id"),
                "event": event_data
            }
            
            json_output = json.dumps(output_data, ensure_ascii=False, indent=2)
            print(f"[EVENT] {json_output}", flush=True)
            
        except Exception as e:
            handle_application_error("stdout 출력 실패", e, raise_error=False, verbose=self.verbose)
        
    def task_done(self) -> None:
        """태스크 완료 로그를 남긴다."""
        write_log_message(f"시뮬레이션 태스크 완료: {self.todo['id']}", self.verbose)
        self._output_event_to_stdout({"type": "task_completed", "data": {"message": "Task simulation completed"}})

    async def close(self) -> None:
        """큐 종료 훅."""
        self._output_event_to_stdout({"type": "queue_closed", "data": {"message": "Event queue closed"}})


class SmartSimulationExecutor(AgentExecutor):
    """스마트 시뮬레이션 실행기 - 프롬프트에 따라 다른 프로세스 실행"""
    
    def __init__(self, simulation_steps: int = 5, step_delay: float = 1.0, verbose: bool = False):
        self.simulation_steps = simulation_steps
        self.step_delay = step_delay
        self.is_cancelled = False
        self.verbose = verbose

    async def execute(self, context: RequestContext, event_queue: EventQueue) -> None:
        """스마트 시뮬레이션 실행"""
        write_log_message("스마트 시뮬레이션 실행기 시작", self.verbose)
        
        prompt = context.get_user_input()
        process_type = self._determine_process_type(prompt)
        
        # 시작 이벤트
        start_event = Event(
            type="task_started",
            data={
                "message": f"시뮬레이션 시작: {prompt}",
                "prompt": prompt,
                "process_type": process_type,
                "estimated_steps": self.simulation_steps
            }
        )
        event_queue.enqueue_event(start_event)

        # 프로세스 타입별 단계 실행
        steps = self._get_process_steps(process_type)
        
        for i, step_info in enumerate(steps, 1):
            if self.is_cancelled:
                break
                
            await asyncio.sleep(self.step_delay)
            
            # 진행 이벤트
            progress_event = Event(
                type="progress",
                data={
                    "step": i,
                    "total_steps": len(steps),
                    "step_name": step_info["name"],
                    "message": step_info["message"],
                    "progress_percentage": (i / len(steps)) * 100,
                    "process_type": process_type
                }
            )
            event_queue.enqueue_event(progress_event)

        if not self.is_cancelled:
            # 결과 생성
            result = self._generate_result(prompt, process_type)
            
            # 결과 출력 이벤트
            output_event = Event(
                type="output",
                data={
                    "content": result,
                    "final": True,
                    "process_type": process_type
                }
            )
            event_queue.enqueue_event(output_event)

            # 완료 이벤트
            done_event = Event(
                type="done",
                data={
                    "message": f"'{process_type}' 프로세스 완료",
                    "success": True,
                    "process_type": process_type,
                    "execution_time": len(steps) * self.step_delay
                }
            )
            event_queue.enqueue_event(done_event)

        write_log_message("스마트 시뮬레이션 실행기 종료", self.verbose)

    async def cancel(self, context: RequestContext, event_queue: EventQueue) -> None:
        """시뮬레이션 취소"""
        write_log_message("스마트 시뮬레이션 취소 요청", self.verbose)
        self.is_cancelled = True

    def _determine_process_type(self, prompt: str) -> str:
        """프롬프트를 분석하여 프로세스 타입 결정"""
        prompt_lower = prompt.lower()
        
        if any(keyword in prompt_lower for keyword in ["분석", "데이터", "차트", "그래프", "통계"]):
            return "데이터 분석"
        elif any(keyword in prompt_lower for keyword in ["보고서", "리포트", "문서", "작성"]):
            return "보고서 작성"
        elif any(keyword in prompt_lower for keyword in ["고객", "서비스", "문의", "지원"]):
            return "고객 서비스"
        elif any(keyword in prompt_lower for keyword in ["프로젝트", "관리", "계획", "일정"]):
            return "프로젝트 관리"
        else:
            return "일반 작업"

    def _get_process_steps(self, process_type: str) -> List[Dict[str, str]]:
        """프로세스 타입별 단계 정의"""
        steps_map = {
            "데이터 분석": [
                {"name": "데이터 수집", "message": "필요한 데이터를 수집하고 있습니다..."},
                {"name": "데이터 정제", "message": "데이터를 정제하고 전처리하고 있습니다..."},
                {"name": "분석 수행", "message": "통계 분석 및 패턴 인식을 수행하고 있습니다..."},
                {"name": "결과 생성", "message": "분석 결과를 생성하고 있습니다..."},
                {"name": "시각화", "message": "차트와 그래프를 생성하고 있습니다..."}
            ],
            "보고서 작성": [
                {"name": "요구사항 분석", "message": "보고서 요구사항을 분석하고 있습니다..."},
                {"name": "구조 설계", "message": "보고서 구조와 목차를 설계하고 있습니다..."},
                {"name": "내용 작성", "message": "주요 내용을 작성하고 있습니다..."},
                {"name": "검토 및 수정", "message": "작성된 내용을 검토하고 수정하고 있습니다..."}
            ],
            "고객 서비스": [
                {"name": "문의 분석", "message": "고객 문의 내용을 분석하고 있습니다..."},
                {"name": "솔루션 검색", "message": "기존 솔루션 데이터베이스에서 검색하고 있습니다..."},
                {"name": "응답 준비", "message": "고객 맞춤 응답을 준비하고 있습니다..."}
            ],
            "프로젝트 관리": [
                {"name": "프로젝트 분석", "message": "프로젝트 요구사항을 분석하고 있습니다..."},
                {"name": "일정 계획", "message": "프로젝트 일정을 계획하고 있습니다..."},
                {"name": "리소스 할당", "message": "필요한 리소스를 할당하고 있습니다..."},
                {"name": "위험 평가", "message": "프로젝트 위험을 평가하고 있습니다..."}
            ],
            "일반 작업": [
                {"name": "작업 분석", "message": "작업 요구사항을 분석하고 있습니다..."},
                {"name": "처리 수행", "message": "작업을 처리하고 있습니다..."},
                {"name": "결과 생성", "message": "결과를 생성하고 있습니다..."}
            ]
        }
        
        return steps_map.get(process_type, steps_map["일반 작업"])

    def _generate_result(self, prompt: str, process_type: str) -> Dict[str, Any]:
        """프로세스 타입별 결과 생성"""
        base_result = {
            "input_prompt": prompt,
            "process_type": process_type,
            "completion_status": "성공",
            "simulation_mode": True
        }
        
        if process_type == "데이터 분석":
            base_result.update({
                "findings": [
                    "주요 트렌드 3개 발견",
                    "데이터 품질 점수: 85%",
                    "이상값 2개 감지"
                ],
                "recommendations": [
                    "월별 모니터링 강화",
                    "데이터 정제 프로세스 개선"
                ],
                "visualizations": ["trend_chart.png", "distribution_plot.png"]
            })
        elif process_type == "보고서 작성":
            base_result.update({
                "sections": ["개요", "현황 분석", "주요 발견사항", "권장사항"],
                "word_count": 2500,
                "review_status": "초안 완료"
            })
        elif process_type == "고객 서비스":
            base_result.update({
                "response_prepared": True,
                "estimated_resolution_time": "2시간",
                "satisfaction_prediction": 4.5
            })
        elif process_type == "프로젝트 관리":
            base_result.update({
                "timeline": "6주 예상",
                "resource_requirements": ["개발자 2명", "디자이너 1명"],
                "risk_level": "중간"
            })
        
        return base_result


def parse_arguments():
    """CLI 인자 파싱"""
    parser = argparse.ArgumentParser(
        description="독립적인 ProcessGPT Agent Simulator",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
예시:
  %(prog)s "데이터를 분석해주세요"
  %(prog)s "보고서를 작성해주세요" --agent-orch "report_writer" --steps 4
  %(prog)s "고객 문의를 처리해주세요" --delay 0.5 --verbose
        """
    )
    
    parser.add_argument(
        "prompt",
        help="에이전트가 처리할 프롬프트 메시지"
    )
    
    parser.add_argument(
        "--agent-orch",
        default="simulator",
        help="에이전트 오케스트레이션 타입 (기본값: simulator)"
    )
    
    parser.add_argument(
        "--activity-name",
        default="simulation_task",
        help="활동 이름 (기본값: simulation_task)"
    )
    
    parser.add_argument(
        "--steps",
        type=int,
        help="시뮬레이션 단계 수 (프로세스별 자동 결정)"
    )
    
    parser.add_argument(
        "--delay",
        type=float,
        default=1.0,
        help="각 단계별 대기 시간(초) (기본값: 1.0)"
    )
    
    parser.add_argument(
        "--verbose",
        action="store_true",
        help="상세한 로그 출력"
    )
    
    return parser.parse_args()


async def main():
    """메인 함수"""
    args = parse_arguments()
    
    try:
        write_log_message("독립적인 ProcessGPT Agent Simulator 시작", args.verbose)
        write_log_message(f"프롬프트: {args.prompt}", args.verbose)
        
        # 시뮬레이션 실행기 생성
        executor = SmartSimulationExecutor(
            simulation_steps=args.steps or 5,
            step_delay=args.delay,
            verbose=args.verbose
        )
        
        # 시뮬레이터 생성
        simulator = StandaloneProcessGPTAgentSimulator(
            executor=executor,
            agent_orch=args.agent_orch,
            verbose=args.verbose
        )
        
        # 시뮬레이션 실행
        await simulator.run_simulation(
            prompt=args.prompt,
            activity_name=args.activity_name
        )
        
        write_log_message("독립적인 ProcessGPT Agent Simulator 완료", args.verbose)
        
    except KeyboardInterrupt:
        write_log_message("사용자에 의해 중단됨", args.verbose)
        sys.exit(1)
    except Exception as e:
        write_log_message(f"시뮬레이션 오류: {e}", True)
        sys.exit(1)


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print("\n시뮬레이션이 중단되었습니다.", file=sys.stderr)
        sys.exit(1)
