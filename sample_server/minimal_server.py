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
        agent_executor=MinimalExecutor(),
        agent_type="langchain-react",
    )

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
    server.mount_chat_sse(app, path="/chat/stream")
    uvicorn_server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=8010, log_level="info"))
    await asyncio.gather(server.run(), uvicorn_server.serve())


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        pass


