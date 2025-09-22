#!/usr/bin/env python3
"""
사용자 정의 실행기 예제

이 예제는 실제 AI 모델이나 비즈니스 로직을 시뮬레이션하는 
더 현실적인 실행기를 보여줍니다.
"""

import asyncio
import os
import sys
import json
from typing import Dict, List, Any

# 프로젝트 루트를 import 경로에 추가
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from processgpt_agent_sdk.simulator import ProcessGPTAgentSimulator
from a2a.server.agent_execution import AgentExecutor, RequestContext
from a2a.server.events import EventQueue, Event
from processgpt_agent_sdk.utils.logger import write_log_message
from a2a.utils import new_agent_text_message, new_task, new_text_artifact

class CustomBusinessExecutor(AgentExecutor):
    """비즈니스 로직을 시뮬레이션하는 사용자 정의 실행기"""
    
    def __init__(self):
        self.is_cancelled = False

    async def execute(self, context: RequestContext, event_queue: EventQueue) -> None:
        """사용자 정의 비즈니스 로직을 실행한다."""
        write_log_message("사용자 정의 실행기 시작")
        
        prompt = context.get_user_input()
        task = context.current_task

        context_data = context.get_context_data()

        if not context.message:
            raise Exception('No message provided')

        if not task:
            task = new_task(context.message)
            await event_queue.enqueue_event(task)
            

        await event_queue.enqueue_event(
            TaskArtifactUpdateEvent(
                append=False,
                context_id=task.context_id,
                task_id=task.id,
                last_chunk=True,
                artifact=new_text_artifact(
                    name='current_result',
                    description='Result of request to agent.',
                    text="결과문장",
                ),
            )
        )
        await event_queue.enqueue_event(
            TaskStatusUpdateEvent(
                status=TaskStatus(state=TaskState.completed),
                final=True,
                context_id=task.context_id,
                task_id=task.id,
            )
        )
    

        # # 시작 이벤트
        # start_event = Event(
        #     type="task_started",
        #     data={
        #         "message": f"비즈니스 프로세스 시작: {prompt}",
        #         "prompt": prompt,
        #         "executor_type": "CustomBusinessExecutor"
        #     }
        # )
        # event_queue.enqueue_event(start_event)

        # # 프롬프트 기반으로 적절한 비즈니스 프로세스 선택
        # process_type = self._determine_process_type(prompt)
        # write_log_message(f"선택된 프로세스: {process_type}")
        
        # # 프로세스 타입 이벤트
        # process_event = Event(
        #     type="process_selected",
        #     data={
        #         "process_type": process_type,
        #         "message": f"'{process_type}' 프로세스가 선택되었습니다."
        #     }
        # )
        # event_queue.enqueue_event(process_event)

        # # 해당 비즈니스 프로세스 실행
        # try:
        #     process_func = self.business_processes.get(process_type, self.business_processes["기본"])
        #     await process_func(prompt, context_data, event_queue)
        # except Exception as e:
        #     error_event = Event(
        #         type="error",
        #         data={
        #             "error": str(e),
        #             "message": f"프로세스 실행 중 오류 발생: {e}"
        #         }
        #     )
        #     event_queue.enqueue_event(error_event)
        #     return

        # if not self.is_cancelled:
        #     # 완료 이벤트
        #     done_event = Event(
        #         type="done",
        #         data={
        #             "message": f"'{process_type}' 프로세스 완료",
        #             "success": True,
        #             "process_type": process_type
        #         }
        #     )
        #     event_queue.enqueue_event(done_event)

        # write_log_message("사용자 정의 실행기 종료")

    async def cancel(self, context: RequestContext, event_queue: EventQueue) -> None:
        """실행 취소를 수행한다."""
        write_log_message("사용자 정의 실행기 취소 요청")
        self.is_cancelled = True

    

async def main():
    """메인 함수 - 사용자 정의 실행기로 시뮬레이션 실행"""
    print("=== ProcessGPT 사용자 정의 실행기 예제 ===")
    print()
    
    # 실행기 생성
    executor = CustomBusinessExecutor()
    
    # 시뮬레이터 생성
    simulator = ProcessGPTAgentSimulator(
        executor=executor,
        agent_orch="custom_business"
    )
    
    # 여러 예제 실행
    examples = [
        "고객 데이터를 분석해서 트렌드를 파악해주세요",
        "분기별 성과 보고서를 작성해주세요",
        "고객 문의에 대한 응답을 준비해주세요",
        "신제품 개발 프로젝트를 계획해주세요"
    ]
    
    for i, prompt in enumerate(examples, 1):
        print(f"\n--- 예제 {i}: {prompt} ---")
        await simulator.run_simulation(prompt)
        print("\n" + "="*60)


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print("\n예제 실행이 중단되었습니다.")
        sys.exit(1)
