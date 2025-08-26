"""ProcessGPT DB utilities

역할:
- Supabase 연결/초기화
- 안전한 RPC/CRUD 호출(재시도/폴백 포함)
- 이벤트 기록, 작업 클레임/상태/저장/조회, 사용자·에이전트·폼·테넌트 조회

반환 규칙(폴백 포함):
- Optional 단건 조회 계열 → 실패 시 None
- 목록/시퀀스 계열 → 실패 시 빈 리스트 []
- 변경/기록 계열 → 실패 시 경고 로그만 남기고 None
"""

import os
import json
import asyncio
import socket
import uuid
from datetime import datetime
from typing import Any, Dict, List, Optional, Tuple

from dotenv import load_dotenv
from supabase import Client, create_client
import logging
import random
from typing import Callable, TypeVar

T = TypeVar("T")

from ..utils.logger import handle_application_error, write_log_message


async def _async_retry(
    fn: Callable[[], T],
    *,
    name: str,
    retries: int = 3,
    base_delay: float = 0.8,
    fallback: Optional[Callable[[], T]] = None,
) -> Optional[T]:
    """재시도 유틸(지수 백오프+jitter).

    - 최종 실패 시: fallback이 있으면 실행, 없으면 None
    - 로그: 시도/지연/최종 실패/폴백 사용/폴백 실패
    """
    last_err: Optional[Exception] = None
    for attempt in range(1, retries + 1):
        try:
            # 블로킹 DB 호출은 스레드로 위임해 이벤트 루프 차단 방지
            return await asyncio.to_thread(fn)
        except Exception as e:
            last_err = e
            jitter = random.uniform(0, 0.3)
            delay = base_delay * (2 ** (attempt - 1)) + jitter
            write_log_message(f"{name} 재시도 {attempt}/{retries} (delay={delay:.2f}s): {e}", level=logging.WARNING)
            await asyncio.sleep(delay)
    write_log_message(f"{name} 최종 실패: {last_err}", level=logging.ERROR)
    if fallback is not None:
        try:
            fb_val = fallback()
            write_log_message(f"{name} 폴백 사용", level=logging.WARNING)
            return fb_val
        except Exception as e:
            write_log_message(f"{name} 폴백 실패: {e}", level=logging.ERROR)
    return None
 


# ------------------------------
# Consumer 식별자 도우미
# ------------------------------
_supabase_client: Optional[Client] = None


def initialize_db() -> None:
    """환경변수 로드 및 Supabase 클라이언트 초기화"""
    global _supabase_client
    if _supabase_client is not None:
        return
    if os.getenv("ENV") != "production":
        load_dotenv()
    supabase_url = os.getenv("SUPABASE_URL") or os.getenv("SUPABASE_KEY_URL")
    supabase_key = os.getenv("SUPABASE_KEY") or os.getenv("SUPABASE_ANON_KEY")
    if not supabase_url or not supabase_key:
        raise RuntimeError("SUPABASE_URL 및 SUPABASE_KEY가 필요합니다")
    _supabase_client = create_client(supabase_url, supabase_key)


def get_db_client() -> Client:
    """초기화된 Supabase 클라이언트 반환."""
    if _supabase_client is None:
        raise RuntimeError("DB 미초기화: initialize_db() 먼저 호출")
    return _supabase_client


def get_consumer_id() -> str:
    """파드/프로세스 식별자 생성(CONSUMER_ID>HOST:PID)."""
    env_consumer = os.getenv("CONSUMER_ID")
    if env_consumer:
        return env_consumer
    host = socket.gethostname()
    pid = os.getpid()
    return f"{host}:{pid}"


# ------------------------------
# 폴링: 대기 작업 1건 클레임(RPC 내부에서 상태 변경 포함)
# ------------------------------
async def polling_pending_todos(agent_orch: str, consumer: str) -> Optional[Dict[str, Any]]:
    """대기중 작업 하나를 RPC로 클레임하고 반환. 실패/없음 시 None."""
    def _call():
        client = get_db_client()
        return client.rpc(
            "fetch_pending_task",
            {"p_agent_orch": agent_orch, "p_consumer": consumer, "p_limit": 1},
        ).execute()

    resp = await _async_retry(_call, name="polling_pending_todos", fallback=lambda: None)
    if not resp or not resp.data:
        return None
    return resp.data[0]


