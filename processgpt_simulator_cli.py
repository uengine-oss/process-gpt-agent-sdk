#!/usr/bin/env python3
"""
ProcessGPT Agent Simulator CLI Tool

데이터베이스 연결 없이 ProcessGPT 에이전트를 시뮬레이션하는 CLI 도구입니다.
프롬프트를 CLI 인자로 받고, 진행 상태 이벤트를 stdout으로 출력합니다.

Usage:
    python processgpt_simulator_cli.py "Your prompt here"
    python processgpt_simulator_cli.py "Analyze the data" --agent-orch "data_analysis" --activity-name "data_task"
"""

import asyncio
import argparse
import sys
import os
from typing import Optional

# 프로젝트 루트를 import 경로에 추가
sys.path.insert(0, os.path.abspath(os.path.dirname(__file__)))

from processgpt_agent_sdk.simulator import ProcessGPTAgentSimulator
from a2a.server.agent_execution import AgentExecutor, RequestContext
from a2a.server.events import EventQueue, Event
from processgpt_agent_sdk.utils.logger import write_log_message


class SimulationExecutor(AgentExecutor):
    """시뮬레이션용 실행기: 실제 AI 모델 호출 대신 모킹된 동작을 수행"""
    
    def __init__(self, simulation_steps: int = 5, step_delay: float = 1.0):
        self.simulation_steps = simulation_steps
        self.step_delay = step_delay
        self.is_cancelled = False

    async def execute(self, context: RequestContext, event_queue: EventQueue) -> None:
        """시뮬레이션된 실행을 수행한다."""
        write_log_message("시뮬레이션 실행기 시작")
        
        prompt = context.get_user_input()
        context_data = context.get_context_data()
        
        # 시작 이벤트
        start_event = Event(
            type="task_started",
            data={
                "message": f"시뮬레이션 시작: {prompt}",
                "prompt": prompt,
                "agent_orch": context_data.get("agent_orch", ""),
                "task_id": context_data.get("task_id", "")
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
            # 중간 결과 출력
            output_event = Event(
                type="output",
                data={
                    "content": {
                        "result": f"'{prompt}'에 대한 시뮬레이션 분석 결과",
                        "analysis": {
                            "input_length": len(prompt),
                            "word_count": len(prompt.split()),
                            "simulated_processing_time": self.simulation_steps * self.step_delay,
                            "status": "completed"
                        },
                        "recommendations": [
                            "시뮬레이션이 성공적으로 완료되었습니다.",
                            "실제 환경에서는 더 복잡한 처리가 수행됩니다.",
                            "데이터베이스 연결이 필요한 기능들은 모킹되었습니다."
                        ]
                    },
                    "final": True
                }
            )
            event_queue.enqueue_event(output_event)

            # 완료 이벤트
            done_event = Event(
                type="done",
                data={
                    "message": "시뮬레이션 완료",
                    "success": True,
                    "execution_time": self.simulation_steps * self.step_delay
                }
            )
            event_queue.enqueue_event(done_event)
        else:
            # 취소 이벤트
            cancel_event = Event(
                type="cancelled",
                data={
                    "message": "시뮬레이션이 취소되었습니다",
                    "cancelled_at_step": step
                }
            )
            event_queue.enqueue_event(cancel_event)

        write_log_message("시뮬레이션 실행기 종료")

    async def cancel(self, context: RequestContext, event_queue: EventQueue) -> None:
        """시뮬레이션 취소를 수행한다."""
        write_log_message("시뮬레이션 취소 요청")
        self.is_cancelled = True


def parse_arguments():
    """CLI 인자를 파싱한다."""
    parser = argparse.ArgumentParser(
        description="ProcessGPT Agent Simulator - 데이터베이스 없이 에이전트 시뮬레이션",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
예시:
  %(prog)s "데이터를 분석해주세요"
  %(prog)s "보고서를 작성해주세요" --agent-orch "report_writer" --steps 3
  %(prog)s "고객 문의를 처리해주세요" --activity-name "customer_service" --delay 0.5
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
        "--user-id",
        help="사용자 ID (기본값: 자동 생성)"
    )
    
    parser.add_argument(
        "--tenant-id",
        help="테넌트 ID (기본값: 자동 생성)"
    )
    
    parser.add_argument(
        "--tool",
        default="default",
        help="사용할 도구 (기본값: default)"
    )
    
    parser.add_argument(
        "--feedback",
        default="",
        help="피드백 메시지"
    )
    
    parser.add_argument(
        "--steps",
        type=int,
        default=5,
        help="시뮬레이션 단계 수 (기본값: 5)"
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
    
    # 로깅 설정
    if not args.verbose:
        # 시뮬레이션 이벤트만 stdout에 출력되도록 로그 레벨 조정
        import logging
        logging.getLogger("process-gpt-agent-framework").setLevel(logging.WARNING)
    
    try:
        write_log_message("ProcessGPT Agent Simulator CLI 시작")
        write_log_message(f"프롬프트: {args.prompt}")
        
        # 시뮬레이션 실행기 생성
        executor = SimulationExecutor(
            simulation_steps=args.steps,
            step_delay=args.delay
        )
        
        # 시뮬레이터 생성
        simulator = ProcessGPTAgentSimulator(
            executor=executor,
            agent_orch=args.agent_orch
        )
        
        # 시뮬레이션 실행
        await simulator.run_simulation(
            prompt=args.prompt,
            activity_name=args.activity_name,
            user_id=args.user_id,
            tenant_id=args.tenant_id,
            tool=args.tool,
            feedback=args.feedback
        )
        
        write_log_message("ProcessGPT Agent Simulator CLI 완료")
        
    except KeyboardInterrupt:
        write_log_message("사용자에 의해 중단됨")
        sys.exit(1)
    except Exception as e:
        write_log_message(f"시뮬레이션 오류: {e}")
        sys.exit(1)


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print("\n시뮬레이션이 중단되었습니다.", file=sys.stderr)
        sys.exit(1)
