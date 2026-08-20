import os
import json
import asyncio
import socket
from typing import Any, Dict, List, Optional, Tuple, Callable, TypeVar
from importlib import resources

from dotenv import load_dotenv
from supabase import Client, create_client
import logging
import random

T = TypeVar("T")
logger = logging.getLogger(__name__)

# ------------------------------ Retry & JSON utils ------------------------------
async def _async_retry(
    fn: Callable[[], Any],
    *,
    name: str,
    retries: int = 3,
    base_delay: float = 0.8,
    fallback: Optional[Callable[[], Any]] = None,
) -> Optional[Any]:
    """
    - 각 시도 실패: warning 로깅(시도/지연/에러 포함)
    - 최종 실패: FATAL 로깅(스택 포함), 예외는 재전파하지 않고 None 반환(기존 정책 유지)
    - fallback 이 있으면 실행(실패 시에도 로깅 후 None)
    """
    last_err: Optional[Exception] = None
    for attempt in range(1, retries + 1):
        try:
            return await asyncio.to_thread(fn)
        except Exception as e:
            last_err = e
            jitter = random.uniform(0, 0.3)
            delay = base_delay * (2 ** (attempt - 1)) + jitter
            logger.warning(
                "retry warn: name=%s attempt=%d/%d delay=%.2fs error=%s",
                name, attempt, retries, delay, str(e),
                exc_info=e
            )
            await asyncio.sleep(delay)

    # 최종 실패
    if last_err is not None:
        logger.error(
            "FATAL: retry failed: name=%s retries=%s error=%s",
            name, retries, str(last_err), exc_info=last_err
        )

    if fallback is not None:
        try:
            return fallback()
        except Exception as fb_err:
            logger.error("fallback failed: name=%s error=%s", name, str(fb_err), exc_info=fb_err)
            return None
    return None

def _to_jsonable(value: Any) -> Any:
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

# ------------------------------ DB Client ------------------------------
_supabase_client: Optional[Client] = None

def initialize_db() -> None:
    global _supabase_client
    if _supabase_client is not None:
        return
    try:
        if os.getenv("ENV") != "production":
            load_dotenv()

        supabase_url = os.getenv("SUPABASE_URL") or os.getenv("SUPABASE_KEY_URL")
        supabase_key = os.getenv("SERVICE_ROLE_KEY") or os.getenv("SUPABASE_KEY") or os.getenv("SUPABASE_ANON_KEY")
        logger.info(
            "[SUPABASE 연결정보]\n  URL: %s\n  KEY: %s\n",
            supabase_url,
            supabase_key
        )
        if not supabase_url or not supabase_key:
            raise RuntimeError("SUPABASE_URL 및 SUPABASE_KEY가 필요합니다")
        _supabase_client = create_client(supabase_url, supabase_key)
    except Exception as e:
        logger.error("initialize_db failed: %s", str(e), exc_info=e)
        raise

def get_db_client() -> Client:
    if _supabase_client is None:
        raise RuntimeError("DB 미초기화: initialize_db() 먼저 호출")
    return _supabase_client

def get_consumer_id() -> str:
    env_consumer = os.getenv("CONSUMER_ID")
    if env_consumer:
        return env_consumer
    host = socket.gethostname()
    pid = os.getpid()
    return f"{host}:{pid}"

