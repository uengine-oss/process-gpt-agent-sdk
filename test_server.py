import asyncio
from dotenv import load_dotenv
from a2a.server.agent_execution import AgentExecutor, RequestContext
from a2a.server.events import EventQueue
from processgpt_agent_sdk import ProcessGPTAgentServer

class SimpleExecutor(AgentExecutor):
    async def execute(self, context: RequestContext, event_queue: EventQueue):
        print(f"✅ 작업 실행됨! ID: {context.get_context_data()['row']['id']}")
    
    async def cancel(self, context: RequestContext, event_queue: EventQueue):
        # 취소 동작 없음
        return

async def main():
    load_dotenv()
    server = ProcessGPTAgentServer(
        agent_executor=SimpleExecutor(),
        agent_type="browser-automation-agent"
    )
    await server.run()

if __name__ == "__main__":
    asyncio.run(main())
