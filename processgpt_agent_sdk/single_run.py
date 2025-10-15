import logging
from a2a.server.agent_execution import AgentExecutor
from .database import initialize_db, fetch_todo_by_id
from .processgpt_agent_framework import ProcessGPTAgentServer


logger = logging.getLogger(__name__)



async def run_single_todo_readonly(agent_executor: AgentExecutor, agent_type: str, todo_id: str) -> None:
    """테스트/검증용 단건 실행: 상태 변경 없이 id로만 조회하여 실행.

    - 실서비스와 동일한 컨텍스트 준비 및 실행 파이프라인을 사용합니다.
    - DB의 `todolist` 행은 어떤 필드도 업데이트하지 않습니다.
    """
    initialize_db()
    row = await fetch_todo_by_id(todo_id)
    if not row:
        logger.warning("run_single_todo_readonly: 대상 없음 todo_id=%s", todo_id)
        return

    server = ProcessGPTAgentServer(agent_executor, agent_type)
    try:
        server._current_todo_id = str(row.get("id"))
        await server.process_todolist_item(row)
    finally:
        server._current_todo_id = None