# ------------------------------ Polling ------------------------------
async def polling_pending_todos(agent_orch: str, consumer: str) -> Optional[Dict[str, Any]]:
    """단일 RPC(fetch_pending_task) 호출: p_env 로 dev/prod 분기"""
    if agent_orch is None:
        agent_orch = ""
    if consumer is None:
        consumer = ""

    def _call():
        client = get_db_client()
        consumer_id = consumer or socket.gethostname()

        # ENV 값을 dev / (그외=prod) 로만 정규화
        p_env = (os.getenv("ENV") or "").lower()
        if p_env != "dev":
            p_env = "prod"

        logger.info("\n🔍 [폴링 시작] 작업 대기 중...")
        logger.info("agent_orch=%s, consumer_id=%s, p_env=%s, p_limit=%d", agent_orch, get_consumer_id(), p_env, 1)
        resp = client.rpc(
            "fetch_pending_task",
            {
                "p_agent_orch": agent_orch,
                "p_consumer": consumer_id,
                "p_limit": 1,
                "p_env": p_env,
            },
        ).execute()

        rows = resp.data or []
        if not rows:
            logger.info("\n❌ [폴링 결과 없음] 작업 대기 중...")
            return None
        
        row = rows[0]
        # 빈 값들을 NULL로 변환
        if row.get("feedback") in ([], {}):
            row["feedback"] = None
        if row.get("output") in ([], {}):
            row["output"] = None
        if row.get("draft") in ([], {}):
            row["draft"] = None

        if agent_orch == "browser-automation-agent":
            resp = (
                client.table("env").select("value").eq("key", "browser_use").eq("tenant_id", row["tenant_id"] or "").execute()
            )
            data = (resp.data or [])
            logger.info("browser_use_sensitive_data: %s", data)
            if not data:
                row["sensitive_data"] = "{}"
            else:
                row["sensitive_data"] = data[0].get("value") if data else None

            logger.info("browser_use_sensitive_data: %s", row["sensitive_data"])
            
        return row

    return await _async_retry(_call, name="polling_pending_todos", fallback=lambda: None)


# ------------------------------ Fetch Single Todo ------------------------------
async def fetch_todo_by_id(todo_id: str) -> Optional[Dict[str, Any]]:
    """todo_id로 단건 조회 후 컨텍스트 준비에 필요한 형태로 정규화합니다.

    - 어떤 필드도 업데이트하지 않고, 조건 없이 id로만 조회합니다.
    """
    if not todo_id:
        return None

    def _call():
        client = get_db_client()
        resp = (
            client.table("todolist")
            .select("*")
            .eq("id", todo_id)
            .single()
            .execute()
        )
        row = getattr(resp, "data", None)
        if not row:
            return None
        return row

    row = await _async_retry(_call, name="fetch_todo_by_id", fallback=lambda: None)
    if not row:
        return None

    # 빈 컨테이너 정규화
    if row.get("feedback") in ([], {}):
        row["feedback"] = None
    if row.get("output") in ([], {}):
        row["output"] = None
    if row.get("draft") in ([], {}):
        row["draft"] = None

    return row


async def fetch_todo_draft_status(todo_id: str) -> Optional[str]:
    """취소 감지 폴링 전용 경량 조회: draft_status 컬럼만 가져온다.

    fetch_todo_by_id는 컨텍스트 준비용으로 전체 행(select *)을 가져오지만, 실행 중
    반복 폴링에는 컬럼 하나면 충분하다.
    """
    if not todo_id:
        return None

    def _call():
        client = get_db_client()
        resp = (
            client.table("todolist")
            .select("draft_status")
            .eq("id", todo_id)
            .single()
            .execute()
        )
        row = getattr(resp, "data", None)
        return (row or {}).get("draft_status")

    return await _async_retry(_call, name="fetch_todo_draft_status", retries=1, fallback=lambda: None)

# ------------------------------ Events & Results ------------------------------
async def record_events_bulk(payloads: List[Dict[str, Any]]) -> None:
    """이벤트 다건 저장 함수"""

    if not payloads:
        return

    safe_list: List[Dict[str, Any]] = []
    for p in payloads:
        sp = _to_jsonable(p)
        if isinstance(sp, dict) and sp.get("status", "") == "":
            sp["status"] = None
        safe_list.append(sp)

    def _call():
        client = get_db_client()
        return client.rpc("record_events_bulk", {"p_events": safe_list}).execute()

    res = await _async_retry(_call, name="record_events_bulk", fallback=lambda: None)
    if res is None:
        logger.error("❌ record_events_bulk failed: events not persisted count=%d", len(safe_list))
    else:
        logger.info("record_events_bulk ok: count=%d", len(safe_list))

async def record_event(payload: Dict[str, Any]) -> None:
    """단건 이벤트 저장 함수"""

    if not payload:
        return

    def _call():
        client = get_db_client()
        safe_payload = _to_jsonable(payload)
        if isinstance(safe_payload, dict) and safe_payload.get("status", "") == "":
            safe_payload["status"] = None
        return client.table("events").insert(safe_payload).execute()

    res = await _async_retry(_call, name="record_event", fallback=lambda: None)
    if res is None:
        logger.error("❌ record_event failed =%s", payload.get("event_type"))
    else:
        logger.info("record_event ok: event_type=%s", payload.get("event_type"))

