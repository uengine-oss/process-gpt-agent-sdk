import os
import sys
import asyncio
from dotenv import load_dotenv

sys.path.append(os.path.dirname(os.path.dirname(__file__)))

from processgpt_agent_sdk.processgpt_agent_framework import ProcessGPTAgentServer
from sample_server.minimal_executor import MinimalExecutor


async def main():
    load_dotenv()
    server = ProcessGPTAgentServer(
        agent_executor=MinimalExecutor(),          # executor 내부에서 chat/process 분기
        agent_type=os.getenv("PROCESS_AGENT_TYPE", "langchain-react"),
    )

    # 채팅 SSE 서버 설정
    try:
        from starlette.applications import Starlette  # type: ignore[reportMissingImports]
        import uvicorn  # type: ignore[reportMissingImports]
    except Exception as e:
        raise RuntimeError(
            "Chat(SSE) 모드 테스트에는 extras 설치가 필요합니다.\n"
            '- uv 사용 시: `uv run --extra sse python sample_server/minimal_server.py`\n'
            '- pip 사용 시: `pip install "process-gpt-agent-sdk[sse]"`'
        ) from e

    app = Starlette()
    server.mount_chat_sse(app, path=os.getenv("CHAT_SSE_PATH", "/chat/stream"))

    # 기본 포트는 로컬에서 충돌이 잦아 8010을 사용
    host = os.getenv("CHAT_HOST", "127.0.0.1")
    port = int(os.getenv("CHAT_PORT", "8010"))
    uvicorn_server = uvicorn.Server(uvicorn.Config(app, host=host, port=port, log_level="info"))

    # 프로세스 모드(폴링) + 채팅 모드(SSE) 동시 실행
    await asyncio.gather(server.run(), uvicorn_server.serve())


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        pass


