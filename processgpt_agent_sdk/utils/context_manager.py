from __future__ import annotations

# =============================================================================
# Context Manager
# 설명: 요청/프로세스 범위의 컨텍스트 값을 ContextVar로 관리
# =============================================================================

from contextvars import ContextVar
from typing import Optional


# 컨텍스트 변수 정의
todo_id_var: ContextVar[Optional[str]] = ContextVar("todo_id", default=None)
proc_id_var: ContextVar[Optional[str]] = ContextVar("proc_id", default=None)
crew_type_var: ContextVar[Optional[str]] = ContextVar("crew_type", default=None)
form_key_var: ContextVar[Optional[str]] = ContextVar("form_key", default=None)
form_id_var: ContextVar[Optional[str]] = ContextVar("form_id", default=None)
all_users_var: ContextVar[Optional[str]] = ContextVar("all_users", default=None)


def set_context(*, todo_id: Optional[str] = None, proc_inst_id: Optional[str] = None, crew_type: Optional[str] = None, form_key: Optional[str] = None, form_id: Optional[str] = None, all_users: Optional[str] = None) -> None:
    """전달된 값들만 ContextVar에 설정한다."""
    if todo_id is not None:
        todo_id_var.set(todo_id)
    if proc_inst_id is not None:
        proc_id_var.set(proc_inst_id)
    if crew_type is not None:
        crew_type_var.set(crew_type)
    if form_key is not None:
        form_key_var.set(form_key)
    if form_id is not None:
        form_id_var.set(form_id)
    if all_users is not None:
        all_users_var.set(all_users)


def reset_context() -> None:
    """모든 컨텍스트 값을 초기 상태(None)로 되돌린다."""
    todo_id_var.set(None)
    proc_id_var.set(None)
    crew_type_var.set(None)
    form_key_var.set(None)
    form_id_var.set(None)
    all_users_var.set(None)

