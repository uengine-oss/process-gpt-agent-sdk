"""재개 샘플 에이전트 워커 — 워크아이템을 폴링해 ResumableExecutor 로 실행한다.

설치된 SDK 를 쓴다(이 저장소 소스를 sys.path 에 넣지 않는다):

    pip install "process-gpt-agent-sdk>=0.11.0"
    SUPABASE_URL=... SUPABASE_KEY=... python sample_server/resume_server.py

``RESUME_SAMPLE_AGENT_ORCH`` (기본 ``resume-sample``) 인 작업만 집는다.
"""

import asyncio
import logging
import os
from importlib.metadata import version

import processgpt_agent_sdk
from dotenv import load_dotenv
from processgpt_agent_sdk import ProcessGPTAgentServer

from resume_executor import ResumableExecutor

AGENT_ORCH = os.getenv("RESUME_SAMPLE_AGENT_ORCH", "resume-sample")


async def main() -> None:
    load_dotenv()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    # 어느 SDK 로 돌았는지 남긴다 — 소스 경로가 아니라 설치본이어야 검증이 된다.
    print(
        f"resume-sample ready sdk={version('process-gpt-agent-sdk')} "
        f"path={os.path.dirname(processgpt_agent_sdk.__file__)} agent_orch={AGENT_ORCH}",
        flush=True,
    )
    server = ProcessGPTAgentServer(agent_executor=ResumableExecutor(), agent_type=AGENT_ORCH)
    await server.run()


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        pass
