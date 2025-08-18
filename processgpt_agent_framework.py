import asyncio
import logging
import json
from datetime import datetime
from typing import Dict, Any, Optional, List
from dataclasses import dataclass, asdict
import uuid

# Supabase client
from supabase import create_client, Client
import os
from dotenv import load_dotenv

# A2A SDK imports (가정)
from a2a.server.agent_execution import AgentExecutor, RequestContext
from a2a.server.events import EventQueue, Event
from a2a.types import (
    TaskArtifactUpdateEvent,
    TaskState,
    TaskStatus,
    TaskStatusUpdateEvent,
)

load_dotenv()

# Configure logging
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

@dataclass
class TodoListItem:
    """TodoList 테이블의 데이터 구조"""
    id: str
    agent_type: str
    prompt: str
    input_data: Dict[str, Any]
    agent_status: str
    agent_output: Optional[Dict[str, Any]] = None
    priority: int = 0
    created_at: Optional[datetime] = None
    updated_at: Optional[datetime] = None
    started_at: Optional[datetime] = None
    completed_at: Optional[datetime] = None

@dataclass
class EventItem:
    """Events 테이블의 데이터 구조"""
    todolist_id: str
    event_type: str
    event_data: Dict[str, Any]
    context_id: Optional[str] = None
    task_id: Optional[str] = None
    message: Optional[str] = None
    id: Optional[str] = None
    created_at: Optional[datetime] = None

class ProcessGPTRequestContext(RequestContext):
    """Supabase 데이터를 기반으로 한 RequestContext 구현"""
    
    def __init__(self, todolist_item: TodoListItem):
        self.todolist_item = todolist_item
        self._user_input = todolist_item.prompt
        self._message = todolist_item.input_data.get('message', todolist_item.prompt)
        self._current_task = None
        self._task_state = todolist_item.agent_status
    
    def get_user_input(self) -> str:
        return self._user_input
    
    @property
    def message(self) -> str:
        return self._message
    
    @property
    def current_task(self):
        return self._current_task
    
    @current_task.setter
    def current_task(self, task):
        self._current_task = task
    
    @property
    def task_state(self) -> str:
        return self._task_state
    
    def get_context_data(self) -> Dict[str, Any]:
        """추가적인 컨텍스트 데이터 반환"""
        return {
            'todolist_id': self.todolist_item.id,
            'agent_type': self.todolist_item.agent_type,
            'input_data': self.todolist_item.input_data,
            'priority': self.todolist_item.priority
        }

