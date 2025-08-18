#!/usr/bin/env python3
"""
ProcessGPT Agent Framework 사용 예제

이 스크립트는 ProcessGPT 프레임워크의 기본적인 사용법을 보여줍니다.
"""

import asyncio
import os
from supabase import create_client
from dotenv import load_dotenv
from processgpt_utils import ProcessGPTClient, ProcessGPTMonitor, quick_submit_task, batch_submit_tasks

# 환경변수 로드
load_dotenv()

async def example_basic_usage():
    """기본 사용법 예제"""
    print("=== ProcessGPT Framework 기본 사용법 예제 ===\n")
    
    # Supabase 클라이언트 초기화
    supabase = create_client(
        os.getenv("SUPABASE_URL"),
        os.getenv("SUPABASE_ANON_KEY")
    )
    
    # ProcessGPT 클라이언트 생성
    client = ProcessGPTClient(supabase)
    
    # 1. 단일 태스크 제출
    print("1. 단일 태스크 제출")
    todolist_id = await client.submit_task(
        agent_type="crew-ai-dr",
        prompt="2024년 AI 기술 트렌드에 대한 심층 연구",
        input_data={
            "domain": "artificial_intelligence",
            "year": 2024,
            "depth": "comprehensive"
        },
        priority=5
    )
    print(f"   태스크 ID: {todolist_id}")
    
    # 2. 태스크 상태 확인
    print("\n2. 태스크 상태 확인")
    status = await client.get_task_status(todolist_id)
    print(f"   현재 상태: {status['agent_status']}")
    print(f"   생성 시간: {status['created_at']}")
    
    # 3. 태스크 이벤트 조회
    print("\n3. 태스크 이벤트 조회")
    events = await client.get_task_events(todolist_id)
    print(f"   총 이벤트 수: {len(events)}")
    for event in events[:3]:  # 최근 3개만 표시
        print(f"   - {event['event_type']}: {event.get('message', 'N/A')}")
    
    return todolist_id

async def example_batch_submission():
    """배치 태스크 제출 예제"""
    print("\n=== 배치 태스크 제출 예제 ===\n")
    
    supabase = create_client(
        os.getenv("SUPABASE_URL"),
        os.getenv("SUPABASE_ANON_KEY")
    )
    
    # 여러 태스크를 한번에 제출
    tasks = [
        {
            "agent_type": "crew-ai-dr",
            "prompt": "블록체인 기술의 최신 동향 분석",
            "input_data": {"domain": "blockchain"},
            "priority": 3
        },
        {
            "agent_type": "crew-ai-dr", 
            "prompt": "메타버스 시장 전망 연구",
            "input_data": {"domain": "metaverse"},
            "priority": 4
        },
        {
            "agent_type": "crew-ai-dr",
            "prompt": "양자컴퓨팅 발전 현황 조사",
            "input_data": {"domain": "quantum_computing"},
            "priority": 2
        }
    ]
    
    todolist_ids = await batch_submit_tasks(supabase, tasks)
    print(f"배치로 제출된 태스크 수: {len(todolist_ids)}")
    for i, task_id in enumerate(todolist_ids):
        print(f"   태스크 {i+1}: {task_id}")
    
    return todolist_ids

async def example_monitoring():
    """모니터링 예제"""
    print("\n=== 시스템 모니터링 예제 ===\n")
    
    supabase = create_client(
        os.getenv("SUPABASE_URL"),
        os.getenv("SUPABASE_ANON_KEY")
    )
    
    monitor = ProcessGPTMonitor(supabase)
    
    # 1. 시스템 통계 조회
    print("1. 시스템 통계")
    stats = await monitor.get_system_stats()
    print(f"   총 태스크: {stats.get('total_tasks', 0)}")
    print(f"   대기 중: {stats.get('pending_tasks', 0)}")
    print(f"   진행 중: {stats.get('in_progress_tasks', 0)}")
    print(f"   완료됨: {stats.get('completed_tasks', 0)}")
    print(f"   실패함: {stats.get('failed_tasks', 0)}")
    
    # 2. 최근 이벤트 조회
    print("\n2. 최근 이벤트 (최대 5개)")
    recent_events = await monitor.get_recent_events(limit=5)
    for event in recent_events:
        if 'error' not in event:
            print(f"   - {event['event_type']}: {event.get('message', 'N/A')[:50]}...")