# ------------------------------
# 단건 todo 조회
# ------------------------------
async def fetch_todo_by_id(todo_id: str) -> Optional[Dict[str, Any]]:
    """todolist에서 특정 id의 row 단건 조회. 실패 시 None."""
    if not todo_id:
        return None
    def _call():
        client = get_db_client()
        return (
            client.table("todolist").select("*").eq("id", todo_id).single().execute()
        )

    resp = await _async_retry(_call, name="fetch_todo_by_id")
    if not resp or not resp.data:
        return None
    return resp.data


# ------------------------------
# 이벤트 기록
# ------------------------------
async def record_event(todo: Dict[str, Any], data: Dict[str, Any], event_type: Optional[str] = None) -> None:
    """UI용 events 테이블에 이벤트 기록. 실패해도 플로우 지속."""
    def _call():
        client = get_db_client()
        payload: Dict[str, Any] = {
            "id": str(uuid.uuid4()),
            "job_id": todo.get("proc_inst_id") or str(todo.get("id")),
            "todo_id": str(todo.get("id")),
            "proc_inst_id": todo.get("proc_inst_id"),
            "crew_type": todo.get("agent_orch"),
            "data": data,
        }
        if event_type is not None:
            payload["event_type"] = event_type
        return client.table("events").insert(payload).execute()

    resp = await _async_retry(_call, name="record_event", fallback=lambda: None)
    if resp is None:
        write_log_message("record_event 최종 실패(무시)", level=logging.WARNING)


# ------------------------------
# 완료된 데이터 조회
# ------------------------------
async def fetch_done_data(proc_inst_id: Optional[str]) -> List[Any]:
    """같은 proc_inst_id의 완료 output 목록 조회. 실패 시 []."""
    if not proc_inst_id:
        return []
    def _call():
        client = get_db_client()
        return client.rpc("fetch_done_data", {"p_proc_inst_id": proc_inst_id}).execute()

    resp = await _async_retry(_call, name="fetch_done_data", fallback=lambda: None)
    if not resp:
        return []
    return [row.get("output") for row in (resp.data or [])]


# ------------------------------
# 결과 저장 (중간/최종)
# ------------------------------
async def save_task_result(todo_id: str, result: Any, final: bool = False) -> None:
    """결과 저장 RPC(중간/최종). 실패해도 경고 로그 후 지속."""
    def _call():
        client = get_db_client()
        payload = result if isinstance(result, (dict, list)) else json.loads(json.dumps(result))
        return client.rpc(
            "save_task_result",
            {"p_todo_id": todo_id, "p_payload": payload, "p_final": final},
        ).execute()

    await _async_retry(_call, name="save_task_result", fallback=lambda: None)


# ------------------------------
# 추가 유틸: 이벤트/작업/사용자/에이전트/폼/테넌트 조회
# ------------------------------
async def fetch_human_response(job_id: str) -> Optional[Dict[str, Any]]:
    """events에서 특정 job_id의 human_response 1건 조회. 실패 시 None."""
    def _call():
        client = get_db_client()
        return (
            client.table("events")
            .select("*")
            .eq("job_id", job_id)
            .eq("event_type", "human_response")
            .execute()
        )

    resp = await _async_retry(_call, name="fetch_human_response")
    if not resp or not resp.data:
        return None
    return resp.data[0]


async def fetch_task_status(todo_id: str) -> Optional[str]:
    """todolist.draft_status 조회. 실패 시 None."""
    def _call():
        client = get_db_client()
        return (
            client.table("todolist").select("draft_status").eq("id", todo_id).single().execute()
        )

    resp = await _async_retry(_call, name="fetch_task_status")
    if not resp or not resp.data:
        return None
    return resp.data.get("draft_status")


async def fetch_all_agents() -> List[Dict[str, Any]]:
    """모든 에이전트 목록 정규화 반환. 실패 시 []."""
    def _call():
        client = get_db_client()
        return (
            client.table("users")
            .select("id, username, role, goal, persona, tools, profile, model, tenant_id, is_agent")
            .eq("is_agent", True)
            .execute()
        )

    resp = await _async_retry(_call, name="fetch_all_agents")
    rows = resp.data or [] if resp else []
    normalized: List[Dict[str, Any]] = []
    for row in rows:
        normalized.append(
            {
                "id": row.get("id"),
                "name": row.get("username"),
                "role": row.get("role"),
                "goal": row.get("goal"),
                "persona": row.get("persona"),
                "tools": row.get("tools") or "mem0",
                "profile": row.get("profile"),
                "model": row.get("model"),
                "tenant_id": row.get("tenant_id"),
            }
        )
    return normalized