async def save_task_result(todo_id: str, result: Any, final: bool = False) -> None:
    """결과 저장 함수"""

    if not todo_id:
        logger.error("save_task_result invalid todo_id: %s", str(todo_id))
        return

    def _safe(val: Any) -> Any:
        try:
            return _to_jsonable(val)
        except Exception:
            try:
                return {"repr": repr(val)}
            except Exception:
                return {"error": "unserializable payload"}

    def _call():
        client = get_db_client()
        payload = _safe(result)
        return client.rpc("save_task_result", {"p_todo_id": todo_id, "p_payload": payload, "p_final": bool(final)}).execute()

    res = await _async_retry(_call, name="save_task_result", fallback=lambda: None)
    if res is None:
        logger.error("❌ save_task_result failed todo_id=%s", todo_id)
    else:
        logger.info("save_task_result ok todo_id=%s", todo_id)


# ------------------------------ Chats ------------------------------
async def insert_chat_message(
    *,
    uuid: str,
    chat_id: str,
    messages: Any,
    tenant_id: Optional[str] = None,
    thread_id: Optional[str] = None,
) -> None:
    """채팅 메시지를 chats 테이블에 upsert합니다. (1 row = 1 message, PK=uuid)

    - `uuid`: chats.uuid (PK). 이미 그 uuid로 row가 있으면(예: 클라이언트가 스트리밍
      중 낙관적으로 먼저 저장해둔 row) insert 충돌 대신 그 row를 갱신한다 — 같은
      논리적 메시지가 클라이언트/서버 양쪽에서 각자 다른 uuid로 중복 저장되는 걸
      막으려면 호출자가 동일한 uuid를 넘겨야 한다.
    - `chat_id`: chats.id (NOT NULL; 외부 서비스가 의미를 부여. 보통 conversation_id/thread_id를 권장)
    - `messages`: chats.messages (jsonb)
    - `tenant_id`: 선택. 미지정 시 DB default(public.tenant_id())에 위임될 수 있음
    - `thread_id`: 선택
    """
    if not uuid:
        raise ValueError("insert_chat_message requires uuid")
    if not chat_id:
        raise ValueError("insert_chat_message requires chat_id")

    payload: Dict[str, Any] = {
        "uuid": str(uuid),
        "id": str(chat_id),
        "messages": _to_jsonable(messages),
    }
    if tenant_id:
        payload["tenant_id"] = tenant_id
    if thread_id:
        payload["thread_id"] = thread_id

    def _call():
        client = get_db_client()
        return client.table("chats").upsert(payload, on_conflict="uuid").execute()

    res = await _async_retry(_call, name="insert_chat_message", retries=1, fallback=lambda: None)
    if res is None and tenant_id:
        # tenant_id FK가 맞지 않는 환경(로컬 샘플 등)에서는 tenant_id를 생략하고 한 번 더 시도
        payload_wo_tenant = dict(payload)
        payload_wo_tenant.pop("tenant_id", None)

        def _call_wo_tenant():
            client = get_db_client()
            return client.table("chats").upsert(payload_wo_tenant, on_conflict="uuid").execute()

        res = await _async_retry(_call_wo_tenant, name="insert_chat_message_without_tenant", retries=1, fallback=lambda: None)

    if res is None:
        logger.error("❌ insert_chat_message failed uuid=%s", uuid)
    else:
        logger.info("insert_chat_message ok uuid=%s", uuid)


# Backward-compatible alias (deprecated)
upsert_chat = insert_chat_message

# ------------------------------ Failure Status ------------------------------
async def update_task_error(todo_id: str) -> None:
    """작업 실패 상태 업데이트 함수"""

    if not todo_id:
        return

    def _call():
        client = get_db_client()
        return client.table("todolist").update({"draft_status": "FAILED", "consumer": None}).eq("id", todo_id).execute()

    res = await _async_retry(_call, name="update_task_error", fallback=lambda: None)
    if res is None:
        logger.error("❌ update_task_error failed todo_id=%s", todo_id)
    else:
        logger.info("update_task_error ok todo_id=%s", todo_id)

# ============================== Prepare Context ==============================

