from typing_extensions import override

from a2a.helpers import new_text_message
from a2a.server.agent_execution import AgentExecutor, RequestContext
from a2a.server.events import EventQueue
from a2a.types import Role


class MinimalChatExecutor(AgentExecutor):
    """Message-only 규칙을 따르는 최소 채팅 Executor."""

    @override
    async def execute(self, context: RequestContext, event_queue: EventQueue) -> None:
        text = context.get_user_input()
        await event_queue.enqueue_event(new_text_message(f"[chat] {text}", role=Role.ROLE_AGENT))

    @override
    async def cancel(self, context: RequestContext, event_queue: EventQueue) -> None:
        return

