
import os
import sys
import asyncio
import click
from dotenv import load_dotenv

# ProcessGPT 프레임워크 import
sys.path.append(os.path.dirname(os.path.dirname(__file__)))
from processgpt_agent_framework import ProcessGPTAgentServer
from crew_ai_dr_agent_executor import CrewAIDeepResearchAgentExecutor

load_dotenv()

@click.command()
@click.option('--agent-type', default='crew-ai-dr', help='Agent type identifier')
@click.option('--polling-interval', default=5, help='Polling interval in seconds')
def cli_main(agent_type: str, polling_interval: int):
    """ProcessGPT Agent Server for CrewAI Deep Research Agent"""
    
    # Agent Executor 초기화
    agent_executor = CrewAIDeepResearchAgentExecutor()
    
    # ProcessGPT Agent Server 초기화
    # Supabase에서 todolist를 폴링하고 주어진 agent_executor의 execute 메서드를 
    # 커스터마이즈된 EventQueue와 함께 호출
    server = ProcessGPTAgentServer(
        agent_executor=agent_executor,
        agent_type=agent_type
    )
    
    # 폴링 간격 설정
    server.polling_interval = polling_interval
    
    print(f"Starting ProcessGPT Agent Server...")
    print(f"Agent Type: {agent_type}")
    print(f"Polling Interval: {polling_interval} seconds")
    print("Press Ctrl+C to stop")
    
    try:
        # 비동기 서버 실행
        asyncio.run(server.run())
    except KeyboardInterrupt:
        print("\nShutting down server...")
        server.stop()
    except Exception as e:
        print(f"Server error: {e}")
        sys.exit(1)

if __name__ == "__main__":
    cli_main()