from typing import Any, Dict, List, Optional, Tuple

async def fetch_form_def(tool_val: str, tenant_id: str) -> Tuple[str, List[Dict[str, Any]], Optional[str]]:
    """폼 정의 조회 함수"""
    form_id = (tool_val or "").replace("formHandler:", "", 1)

    def _call():
        client = get_db_client()
        resp = (
            client.table("form_def")
            .select("fields_json, html")
            .eq("id", form_id)
            .eq("tenant_id", tenant_id or "")
            .execute()
        )
        data = (resp.data or [])
        if not data:
            return None

        row = data[0]
        return {
            "fields": row.get("fields_json"),
            "html": row.get("html"),
        }

    try:
        res = await _async_retry(_call, name="fetch_form_def")
    except Exception as e:
        logger.error("fetch_form_def fatal: %s", str(e), exc_info=e)
        res = None

    if not res or not res.get("fields"):
        # 기본(자유형식) 폼
        return (
            form_id or "freeform",
            [{"key": "freeform", "type": "textarea", "text": "자유형식 입력", "placeholder": "원하는 내용을 자유롭게 입력해주세요."}],
            None,
        )
    return (form_id or "freeform", res["fields"], res.get("html"))


def _load_default_settings() -> Dict[str, Any]:
    """default_settings.json 파일에서 기본 설정을 로드하는 함수"""
    try:
        try:
            # Python 3.9+ 방식
            if hasattr(resources, 'files'):
                with resources.files('processgpt_agent_sdk').joinpath('default_settings.json').open('r', encoding='utf-8') as f:
                    data = json.load(f)
            else:
                # Python 3.7-3.8 방식
                with resources.open_text('processgpt_agent_sdk', 'default_settings.json', encoding='utf-8') as f:
                    data = json.load(f)
        except (FileNotFoundError, ModuleNotFoundError):
            # 패키지로 설치되지 않은 경우 (개발 환경) fallback
            current_dir = os.path.dirname(os.path.abspath(__file__))
            settings_json_path = os.path.join(current_dir, "default_settings.json")
            if not os.path.exists(settings_json_path):
                logger.warning("default_settings.json 파일을 찾을 수 없습니다: %s", settings_json_path)
                return {"default_agents": [], "default_mcp": None}
            with open(settings_json_path, "r", encoding="utf-8") as f:
                data = json.load(f)
        
        return {
            "default_agents": data.get("default_agents", []),
            "default_mcp": data.get("default_mcp")
        }
    except Exception as e:
        logger.error("default_settings.json 로드 실패: %s", str(e), exc_info=e)
        return {"default_agents": [], "default_mcp": None}

async def fetch_users_grouped(user_ids: List[str]) -> Tuple[List[Dict[str, Any]], List[Dict[str, Any]]]:
    """해당 todo에서 사용자 목록과 에이전트 목록 조회하는 함수 (DB + default_settings.json)"""
    ids = [u for u in (user_ids or []) if u]
    if not ids:
        return ([], [])

    # DB에서 조회
    def _call():
        client = get_db_client()
        resp = (
            client.table("users")
            .select("*")
            .in_("id", ids)
            .execute()
        )
        rows = resp.data or []
        return rows

    try:
        rows = await _async_retry(_call, name="fetch_users_grouped", fallback=lambda: [])
    except Exception as e:
        logger.error("fetch_users_grouped fatal: %s", str(e), exc_info=e)
        rows = []

    # default_settings.json에서 기본 에이전트 조회
    default_settings = _load_default_settings()
    default_agents = default_settings.get("default_agents", [])
    ids_set = set(ids)
    
    # DB에서 조회된 ID 집합 (중복 제거를 위해)
    db_ids_set = {r.get("id") for r in rows if r.get("id")}
    
    # default_settings.json에서 요청된 ID에 해당하는 에이전트 찾기 (DB에 없는 것만)
    json_agents = []
    for agent in default_agents:
        agent_id = agent.get("id")
        if agent_id and agent_id in ids_set and agent_id not in db_ids_set:
            json_agents.append(agent)

    # DB 결과와 JSON 결과 병합
    agents, users = [], []
    for r in rows:
        if r.get("is_agent") is True:
            agents.append(r)
        else:
            users.append(r)
    
    # JSON에서 찾은 에이전트 추가
    agents.extend(json_agents)
    
    return (agents, users)

