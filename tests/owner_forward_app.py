"""넘겨주기 통합 시험용 서버 — 한 프로세스가 파드 하나 노릇을 한다.

    python -m tests.owner_forward_app <port>

환경변수(CHAT_OWNER_FORWARDING · CHAT_OWNER_REDIS_URL · CHAT_OWNER_ADDRESS)는 시험이 넣는다.
Executor 는 자기 포트를 먼저 흘리고(어느 파드가 처리했는지 보이게), 중지되거나 시간이
다 될 때까지 방향 바꾸기 접수함을 들여다보며 받은 지시를 그대로 흘린다.
"""

import asyncio
import sys

import uvicorn
from a2a.helpers import new_text_artifact_update_event
from a2a.server.agent_execution import AgentExecutor, RequestContext
from a2a.server.events import EventQueue
from starlette.applications import Starlette

from processgpt_agent_sdk import chat_sse, processgpt_agent_framework
from processgpt_agent_sdk.context_api import get_streamer
from processgpt_agent_sdk.processgpt_agent_framework import ProcessGPTAgentServer
from processgpt_agent_sdk.steering import get_steering_inbox

PORT = int(sys.argv[1])
TENANT = "acme"


class _Executor(AgentExecutor):
    async def execute(self, context: RequestContext, event_queue: EventQueue) -> None:
        streamer = get_streamer(context)
        cid = str(context.context_id)
        await streamer.send_text(f"port={PORT} ")
        for _ in range(200):  # 최대 20초
            for d in await get_steering_inbox().take(cid):
                await streamer.send_text(f"applied:{d.message} ")
            await asyncio.sleep(0.1)
        await event_queue.enqueue_event(
            new_text_artifact_update_event(
                task_id=str(context.task_id), context_id=cid, text=f"port={PORT} 끝", last_chunk=True,
            )
        )

    async def cancel(self, context: RequestContext, event_queue: EventQueue) -> None:
        return None

    def steer(self, directive):
        return None


async def _owns(conversation_id: str) -> str:
    return TENANT


async def _no_persist(*_a, **_k) -> None:
    return None


chat_sse.fetch_chat_room_tenant_id = _owns
processgpt_agent_framework.persist_chat_to_db = _no_persist
processgpt_agent_framework.initialize_db = lambda: None

app = Starlette()
ProcessGPTAgentServer(agent_executor=_Executor(), agent_type="owner-test", tenant_auth=False).mount_chat_routes(
    app, health_path="/health"
)

if __name__ == "__main__":
    uvicorn.run(app, host="127.0.0.1", port=PORT, log_level="warning")
