import asyncio
import json
from typing_extensions import override
from a2a.server.agent_execution import AgentExecutor, RequestContext
from a2a.server.events import EventQueue
from a2a.types import TaskStatusUpdateEvent, TaskState, TaskArtifactUpdateEvent
from a2a.utils import new_agent_text_message, new_text_artifact


class MinimalExecutor(AgentExecutor):
    """A2A 규격 2종 이벤트만 전송하는 최소 예시 익스큐터."""

    @override
    async def execute(self, context: RequestContext, event_queue: EventQueue) -> None:
        query = context.get_user_input()
        print(f"query: {query}")

        row = context.get_context_data()["row"]
        context_id = row.get("root_proc_inst_id") or row.get("proc_inst_id")
        task_id = row.get("id")

        # 1) 진행 상태 이벤트 (events 저장, data=문자열)
        # 결과 본문(JSON)을 그대로 보내되, Message 스키마 요구로 문자열로 직렬화
        payload = {
            "order_process_activity_order_request_form": {
                "orderer_name": "안치윤",
                "product_name": "금형세트",
                "order_quantity": "50",
            }
        }
        # A2A Message 유틸을 사용해 표준 스키마 메시지 생성
        event_queue.enqueue_event(
            TaskStatusUpdateEvent(
                status={
                    "state": TaskState.working,
                    "message": new_agent_text_message(
                        json.dumps(payload, ensure_ascii=False),
                        context_id,
                        task_id,
                    ),
                },
                final=False,
                contextId=context_id,
                taskId=task_id,
                metadata={
                    "crew_type": "action",
                    "event_type": "task_started",
                    "job_id": "job-demo-0001",
                },
            )
        )

        await asyncio.sleep(0.1)

        # 1-2) 휴먼 인더 루프: 사용자 입력 요청 이벤트 (events 저장, event_type=human_asked)
        question_text = json.dumps(payload, ensure_ascii=False)
        event_queue.enqueue_event(
            TaskStatusUpdateEvent(
                status={
                    "state": TaskState.input_required,
                    "message": new_agent_text_message(
                        question_text,
                        context_id,
                        task_id,
                    ),
                },
                final=True,
                contextId=context_id,
                taskId=task_id,
                metadata={
                    "crew_type": "action",
                    "job_id": "job-demo-0001"
                },
            )
        )

        await asyncio.sleep(0.1)

        # 2) 최종 아티팩트 이벤트 (todolist 저장, p_final=True)
        # 유틸을 사용해 표준 아티팩트 생성
        artifact = new_text_artifact(
            name="current_result",
            description="Result of request to agent.",
            text=json.dumps(payload, ensure_ascii=False),
        )
        event_queue.enqueue_event(
            TaskArtifactUpdateEvent(
                artifact=artifact,
                lastChunk=True,
                contextId=context_id,
                taskId=task_id,
            )
        )

    @override
    async def cancel(self, context: RequestContext, event_queue: EventQueue) -> None:
        # 최소 구현: 특별한 취소 동작 없음
        return