async def fetch_email_users_by_proc_inst_id(proc_inst_id: str) -> str:
    """proc_inst_id로 이메일 수집(사람만): todolist → users(in) 한 번에"""
    if not proc_inst_id:
        return ""

    def _call():
        client = get_db_client()
        # 3-1) 해당 인스턴스의 user_id 수집(중복 제거)
        tl = (
            client.table("todolist")
            .select("user_id")
            .eq("proc_inst_id", proc_inst_id)
            .execute()
        )
        ids_set = set()
        for row in (tl.data or []):
            uid_csv = (row.get("user_id") or "").strip()
            if not uid_csv:
                continue
            # user_id는 문자열 CSV라고 전제
            for uid in uid_csv.split(","):
                u = uid.strip()
                if u:
                    ids_set.add(u)
        if not ids_set:
            return []

        # 3-2) 한 번의 IN 조회로 사람만 이메일 추출
        ur = (
            client.table("users")
            .select("id, email, is_agent")
            .in_("id", list(ids_set))
            .eq("is_agent", False)
            .execute()
        )
        emails = []
        for u in (ur.data or []):
            email = (u.get("email") or "").strip()
            if email:
                emails.append(email)
        # 중복 제거 및 정렬(보기 좋게)
        return sorted(set(emails))

    try:
        emails = await _async_retry(_call, name="fetch_email_users_by_proc_inst_id", fallback=lambda: [])
    except Exception as e:
        logger.error("fetch_email_users_by_proc_inst_id fatal: %s", str(e), exc_info=e)
        emails = []

    return ",".join(emails) if emails else ""

async def fetch_tenant_mcp(tenant_id: str) -> Optional[Dict[str, Any]]:
    """mcp 설정 조회 함수 (DB + default_settings.json의 기본 MCP 병합)"""
    # 기본 MCP 로드
    default_settings = _load_default_settings()
    default_mcp = default_settings.get("default_mcp")
    
    if not tenant_id:
        # tenant_id가 없으면 기본 MCP만 반환
        return default_mcp

    # DB에서 tenant의 MCP 조회
    def _call():
        client = get_db_client()
        return (
            client.table("tenants")
            .select("mcp")
            .eq("id", tenant_id)
            .single()
            .execute()
        )

    try:
        resp = await _async_retry(_call, name="fetch_tenant_mcp", fallback=lambda: None)
    except Exception as e:
        logger.error("fetch_tenant_mcp fatal: %s", str(e), exc_info=e)
        resp = None

    db_mcp = resp.data.get("mcp") if resp and getattr(resp, "data", None) else None
    
    # 기본 MCP와 DB MCP 병합 (DB MCP가 우선, 기본 MCP는 보완)
    if not db_mcp:
        return default_mcp
    
    if not default_mcp:
        return db_mcp
    
    # 두 MCP 병합: DB MCP의 mcpServers를 우선하고, 기본 MCP의 mcpServers로 보완
    merged_mcp = db_mcp.copy() if isinstance(db_mcp, dict) else {}
    
    if isinstance(default_mcp, dict) and "mcpServers" in default_mcp:
        if "mcpServers" not in merged_mcp:
            merged_mcp["mcpServers"] = {}
        
        # 기본 MCP의 서버들을 추가 (DB에 없는 것만)
        for server_name, server_config in default_mcp["mcpServers"].items():
            if server_name not in merged_mcp["mcpServers"]:
                merged_mcp["mcpServers"][server_name] = server_config
    
    return merged_mcp

async def fetch_proc_inst_sources(proc_inst_id: str) -> List[Dict[str, Any]]:
    """proc_inst_id로 프로세스 인스턴스 소스 목록 조회 함수"""
    if not proc_inst_id:
        return []

    def _call():
        client = get_db_client()
        resp = (
            client.table("proc_inst_source")
            .select("*")
            .eq("proc_inst_id", proc_inst_id)
            .execute()
        )
        return resp.data or []

    try:
        rows = await _async_retry(_call, name="fetch_proc_inst_sources", fallback=lambda: [])
    except Exception as e:
        logger.error("fetch_proc_inst_sources fatal: %s", str(e), exc_info=e)
        rows = []

    return rows