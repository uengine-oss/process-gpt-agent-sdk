import asyncio
import argparse
import os
import sys

# 프로젝트 루트를 import 경로에 추가
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from processgpt_agent_sdk import ProcessGPTAgentServer
from a2a.server.agent_execution import AgentExecutor, RequestContext
from a2a.server.events import EventQueue
from processgpt_agent_sdk.utils.logger import log as _emit_log


class InlineSuccessExecutor(AgentExecutor):
    """테스트용 더미 실행기: 실제 비즈니스 함수 호출 없이 로그만 출력."""
    async def execute(self, context: RequestContext, event_queue: EventQueue) -> None:
        _emit_log("[TEST] execute called.")

    async def cancel(self, context: RequestContext, event_queue: EventQueue) -> None:
        _emit_log("[TEST] cancel called.")

async def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--server-seconds", type=int, default=15, help="run() 실행 유지 시간(초)")
    ap.add_argument("--polling-interval", type=int, default=5, help="서버 폴링 주기(초)")
    ap.add_argument("--agent-orch", default="", help="폴링 시 사용할 agent_orch 필터")
    args = ap.parse_args()

    executor = InlineSuccessExecutor()
    server = ProcessGPTAgentServer(
        executor,
        polling_interval=args.polling_interval,
        agent_orch=args.agent_orch or "",
    )

    _emit_log("[TEST] start await server.run()")
    try:
        await asyncio.wait_for(server.run(), timeout=args.server_seconds)
    except asyncio.TimeoutError:
        _emit_log("[TEST] timeout reached; stopping server")
        server.stop()
    _emit_log("[TEST] server stopped")


if __name__ == "__main__":
    asyncio.run(main())


