import os
import json
import asyncio
import socket
import uuid
from typing import Any, Dict, List, Optional, Tuple, Callable, TypeVar

from dotenv import load_dotenv
from supabase import Client, create_client
import logging
import random

T = TypeVar("T")


# 모듈 전역 로거 (정상 경로는 로깅하지 않고, 오류 시에만 사용)
logger = logging.getLogger(__name__)


# ============================================================================
# Utility: 재시도 헬퍼 및 유틸
# 설명: 동기 DB 호출을 안전하게 재시도 (지수 백오프 + 지터) 및 유틸
# ============================================================================

async def _async_retry(
    fn: Callable[[], T],
    *,
    name: str,
    retries: int = 3,
    base_delay: float = 0.8,
    fallback: Optional[Callable[[], T]] = None,
) -> Optional[T]:
    """지수 백오프+jitter로 재시도하고 실패 시 fallback/None 반환."""
    last_err: Optional[Exception] = None
    for attempt in range(1, retries + 1):
        try:
            return await asyncio.to_thread(fn)
        except Exception as e:
            last_err = e
            jitter = random.uniform(0, 0.3)
            delay = base_delay * (2 ** (attempt - 1)) + jitter
            await asyncio.sleep(delay)
    if last_err is not None:
        logger.error(
            "retry failed: name=%s retries=%s error=%s", name, retries, str(last_err),
            exc_info=last_err,
        )
    if fallback is not None:
        try:
            fb_val = fallback()
            return fb_val
        except Exception as fb_err:
            logger.error("fallback failed: name=%s error=%s", name, str(fb_err), exc_info=fb_err)
            return None
    return None
 

def _is_valid_uuid(value: str) -> bool:
    """UUID 문자열 형식 검증 (v1~v8 포함)"""
    try:
        uuid.UUID(value)
        return True
    except Exception:
        return False
        
def _to_jsonable(value: Any) -> Any:
    """간단한 JSON 변환: dict 재귀, list/tuple/set→list, 기본형 유지, 나머지는 repr."""
    try:
        if value is None or isinstance(value, (str, int, float, bool)):
            return value
        if isinstance(value, dict):
            return {str(k): _to_jsonable(v) for k, v in value.items()}
        if isinstance(value, (list, tuple, set)):
            return [_to_jsonable(v) for v in list(value)]
        if hasattr(value, "__dict__"):
            return _to_jsonable(vars(value))
        return repr(value)
    except Exception:
        return repr(value)


# ============================================================================
# DB 연결/클라이언트
# 설명: 환경 변수 로드, Supabase 클라이언트 초기화/반환, 컨슈머 식별자
# ============================================================================
_supabase_client: Optional[Client] = None


def initialize_db() -> None:
    """환경변수 로드 및 Supabase 클라이언트 초기화"""
    global _supabase_client
    if _supabase_client is not None:
        return
    try:
        if os.getenv("ENV") != "production":
            load_dotenv()
        supabase_url = os.getenv("SUPABASE_URL") or os.getenv("SUPABASE_KEY_URL")
        supabase_key = os.getenv("SUPABASE_KEY") or os.getenv("SUPABASE_ANON_KEY")
        if not supabase_url or not supabase_key:
            raise RuntimeError("SUPABASE_URL 및 SUPABASE_KEY가 필요합니다")
        _supabase_client = create_client(supabase_url, supabase_key)
    except Exception as e:
        logger.error("initialize_db failed: %s", str(e), exc_info=e)
        raise


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


# ============================================================================
# 데이터 조회
# 설명: TODOLIST 테이블 조회, 완료 output 목록 조회, 이벤트 조회, 폼 조회, 테넌트 MCP 설정 조회, 사용자 및 에이전트 조회
# ============================================================================
async def polling_pending_todos(agent_orch: str, consumer: str) -> Optional[Dict[str, Any]]:
    """TODOLIST 테이블에서 대기중인 워크아이템을 조회 (agent_orch 전달).

    - 정상 동작 시 로그를 남기지 않는다.
    - 예외 시에만 풍부한 에러 정보를 남기되, 호출자에게 None을 반환하여 폴링 루프가 중단되지 않게 한다.
    """
    if agent_orch is None:
        agent_orch = ""
    if consumer is None:
        consumer = ""

    def _call():
        client = get_db_client()
        consumer_id = consumer or socket.gethostname()
        env = (os.getenv("ENV") or "").lower()

        if env == "dev":
            resp = client.rpc(
                "fetch_pending_task_dev",
                {"p_agent_orch": agent_orch, "p_consumer": consumer_id, "p_limit": 1, "p_tenant_id": "uengine"},
            ).execute()
        else:
            resp = client.rpc(
                "fetch_pending_task",
                {"p_agent_orch": agent_orch, "p_consumer": consumer_id, "p_limit": 1},
            ).execute()

        rows = resp.data or []
        return rows[0] if rows else None

    return await _async_retry(_call, name="polling_pending_todos", fallback=lambda: None)