async def fetch_agent_data(user_ids: str) -> List[Dict[str, Any]]:
    """user_id(들)로 에이전트를 조회. 없거나 유효하지 않으면 모든 에이전트를 반환.

    - 입력은 UUID 또는 콤마(,)로 구분된 UUID 목록을 허용
    - 유효한 UUID가 하나도 없으면 전체 에이전트 반환
    - 유효한 UUID로 조회했는데 결과가 비면 전체 에이전트 반환
    """
    def _is_valid_uuid(value: str) -> bool:
        try:
            uuid.UUID(str(value))
            return True
        except Exception:
            return False

    # 1) 입력 정규화 및 UUID 필터링
    raw_ids = [x.strip() for x in (user_ids or "").split(",") if x.strip()]
    valid_ids = [x for x in raw_ids if _is_valid_uuid(x)]

    # 2) 유효한 UUID가 없으면 전체 에이전트 반환
    if not valid_ids:
        return await fetch_all_agents()

    # 3) 유효한 UUID로 에이전트 조회
    def _call():
        client = get_db_client()
        resp = (
            client
            .table("users")
            .select("id, username, role, goal, persona, tools, profile, model, tenant_id, is_agent")
            .in_("id", valid_ids)
            .eq("is_agent", True)
            .execute()
        )
        rows = resp.data or []
        normalized: List[Dict[str, Any]] = []
        for row in rows:
            normalized.append(
                {
                    "id": row.get("id"),
                    "name": row.get("username"),
                    "role": row.get("role"),
                    "goal": row.get("goal"),
                    "persona": row.get("persona"),
                    "tools": row.get("tools") or "mem0",
                    "profile": row.get("profile"),
                    "model": row.get("model"),
                    "tenant_id": row.get("tenant_id"),
                }
            )
        return normalized

    result = await _async_retry(_call, name="fetch_agent_data", fallback=lambda: [])

    # 4) 결과가 없으면 전체 에이전트로 폴백
    if not result:
        return await fetch_all_agents()

    return result


async def fetch_form_types(tool_val: str, tenant_id: str) -> Tuple[str, List[Dict[str, Any]]]:
    """폼 타입 정의 조회 및 정규화. 실패 시 기본값 반환."""
    def _call():
        client = get_db_client()
        form_id = tool_val[12:] if tool_val.startswith("formHandler:") else tool_val
        resp = (
            client.table("form_def").select("fields_json").eq("id", form_id).eq("tenant_id", tenant_id).execute()
        )
        fields_json = resp.data[0].get("fields_json") if resp.data else None
        form_html = resp.data[0].get('html') if resp.data else None
        if not fields_json:
            return form_id, [{"key": form_id, "type": "default", "text": ""}], form_html
        return form_id, fields_json, form_html

    resp = await _async_retry(_call, name="fetch_form_types", fallback=lambda: (tool_val, [{"key": tool_val, "type": "default", "text": ""}]))
    return resp if resp else (tool_val, [{"key": tool_val, "type": "default", "text": ""}])


async def fetch_tenant_mcp_config(tenant_id: str) -> Optional[Dict[str, Any]]:
    """테넌트 MCP 설정 조회. 실패 시 None."""
    def _call():
        client = get_db_client()
        return client.table("tenants").select("mcp").eq("id", tenant_id).single().execute()
    try:
        resp = await _async_retry(_call, name="fetch_tenant_mcp_config", fallback=lambda: None)
        return resp.data.get("mcp") if resp and resp.data else None
    except Exception as e:
        handle_application_error("fetch_tenant_mcp_config 실패", e, raise_error=False)
        return None


# ------------------------------
# 오류 상태 업데이트 (FAILED)
# ------------------------------
async def update_task_error(todo_id: str) -> None:
    """작업 오류 상태 업데이트 (FAILED) - 로그 컬럼은 건드리지 않음"""
    if not todo_id:
        return
    def _call():
        client = get_db_client()
        return (
            client
            .table('todolist')
            .update({'draft_status': 'FAILED', 'consumer': None})
            .eq('id', todo_id)
            .execute()
        )

    await _async_retry(_call, name="update_task_error", fallback=lambda: None)


