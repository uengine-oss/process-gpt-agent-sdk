import asyncio
import json
import os
from typing_extensions import override
from a2a.server.agent_execution import AgentExecutor, RequestContext
from a2a.server.events import EventQueue
from a2a.helpers import new_task, new_text_artifact_update_event, new_text_status_update_event
from a2a.helpers import new_text_message
from a2a.types import Role, TaskState

import litellm


class MinimalExecutor(AgentExecutor):
    """A2A 규격 2종 이벤트만 전송하는 최소 예시 익스큐터."""

    def _openai_api_base_from_proxy_url(self, proxy_url: str) -> str:
        base = (proxy_url or "").rstrip("/")
        return base if base.endswith("/v1") else f"{base}/v1"

    @override
    async def execute(self, context: RequestContext, event_queue: EventQueue) -> None:
        # 채팅 모드: Message-only
        # - 프레임워크/전송(SSE) 세부에는 의존하지 않고, A2A 표준 metadata로만 분기합니다.
        if (context.metadata or {}).get("request_kind") == "chat":
            # LLM 프록시 스트리밍 예시 (LiteLLM / OpenAI 호환)
            # - env: LLM_MODEL, LLM_PROXY_URL, LLM_PROXY_API_KEY
            # - 토큰 스트림은 A2A TaskStatusUpdateEvent.metadata(JSON)로 emit → SSE event: message로 변환됨
            model = os.environ.get("LLM_MODEL")
            proxy_url = os.environ.get("LLM_PROXY_URL")
            api_key = os.environ.get("LLM_PROXY_API_KEY")
            if not model or not proxy_url or not api_key:
                raise RuntimeError("LLM 스트리밍 예제 실행을 위해 LLM_MODEL/LLM_PROXY_URL/LLM_PROXY_API_KEY가 필요합니다.")

            ctx_id = context.context_id or "ctx-unknown"
            task_id = context.task_id or "task-unknown"

            user_text = context.get_user_input()
            messages = [
                {"role": "system", "content": "You are a helpful assistant. Reply in Korean."},
                {"role": "user", "content": user_text},
            ]

            stream = await litellm.acompletion(
                model=model,
                messages=messages,
                temperature=0,
                stream=True,
                api_base=self._openai_api_base_from_proxy_url(proxy_url),
                api_key=api_key,
            )

            full = ""
            async for chunk in stream:
                try:
                    # OpenAI-stream-like: choices[0].delta.content
                    delta = chunk.choices[0].delta  # type: ignore[attr-defined]
                    token = getattr(delta, "content", None)
                    if not token:
                        continue
                except Exception:
                    continue

                full += token
                tok_evt = new_text_status_update_event(
                    task_id=str(task_id),
                    context_id=str(ctx_id),
                    state=TaskState.TASK_STATE_WORKING,
                    text="",
                )
                tok_evt.metadata.update({"type": "token", "content": token})
                await event_queue.enqueue_event(tok_evt)

            msg = new_text_message(text=full, role=Role.ROLE_AGENT)
            # (선택) 외부 서비스가 저장을 원하면, message.metadata에 payload를 실어 전송할 수 있습니다.
            # 여기서는 A2A 표준 context_id / tenant(있으면) / metadata만 사용해 예시로 구성합니다.
            md = context.metadata or {}
            msg.metadata.update(
                {
                    "chat_payload": {
                        "role": "assistant",
                        "content": full,
                        "context_id": context.context_id,
                        "task_id": context.task_id,
                        "tenant": getattr(context, "tenant", None),
                        "request_metadata": md,
                    }
                }
            )
            await event_queue.enqueue_event(msg)
            return

        query = context.get_user_input()
        print(f"query: {query}")

        # 프로세스(폴링) 모드: Task lifecycle 패턴 (Task → status/artifact)
        # - A2A 표준 task_id/context_id를 사용합니다.
        context_id = context.context_id or "ctx-unknown"
        task_id = context.task_id or "task-unknown"
        
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