def fetch_human_response_sync(job_id: str) -> Optional[Dict[str, Any]]:
    """events에서 특정 job_id의 human_response 조회"""
    if not job_id:
        return None
    try:
        client = get_db_client()
        resp = (
            client
            .table("events")
            .select("*")
            .eq("job_id", job_id)
            .eq("event_type", "human_response")
            .execute()
        )
        rows = resp.data or []
        return rows[0] if rows else None
    except Exception as e:
        logger.error("fetch_human_response_sync failed: %s", str(e), exc_info=e)
        return None


async def fetch_task_status(todo_id: str) -> Optional[str]:
    """todo의 draft_status를 조회한다."""
    if not todo_id:
        return None
    def _call():
        client = get_db_client()
        return (
            client.table("todolist").select("draft_status").eq("id", todo_id).single().execute()
        )

    try:
        resp = await _async_retry(_call, name="fetch_task_status")
    except Exception as e:
        logger.error("fetch_task_status fatal: %s", str(e), exc_info=e)
        return None
    if not resp or not getattr(resp, "data", None):
        return None
    try:
        return resp.data.get("draft_status")
    except Exception as e:
        logger.error("fetch_task_status parse error: %s", str(e), exc_info=e)
        return None



async def fetch_all_agents() -> List[Dict[str, Any]]:
    """모든 에이전트 목록을 정규화하여 반환한다."""
    def _call():
        client = get_db_client()
        return (
            client.table("users")
            .select("id, username, role, goal, persona, tools, profile, model, tenant_id, is_agent")
            .eq("is_agent", True)
            .execute()
        )

    try:
        resp = await _async_retry(_call, name="fetch_all_agents")
    except Exception as e:
        logger.error("fetch_all_agents fatal: %s", str(e), exc_info=e)
        return []
    rows = resp.data or [] if resp else []
    try:
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
                    "endpoint": row.get("endpoint"),
                }
            )
        return normalized
    except Exception as e:
        logger.error("fetch_all_agents parse error: %s", str(e), exc_info=e)
        return []


async def fetch_agent_data(user_ids: str) -> List[Dict[str, Any]]:
    """TODOLIST의 user_id 값으로, 역할로 지정된 에이전트를 조회하고 정규화해 반환한다."""

    raw_ids = [x.strip() for x in (user_ids or "").split(",") if x.strip()]
    valid_ids = [x for x in raw_ids if _is_valid_uuid(x)]

    if not valid_ids:
        return await fetch_all_agents()

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
                    "endpoint": row.get("endpoint"),
                }
            )
        return normalized

    try:
        result = await _async_retry(_call, name="fetch_agent_data", fallback=lambda: [])
    except Exception as e:
        logger.error("fetch_agent_data fatal: %s", str(e), exc_info=e)
        return await fetch_all_agents()

    if not result:
        return await fetch_all_agents()

    return result


async def fetch_form_types(tool_val: str, tenant_id: str) -> Tuple[str, List[Dict[str, Any]], Optional[str]]:
    """폼 타입 정의를 조회해 (form_id, fields, html)로 반환한다."""
    if tool_val is None:
        tool_val = ""
    if tenant_id is None:
        tenant_id = ""
    form_id = tool_val[12:] if tool_val.startswith("formHandler:") else tool_val

    def _call():
        client = get_db_client()
        resp = (
            client
            .table("form_def")
            .select("fields_json, html")
            .eq("id", form_id)
            .eq("tenant_id", tenant_id)
            .execute()
        )
        fields_json = resp.data[0].get("fields_json") if resp.data else None
        form_html = resp.data[0].get("html") if resp.data else None
        if not fields_json:
            return form_id, [{"key": form_id, "type": "default", "text": ""}], form_html
        return form_id, fields_json, form_html

    try:
        resp = await _async_retry(
            _call,
            name="fetch_form_types",
            fallback=lambda: (form_id, [{"key": form_id, "type": "default", "text": ""}], None),
        )
    except Exception as e:
        logger.error("fetch_form_types fatal: %s", str(e), exc_info=e)
        resp = None
    return resp if resp else (form_id, [{"key": form_id, "type": "default", "text": ""}], None)


async def fetch_tenant_mcp_config(tenant_id: str) -> Optional[Dict[str, Any]]:
    """테넌트 MCP 설정을 조회해 반환한다."""
    if not tenant_id:
        return None
    def _call():
        client = get_db_client()
        return client.table("tenants").select("mcp").eq("id", tenant_id).single().execute()

    try:
        resp = await _async_retry(_call, name="fetch_tenant_mcp_config", fallback=lambda: None)
    except Exception as e:
        logger.error("fetch_tenant_mcp_config fatal: %s", str(e), exc_info=e)
        return None
    return resp.data.get("mcp") if resp and getattr(resp, "data", None) else None


