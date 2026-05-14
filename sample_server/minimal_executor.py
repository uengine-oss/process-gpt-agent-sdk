import os
import uuid
from typing_extensions import override

from a2a.server.agent_execution import AgentExecutor, RequestContext
from a2a.server.events import EventQueue
from a2a.helpers import (
    new_text_message,
    new_text_status_update_event,
    new_text_artifact_update_event,
)
from a2a.types import TaskState, Role

import litellm


class MinimalExecutor(AgentExecutor):
    """A2A 표준만 사용하는 Executor.

    Executor 구현자는 SDK 내부 컨벤션(매직 메타데이터 키 등)을 일절 알 필요가 없습니다.
    A2A 표준 이벤트와 표준 필드(state, append, last_chunk)만 emit하면, SDK의
    EventQueue 구현체가 프로세스 모드(events/todolist) 또는 채팅 모드(SSE + chats)로 라우팅합니다.

    이벤트 흐름:
      1) Task — 라이프사이클 시작
      2) TaskArtifactUpdateEvent(append=..., last_chunk=False) — 토큰 스트리밍 청크
      3) TaskArtifactUpdateEvent(last_chunk=True)              — 최종 응답
    """

    def _openai_api_base_from_proxy_url(self, proxy_url: str) -> str:
        base = (proxy_url or "").rstrip("/")
        return base if base.endswith("/v1") else f"{base}/v1"

    @override
    async def execute(self, context: RequestContext, event_queue: EventQueue) -> None:
        model = os.environ.get("LLM_MODEL")
        proxy_url = os.environ.get("LLM_PROXY_URL")
        api_key = os.environ.get("LLM_PROXY_API_KEY")
        if not model or not proxy_url or not api_key:
            raise RuntimeError(
                "LLM 스트리밍 예제 실행을 위해 LLM_MODEL/LLM_PROXY_URL/LLM_PROXY_API_KEY가 필요합니다."
            )

        context_id = str(context.context_id or "ctx-unknown")
        task_id = str(context.task_id or "task-unknown")
        user_text = context.get_user_input()
        
        context_data = context.get_context_data() or {}
        row = context_data.get("row", {})
        extras = context_data.get("extras", {})

        task_submitted_evt = new_text_status_update_event(
            task_id=task_id,
            context_id=context_id,
            state=TaskState.TASK_STATE_SUBMITTED,
            text="Task submitted",
        )
        await event_queue.enqueue_event(task_submitted_evt)

        stream = await litellm.acompletion(
            model=model,
            messages=[
                {"role": "system", "content": "You are a helpful assistant. Reply in Korean."},
                {"role": "user", "content": user_text},
            ],
            temperature=0,
            stream=True,
            api_base=self._openai_api_base_from_proxy_url(proxy_url),
            api_key=api_key,
        )

        # 스트림 전체가 동일한 아티팩트의 청크임을 명시 (A2A 표준)
        artifact_id = str(uuid.uuid4())

        full = ""
        async for chunk in stream:
            try:
                token = getattr(chunk.choices[0].delta, "content", None)  # type: ignore[attr-defined]
            except Exception:
                continue
            if not token:
                continue

            full += token
            # 채팅 모드에서는 채팅 이벤트를 발행합니다.
            chat_evt = new_text_message(
                text=token,
                role=Role.ROLE_AGENT,
            )
            await event_queue.enqueue_event(chat_evt)

            # 프로세스 모드에서는 상태 이벤트를 발행합니다.
            tok_evt = new_text_status_update_event(
                task_id=task_id,
                context_id=context_id,
                state=TaskState.TASK_STATE_WORKING,
                text=token,
            )

            # if token.startswith("```tool_usage_started"):
            #     tok_evt.metadata.update({
            #         "event_type": "tool_usage_started",
            #     })
            # elif token.startswith("```tool_usage_finished"):
            #     tok_evt.metadata.update({
            #         "event_type": "tool_usage_finished",
            #     })

            await event_queue.enqueue_event(tok_evt)

        final_evt = new_text_status_update_event(
            task_id=task_id,
            context_id=context_id,
            state=TaskState.TASK_STATE_COMPLETED,
            text=full,
        )
        await event_queue.enqueue_event(final_evt)

        # 최종 응답: last_chunk=True. 누적 텍스트 전체를 한 번에 실어 보냄.
        final_artifact_evt = new_text_artifact_update_event(
            task_id=task_id,
            context_id=context_id,
            name="assistant_response",
            text=full,
            append=False,
            last_chunk=True,
            artifact_id=artifact_id,
        )
        await event_queue.enqueue_event(final_artifact_evt)

    @override
    async def cancel(self, context: RequestContext, event_queue: EventQueue) -> None:
        return
