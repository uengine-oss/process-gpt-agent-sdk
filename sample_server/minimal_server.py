import os
import sys
import asyncio
from dotenv import load_dotenv

sys.path.append(os.path.dirname(os.path.dirname(__file__)))

from processgpt_agent_sdk.processgpt_agent_framework import ProcessGPTAgentServer
from sample_server.minimal_executor import MinimalExecutor


async def main():
    load_dotenv()
    server = ProcessGPTAgentServer(agent_executor=MinimalExecutor(), agent_type="crewai-action")
    server.polling_interval = 3
    await server.run()


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        pass