async def fetch_human_users_by_proc_inst_id(proc_inst_id: str) -> str:
    """proc_inst_id로 현재 프로세스의 모든 사용자 이메일 목록을 쉼표로 반환한다."""
    if not proc_inst_id:
        return ""
    
    def _sync():
        try:
            supabase = get_db_client()
            
            resp = (
                supabase
                .table('todolist')
                .select('user_id')
                .eq('proc_inst_id', proc_inst_id)
                .execute()
            )
            
            if not resp.data:
                return ""
            
            all_user_ids = set()
            for row in resp.data:
                user_id = row.get('user_id', '')
                if user_id:
                    ids = [id.strip() for id in user_id.split(',') if id.strip()]
                    all_user_ids.update(ids)
            
            if not all_user_ids:
                return ""
            
            human_user_emails = []
            for user_id in all_user_ids:
                if not _is_valid_uuid(user_id):
                    continue
                
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
                    if not is_agent:
                        email = (user.get('email') or '').strip()
                        if email:
                            human_user_emails.append(email)
            
            return ','.join(human_user_emails)
            
        except Exception as e:
            logger.error("fetch_human_users_by_proc_inst_id failed: %s", str(e), exc_info=e)
            return ""
    
    return await asyncio.to_thread(_sync)


# ============================================================================
# 데이터 저장
# 설명: 이벤트/알림/작업 결과 저장
# ============================================================================
async def record_event(payload: Dict[str, Any]) -> None:
    """UI용 events 테이블에 이벤트 기록 (전달된 payload 그대로 저장)"""
    if payload is None:
        logger.error("record_event invalid payload: None")
        return
    def _call():
        client = get_db_client()
        safe_payload = _to_jsonable(payload)
        # 상태값이 빈 문자열이면 NULL로
        if isinstance(safe_payload, dict):
            status_val = safe_payload.get("status")
            if status_val == "":
                safe_payload["status"] = None
        return client.table("events").insert(safe_payload).execute()

    try:
        resp = await _async_retry(_call, name="record_event", fallback=lambda: None)
    except Exception as e:
        try:
            logger.error("record_event fatal: %s payload=%s", str(e), json.dumps(_to_jsonable(payload), ensure_ascii=False), exc_info=e)
        except Exception:
            logger.error("record_event fatal (payload dump failed): %s", str(e), exc_info=e)
        return
    if resp is None:
        try:
            logger.error("events insert 실패: payload=%s", json.dumps(_to_jsonable(payload), ensure_ascii=False))
        except Exception:
            logger.error("events insert 실패 (payload dump failed)")



async def save_task_result(todo_id: str, result: Any, final: bool = False) -> None:
    """작업 결과를 저장한다. final=True 시 최종 저장."""
    if not todo_id:
        logger.error("save_task_result invalid todo_id: %s", str(todo_id))
        return
    # 안전한 직렬화: 실패 시 문자열화하여 저장자가 원인 파악 가능
    def _safe_payload(val: Any) -> Any:
        try:
            return _to_jsonable(val)
        except Exception as e:
            logger.error("save_task_result payload serialization failed: %s", str(e), exc_info=e)
            try:
                return {"repr": repr(val)}
            except Exception:
                return {"error": "unserializable payload"}

    def _call():
        client = get_db_client()
        payload = _safe_payload(result)
        return client.rpc(
            "save_task_result",
            {"p_todo_id": todo_id, "p_payload": payload, "p_final": bool(final)},
        ).execute()

    try:
        await _async_retry(_call, name="save_task_result", fallback=lambda: None)
    except Exception as e:
        logger.error("save_task_result fatal: %s", str(e), exc_info=e)


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
    """notifications 테이블에 알림 저장"""
    try:
        # 대상 사용자가 없으면 작업 생략
        if not user_ids_csv:
            return

        client = get_db_client()

        user_ids: List[str] = [uid.strip() for uid in user_ids_csv.split(',') if uid and uid.strip()]
        if not user_ids:
            return
        
        rows: List[Dict[str, Any]] = []
        for uid in user_ids:
            rows.append(
                {
                    "id": str(uuid.uuid4()),
                    "user_id": uid,
                    "tenant_id": tenant_id,
                    "title": title,
                    "description": description,
                    "type": notif_type,
                    "url": url,
                    "from_user_id": from_user_id,
                }
            )

        client.table("notifications").insert(rows).execute()
    except Exception as e:
        logger.error("save_notification failed: %s", str(e), exc_info=e)

# ============================================================================
# 상태 변경
# 설명: 실패 작업 상태 업데이트
# ============================================================================

async def update_task_error(todo_id: str) -> None:
    """실패 작업의 상태를 FAILED로 갱신한다."""
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

    try:
        await _async_retry(_call, name="update_task_error", fallback=lambda: None)
    except Exception as e:
        logger.error("update_task_error fatal: %s", str(e), exc_info=e)