# ============================================================================
# 알림 저장
# ============================================================================

def save_notification(
    *,
    title: str,
    notif_type: str,
    description: Optional[str] = None,
    user_ids_csv: Optional[str] = None,
    tenant_id: Optional[str] = None,
    url: Optional[str] = None,
    from_user_id: Optional[str] = None,
) -> None:
    """notifications 테이블에 알림 저장

    - user_ids_csv: 쉼표로 구분된 사용자 ID 목록. 비어있으면 저장 생략
    - 테이블 스키마는 다음 컬럼을 가정: user_id, tenant_id, title, description, type, url, from_user_id
    """
    try:
        # 대상 사용자가 없으면 작업 생략
        if not user_ids_csv:
            write_log_message(f"알림 저장 생략: 대상 사용자 없음 (user_ids_csv={user_ids_csv})")
            return

        supabase = get_db_client()

        user_ids: List[str] = [uid.strip() for uid in user_ids_csv.split(',') if uid and uid.strip()]
        if not user_ids:
            write_log_message(f"알림 저장 생략: 유효한 사용자 ID 없음 (user_ids_csv={user_ids_csv})")
            return
        
        rows: List[Dict[str, Any]] = []
        for uid in user_ids:
            rows.append(
                {
                    "id": str(uuid.uuid4()),  # UUID 자동 생성
                    "user_id": uid,
                    "tenant_id": tenant_id,
                    "title": title,
                    "description": description,
                    "type": notif_type,
                    "url": url,
                    "from_user_id": from_user_id,
                }
            )

        supabase.table("notifications").insert(rows).execute()
        write_log_message(f"알림 저장 완료: {len(rows)}건")
    except Exception as e:
        # 알림 저장 실패는 치명적이지 않으므로 오류만 로깅
        handle_application_error("알림저장오류", e, raise_error=False)


def _is_valid_uuid(value: str) -> bool:
    """UUID 문자열 형식 검증 (v1~v8 포함)"""
    try:
        uuid.UUID(value)
        return True
    except Exception:
        return False

       
# ============================================================================
# 사용자 및 에이전트 정보 조회
# ============================================================================

async def fetch_human_users_by_proc_inst_id(proc_inst_id: str) -> str:
    """proc_inst_id로 해당 프로세스의 실제 사용자(is_agent=false)들의 이메일만 쉼표로 구분하여 반환"""
    if not proc_inst_id:
        return ""
    
    def _sync():
        try:
            supabase = get_db_client()
            
            # 1. proc_inst_id로 todolist에서 user_id들 조회
            resp = (
                supabase
                .table('todolist')
                .select('user_id')
                .eq('proc_inst_id', proc_inst_id)
                .execute()
            )
            
            if not resp.data:
                return ""
            
            # 2. 모든 user_id를 수집 (중복 제거)
            all_user_ids = set()
            for row in resp.data:
                user_id = row.get('user_id', '')
                if user_id:
                    # 쉼표로 구분된 경우 분리
                    ids = [id.strip() for id in user_id.split(',') if id.strip()]
                    all_user_ids.update(ids)
            
            if not all_user_ids:
                return ""
            
            # 3. 각 user_id가 실제 사용자(is_agent=false 또는 null)인지 확인 후 이메일 수집
            human_user_emails = []
            for user_id in all_user_ids:
                # UUID 형식이 아니면 스킵
                if not _is_valid_uuid(user_id):
                    continue
                
                # users 테이블에서 해당 user_id 조회
                user_resp = (
                    supabase
                    .table('users')
                    .select('id, email, is_agent')
                    .eq('id', user_id)
                    .execute()
                )
                
                if user_resp.data:
                    user = user_resp.data[0]
                    is_agent = user.get('is_agent')
                    # is_agent가 false이거나 null인 경우만 실제 사용자로 간주
                    if not is_agent:  # False 또는 None
                        email = (user.get('email') or '').strip()
                        if email:
                            human_user_emails.append(email)
            
            # 4. 쉼표로 구분된 문자열로 반환
            return ','.join(human_user_emails)
            
        except Exception as e:
            handle_application_error("사용자조회오류", e, raise_error=False)
            return ""
    
    return await asyncio.to_thread(_sync)