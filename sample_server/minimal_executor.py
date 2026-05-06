import asyncio
import json
from typing_extensions import override
from a2a.server.agent_execution import AgentExecutor, RequestContext
from a2a.server.events import EventQueue
from a2a.helpers import new_task, new_text_artifact_update_event, new_text_status_update_event
from a2a.helpers import new_text_message
from a2a.types import Role, TaskState

from processgpt_agent_sdk.chat_mode import ChatRequestContext


class MinimalExecutor(AgentExecutor):
    """A2A 규격 2종 이벤트만 전송하는 최소 예시 익스큐터."""

    @override
    async def execute(self, context: RequestContext, event_queue: EventQueue) -> None:
        # 채팅 모드: Message-only (정확히 1개의 Message만 enqueue)
        if isinstance(context, ChatRequestContext):
            # 중간 스트리밍(청크)은 extras.streamer로 SSE에 흘림 (enqueue_event 아님)
            streamer = (context.get_context_data().get("extras") or {}).get("streamer")
            if streamer is not None:
                await streamer.send_text("thinking...\n")
                await asyncio.sleep(0.05)
                await streamer.send_text("almost done...\n")
                await asyncio.sleep(0.05)

            text = f"[chat] {context.get_user_input()}"
            msg = new_text_message(text=text, role=Role.ROLE_AGENT)
            # 방법 A: chats.messages에 저장할 payload를 execute()에서 직접 구성
            msg.metadata.update(
                {
                    "chat_payload": {
                        "role": "assistant",
                        "content": text,
                        "conversation_id": context.req.conversation_id,
                        "tenant_id": context.req.tenant_id,
                        "user_uid": context.req.user_uid,
                    }
                }
            )
            await event_queue.enqueue_event(msg)
            return

        query = context.get_user_input()
        print(f"query: {query}")

        row = context.get_context_data()["row"]
        context_id = row.get("root_proc_inst_id") or row.get("proc_inst_id")
        task_id = row.get("id")
        
        # # 🧪 테스트용 강제 오류 발생
        # print("🧪 테스트용 강제 오류 발생!")
        # raise RuntimeError("MinimalExecutor에서 발생한 테스트용 오류")

        # v1.0 스트리밍 규칙(태스크 라이프사이클): Task를 반드시 첫 이벤트로 enqueue
        task = new_task(
            task_id=str(task_id),
            context_id=str(context_id),
            state=TaskState.TASK_STATE_SUBMITTED,
        )
        await event_queue.enqueue_event(task)

        # 1) 진행 상태 이벤트 (events 저장, data=문자열)
        payload = {
            "order_process_activity_order_request_form": {
                "orderer_name": "안치윤",
                "product_name": "금형세트",
                "order_quantity": "50",
            }
        }
        status_evt = new_text_status_update_event(
            task_id=str(task_id),
            context_id=str(context_id),
            state=TaskState.TASK_STATE_WORKING,
            text=json.dumps(payload, ensure_ascii=False),
        )
        status_evt.metadata.update(
            {
                "crew_type": "action",
                "event_type": "task_started",
                "job_id": "job-demo-0001",
            }
        )
        await event_queue.enqueue_event(status_evt)

        await asyncio.sleep(0.1)

        # 1-2) 휴먼 인더 루프: 사용자 입력 요청 이벤트 (events 저장, event_type=human_asked)
        question_text = json.dumps(payload, ensure_ascii=False)
        hil_evt = new_text_status_update_event(
            task_id=str(task_id),
            context_id=str(context_id),
            state=TaskState.TASK_STATE_INPUT_REQUIRED,
            text=question_text,
        )
        hil_evt.metadata.update(
            {
                "crew_type": "action",
                "job_id": "job-demo-0001",
            }
        )
        await event_queue.enqueue_event(hil_evt)

        await asyncio.sleep(0.1)

        # 2) 최종 아티팩트 이벤트 (todolist 저장, p_final=True)
        artifact_evt = new_text_artifact_update_event(
            task_id=str(task_id),
            context_id=str(context_id),
            name="current_result",
            text=json.dumps(payload, ensure_ascii=False),
            last_chunk=True,
        )
        artifact_evt.artifact.description = "Result of request to agent."
        await event_queue.enqueue_event(artifact_evt)

    @override
    async def cancel(self, context: RequestContext, event_queue: EventQueue) -> None:
        # 최소 구현: 특별한 취소 동작 없음
        return


