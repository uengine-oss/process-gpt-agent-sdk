"""ProcessGPT Framework Utilities"""

import asyncio
import json
from typing import Dict, Any, List, Optional
from datetime import datetime
from supabase import Client
from processgpt_agent_framework import create_todolist_item, get_todolist_status, get_todolist_events

class ProcessGPTClient:
    """ProcessGPT 프레임워크와 상호작용하기 위한 클라이언트"""
    
    def __init__(self, supabase_client: Client):
        self.supabase = supabase_client
    
    async def submit_task(
        self,
        agent_type: str,
        prompt: str,
        input_data: Dict[str, Any] = None,
        priority: int = 0
    ) -> str:
        """새로운 태스크를 제출"""
        todolist_id = await create_todolist_item(
            self.supabase,
            agent_type=agent_type,
            prompt=prompt,
            input_data=input_data or {},
            priority=priority
        )
        return todolist_id
    
    async def get_task_status(self, todolist_id: str) -> Optional[Dict[str, Any]]:
        """태스크 상태 조회"""
        return await get_todolist_status(self.supabase, todolist_id)
    
    async def get_task_events(self, todolist_id: str) -> List[Dict[str, Any]]:
        """태스크 이벤트 로그 조회"""
        return await get_todolist_events(self.supabase, todolist_id)
    
    async def wait_for_completion(
        self,
        todolist_id: str,
        timeout: int = 300,
        check_interval: int = 2
    ) -> Dict[str, Any]:
        """태스크 완료까지 대기"""
        start_time = datetime.now()
        
        while True:
            status = await self.get_task_status(todolist_id)
            if not status:
                raise ValueError(f"Task {todolist_id} not found")
            
            if status['agent_status'] in ['completed', 'failed', 'cancelled']:
                return status
            
            # 타임아웃 체크
            elapsed = (datetime.now() - start_time).total_seconds()
            if elapsed > timeout:
                raise TimeoutError(f"Task {todolist_id} did not complete within {timeout} seconds")
            
            await asyncio.sleep(check_interval)
    
    async def cancel_task(self, todolist_id: str) -> bool:
        """태스크 취소"""
        try:
            self.supabase.table('todolist').update({
                'agent_status': 'cancelled',
                'completed_at': datetime.now().isoformat()
            }).eq('id', todolist_id).execute()
            return True
        except Exception:
            return False
    
    async def list_tasks(
        self,
        agent_type: Optional[str] = None,
        status: Optional[str] = None,
        limit: int = 100
    ) -> List[Dict[str, Any]]:
        """태스크 목록 조회"""
        query = self.supabase.table('todolist').select('*')
        
        if agent_type:
            query = query.eq('agent_type', agent_type)
        
        if status:
            query = query.eq('agent_status', status)
        
        query = query.order('created_at', desc=True).limit(limit)
        
        response = query.execute()
        return response.data

class ProcessGPTMonitor:
    """ProcessGPT 시스템 모니터링"""
    
    def __init__(self, supabase_client: Client):
        self.supabase = supabase_client
    
    async def get_system_stats(self) -> Dict[str, Any]:
        """시스템 통계 조회"""
        try:
            # 전체 태스크 수
            total_tasks = self.supabase.table('todolist').select('id', count='exact').execute()
            
            # 상태별 태스크 수
            pending_tasks = self.supabase.table('todolist')\
                .select('id', count='exact')\
                .eq('agent_status', 'pending')\
                .execute()
            
            in_progress_tasks = self.supabase.table('todolist')\
                .select('id', count='exact')\
                .eq('agent_status', 'in_progress')\
                .execute()
            
            completed_tasks = self.supabase.table('todolist')\
                .select('id', count='exact')\
                .eq('agent_status', 'completed')\
                .execute()
            
            failed_tasks = self.supabase.table('todolist')\
                .select('id', count='exact')\
                .eq('agent_status', 'failed')\
                .execute()
            
            # 에이전트 타입별 통계
            agent_types = self.supabase.table('todolist')\
                .select('agent_type', count='exact')\
                .execute()
            
            return {
                'total_tasks': total_tasks.count if total_tasks.count else 0,
                'pending_tasks': pending_tasks.count if pending_tasks.count else 0,
                'in_progress_tasks': in_progress_tasks.count if in_progress_tasks.count else 0,
                'completed_tasks': completed_tasks.count if completed_tasks.count else 0,
                'failed_tasks': failed_tasks.count if failed_tasks.count else 0,
                'agent_types': [row['agent_type'] for row in agent_types.data] if agent_types.data else [],
                'timestamp': datetime.now().isoformat()
            }
            
        except Exception as e:
            return {'error': str(e), 'timestamp': datetime.now().isoformat()}
    
    async def get_recent_events(self, limit: int = 50) -> List[Dict[str, Any]]:
        """최근 이벤트 조회"""
        try:
            response = self.supabase.table('events')\
                .select('*, todolist:todolist_id(agent_type, prompt)')\
                .order('created_at', desc=True)\
                .limit(limit)\
                .execute()
            
            return response.data
            
        except Exception as e:
            return [{'error': str(e), 'timestamp': datetime.now().isoformat()}]

# 편의 함수들
async def quick_submit_task(
    supabase_client: Client,
    agent_type: str,
    prompt: str,
    wait_for_completion: bool = False,
    timeout: int = 300
) -> Dict[str, Any]:
    """빠른 태스크 제출 및 선택적 완료 대기"""
    client = ProcessGPTClient(supabase_client)
    
    # 태스크 제출
    todolist_id = await client.submit_task(agent_type, prompt)
    
    result = {
        'todolist_id': todolist_id,
        'status': 'submitted'
    }
    
    if wait_for_completion:
        # 완료까지 대기
        final_status = await client.wait_for_completion(todolist_id, timeout)
        result.update({
            'final_status': final_status,
            'completed': True
        })
    
    return result

async def batch_submit_tasks(
    supabase_client: Client,
    tasks: List[Dict[str, Any]]
) -> List[str]:
    """여러 태스크를 한번에 제출"""
    client = ProcessGPTClient(supabase_client)
    todolist_ids = []
    
    for task in tasks:
        todolist_id = await client.submit_task(
            agent_type=task['agent_type'],
            prompt=task['prompt'],
            input_data=task.get('input_data', {}),
            priority=task.get('priority', 0)
        )
        todolist_ids.append(todolist_id)
    
    return todolist_ids 