class ProcessGPTEventQueue(EventQueue):
    """Supabase Events 테이블에 이벤트를 저장하는 EventQueue 구현"""

    def __init__(self, supabase_client: Client, todolist_id: str):
        self.supabase = supabase_client
        self.todolist_id = todolist_id
        super().__init__()

    def enqueue_event(self, event: Event):
        """Supabase events 테이블에 이벤트를 삽입"""
        try:
            event_data = self._convert_event_to_dict(event)
            
            event_item = EventItem(
                todolist_id=self.todolist_id,
                event_type=self._get_event_type(event),
                event_data=event_data,
                context_id=getattr(event, 'contextId', None),
                task_id=getattr(event, 'taskId', None),
                message=self._extract_message(event)
            )
            
            # Supabase에 이벤트 삽입
            result = self.supabase.table('events').insert(asdict(event_item)).execute()
            logger.info(f"Event inserted: {event_item.event_type} for todolist {self.todolist_id}")
            
        except Exception as e:
            logger.error(f"Failed to enqueue event: {e}")
            raise

    def _convert_event_to_dict(self, event: Event) -> Dict[str, Any]:
        """Event 객체를 딕셔너리로 변환"""
        try:
            if hasattr(event, '__dict__'):
                return {k: v for k, v in event.__dict__.items() if not k.startswith('_')}
            else:
                return {'event': str(event)}
        except Exception:
            return {'event': str(event)}

    def _get_event_type(self, event: Event) -> str:
        """Event 타입 결정"""
        if isinstance(event, TaskStatusUpdateEvent):
            if hasattr(event, 'status') and event.status:
                if event.status.state == TaskState.working:
                    return 'task_progress'
                elif event.status.state == TaskState.completed:
                    return 'task_completed'
                elif event.status.state == TaskState.input_required:
                    return 'task_progress'
                elif hasattr(event.status, 'state') and 'fail' in str(event.status.state).lower():
                    return 'task_failed'
            return 'task_progress'
        elif isinstance(event, TaskArtifactUpdateEvent):
            return 'task_progress'
        else:
            return 'task_progress'

    def _extract_message(self, event: Event) -> Optional[str]:
        """Event에서 메시지 추출"""
        if hasattr(event, 'status') and event.status and hasattr(event.status, 'message'):
            message = event.status.message
            if hasattr(message, 'content'):
                return message.content
            elif isinstance(message, str):
                return message
        return None

    def task_done(self) -> None:
        """태스크 완료 시 todolist 상태 업데이트"""
        try:
            self.supabase.table('todolist').update({
                'agent_status': 'completed',
                'completed_at': datetime.now().isoformat()
            }).eq('id', self.todolist_id).execute()
            
            # 완료 이벤트 추가
            completion_event = EventItem(
                todolist_id=self.todolist_id,
                event_type='task_completed',
                event_data={'status': 'completed'},
                message='Task completed successfully'
            )
            
            self.supabase.table('events').insert(asdict(completion_event)).execute()
            logger.info(f"Task marked as completed: {self.todolist_id}")
            
        except Exception as e:
            logger.error(f"Failed to mark task as done: {e}")
            raise