async def example_wait_for_completion():
    """완료 대기 예제"""
    print("\n=== 완료 대기 예제 ===\n")
    
    supabase = create_client(
        os.getenv("SUPABASE_URL"),
        os.getenv("SUPABASE_ANON_KEY")
    )
    
    # 빠른 태스크 제출 및 완료 대기
    result = await quick_submit_task(
        supabase,
        agent_type="crew-ai-dr",
        prompt="간단한 테스트 태스크",
        wait_for_completion=True,
        timeout=60  # 1분 타임아웃
    )
    
    print(f"태스크 ID: {result['todolist_id']}")
    print(f"제출 상태: {result['status']}")
    
    if result.get('completed'):
        final_status = result['final_status']
        print(f"최종 상태: {final_status['agent_status']}")
        if final_status.get('agent_output'):
            print("결과 출력:")
            print(f"   {final_status['agent_output']}")

async def example_task_management():
    """태스크 관리 예제"""
    print("\n=== 태스크 관리 예제 ===\n")
    
    supabase = create_client(
        os.getenv("SUPABASE_URL"),
        os.getenv("SUPABASE_ANON_KEY")
    )
    
    client = ProcessGPTClient(supabase)
    
    # 1. 특정 에이전트 타입의 태스크 목록 조회
    print("1. 에이전트별 태스크 목록")
    tasks = await client.list_tasks(agent_type="crew-ai-dr", limit=5)
    print(f"   crew-ai-dr 태스크 수: {len(tasks)}")
    
    # 2. 상태별 태스크 목록 조회
    print("\n2. 상태별 태스크 목록")
    pending_tasks = await client.list_tasks(status="pending", limit=3)
    print(f"   대기 중인 태스크: {len(pending_tasks)}")
    
    completed_tasks = await client.list_tasks(status="completed", limit=3)
    print(f"   완료된 태스크: {len(completed_tasks)}")
    
    # 3. 태스크 취소 예제 (실제로는 실행하지 않음)
    print("\n3. 태스크 취소 (예제만)")
    if pending_tasks:
        task_id = pending_tasks[0]['id']
        print(f"   취소할 수 있는 태스크: {task_id}")
        # success = await client.cancel_task(task_id)
        # print(f"   취소 결과: {success}")

async def main():
    """메인 실행 함수"""
    print("ProcessGPT Agent Framework 사용 예제를 시작합니다...\n")
    
    try:
        # 기본 사용법
        main_task_id = await example_basic_usage()
        
        # 배치 제출
        batch_task_ids = await example_batch_submission()
        
        # 모니터링
        await example_monitoring()
        
        # 태스크 관리
        await example_task_management()
        
        # 완료 대기 (주석 처리 - 실제 에이전트가 없으면 오래 걸림)
        # await example_wait_for_completion()
        
        print("\n=== 모든 예제가 완료되었습니다! ===")
        print(f"메인 태스크 ID: {main_task_id}")
        print(f"배치 태스크 수: {len(batch_task_ids)}")
        
    except Exception as e:
        print(f"오류 발생: {e}")
        print("환경변수 설정과 Supabase 연결을 확인해주세요.")

if __name__ == "__main__":
    # 환경변수 확인
    required_env_vars = ["SUPABASE_URL", "SUPABASE_ANON_KEY"]
    missing_vars = [var for var in required_env_vars if not os.getenv(var)]
    
    if missing_vars:
        print(f"다음 환경변수가 설정되지 않았습니다: {', '.join(missing_vars)}")
        print("env.example 파일을 참조하여 .env 파일을 생성하세요.")
        exit(1)
    
    # 비동기 메인 함수 실행
    asyncio.run(main()) 