class ProcessGPTAgentServer:
    """Supabase 기반 폴링을 통한 Agent Server"""

    def __init__(self, agent_executor: AgentExecutor, agent_type: str):
        self.agent_executor = agent_executor
        self.agent_type = agent_type
        self.supabase = self._init_supabase()
        self.polling_interval = 5  # seconds
        self.is_running = False

    def _init_supabase(self) -> Client:
        """Supabase 클라이언트 초기화"""
        url = os.getenv("SUPABASE_URL")
        key = os.getenv("SUPABASE_ANON_KEY")
        
        if not url or not key:
            raise ValueError("SUPABASE_URL and SUPABASE_ANON_KEY must be set in environment variables")
        
        return create_client(url, key)

    async def run(self):
        """메인 실행 루프"""
        self.is_running = True
        logger.info(f"ProcessGPT Agent Server started for agent type: {self.agent_type}")
        
        while self.is_running:
            try:
                # 대기 중인 todolist 항목들을 가져옴
                pending_todos = await self.fetch_pending_todolist()
                
                if pending_todos:
                    logger.info(f"Found {len(pending_todos)} pending tasks")
                    
                    # 각 todolist 항목을 처리
                    for todo in pending_todos:
                        try:
                            await self.process_todolist_item(todo)
                        except Exception as e:
                            logger.error(f"Failed to process todolist item {todo.id}: {e}")
                            await self.mark_task_failed(todo.id, str(e))
                
                # 폴링 간격만큼 대기
                await asyncio.sleep(self.polling_interval)
                
            except Exception as e:
                logger.error(f"Error in main loop: {e}")
                await asyncio.sleep(self.polling_interval)

    async def fetch_pending_todolist(self) -> List[TodoListItem]:
        """대기 중인 todolist 항목들을 Supabase에서 가져옴"""
        try:
            response = self.supabase.table('todolist')\
                .select('*')\
                .eq('agent_type', self.agent_type)\
                .eq('agent_status', 'pending')\
                .order('priority', desc=True)\
                .order('created_at', desc=False)\
                .execute()
            
            todos = []
            for data in response.data:
                todo = TodoListItem(
                    id=data['id'],
                    agent_type=data['agent_type'],
                    prompt=data['prompt'],
                    input_data=data.get('input_data', {}),
                    agent_status=data['agent_status'],
                    agent_output=data.get('agent_output'),
                    priority=data.get('priority', 0),
                    created_at=data.get('created_at'),
                    updated_at=data.get('updated_at'),
                    started_at=data.get('started_at'),
                    completed_at=data.get('completed_at')
                )
                todos.append(todo)
            
            return todos
            
        except Exception as e:
            logger.error(f"Failed to fetch pending todolist: {e}")
            return []

    async def process_todolist_item(self, todo: TodoListItem):
        """개별 todolist 항목을 처리"""
        logger.info(f"Processing todolist item: {todo.id}")
        
        try:
            # 상태를 'in_progress'로 업데이트
            await self.mark_task_in_progress(todo.id)
            
            # RequestContext 생성
            context = ProcessGPTRequestContext(todo)
            
            # EventQueue 생성
            event_queue = ProcessGPTEventQueue(self.supabase, todo.id)
            
            # 시작 이벤트 생성
            start_event = EventItem(
                todolist_id=todo.id,
                event_type='task_started',
                event_data={'status': 'started', 'agent_type': self.agent_type},
                message=f'Task started for agent type: {self.agent_type}'
            )
            self.supabase.table('events').insert(asdict(start_event)).execute()
            
            # Agent Executor 실행
            await self.agent_executor.execute(context, event_queue)
            
            # 성공적으로 완료된 경우 task_done 호출
            event_queue.task_done()
            
        except Exception as e:
            logger.error(f"Error processing todolist item {todo.id}: {e}")
            await self.mark_task_failed(todo.id, str(e))
            raise

    async def mark_task_in_progress(self, todolist_id: str):
        """태스크를 진행 중 상태로 표시"""
        try:
            self.supabase.table('todolist').update({
                'agent_status': 'in_progress',
                'started_at': datetime.now().isoformat()
            }).eq('id', todolist_id).execute()
            
        except Exception as e:
            logger.error(f"Failed to mark task as in progress: {e}")
            raise

    async def mark_task_failed(self, todolist_id: str, error_message: str):
        """태스크를 실패 상태로 표시"""
        try:
            self.supabase.table('todolist').update({
                'agent_status': 'failed',
                'agent_output': {'error': error_message},
                'completed_at': datetime.now().isoformat()
            }).eq('id', todolist_id).execute()
            
            # 실패 이벤트 추가
            failure_event = EventItem(
                todolist_id=todolist_id,
                event_type='task_failed',
                event_data={'error': error_message},
                message=f'Task failed: {error_message}'
            )
            
            self.supabase.table('events').insert(asdict(failure_event)).execute()
            
        except Exception as e:
            logger.error(f"Failed to mark task as failed: {e}")

    def stop(self):
        """서버 중지"""
        self.is_running = False
        logger.info("ProcessGPT Agent Server stopped")

# 편의를 위한 헬퍼 함수들
async def create_todolist_item(
    supabase: Client,
    agent_type: str,
    prompt: str,
    input_data: Dict[str, Any] = None,
    priority: int = 0
) -> str:
    """새로운 todolist 항목 생성"""
    if input_data is None:
        input_data = {}
    
    todo_data = {
        'agent_type': agent_type,
        'prompt': prompt,
        'input_data': input_data,
        'agent_status': 'pending',
        'priority': priority
    }
    
    result = supabase.table('todolist').insert(todo_data).execute()
    return result.data[0]['id']

async def get_todolist_status(supabase: Client, todolist_id: str) -> Dict[str, Any]:
    """todolist 항목의 상태 조회"""
    response = supabase.table('todolist').select('*').eq('id', todolist_id).execute()
    if response.data:
        return response.data[0]
    return None

async def get_todolist_events(supabase: Client, todolist_id: str) -> List[Dict[str, Any]]:
    """todolist 항목의 모든 이벤트 조회"""
    response = supabase.table('events')\
        .select('*')\
        .eq('todolist_id', todolist_id)\
        .order('created_at', desc=False)\
        .execute()
    
    return response.data 