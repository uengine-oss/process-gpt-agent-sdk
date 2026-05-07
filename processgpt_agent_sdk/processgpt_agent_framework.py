import asyncio
import logging
import json
import os
import signal
import uuid
from typing import Dict, Any, Optional
from dataclasses import dataclass

from dotenv import load_dotenv

from a2a.server.agent_execution import AgentExecutor, RequestContext
from a2a.server.events import EventQueue

from .database import (
    initialize_db,
    polling_pending_todos,
    record_event,
    update_task_error,
    get_consumer_id,
    fetch_form_def,
    fetch_users_grouped,
    fetch_email_users_by_proc_inst_id,
    fetch_tenant_mcp,
    fetch_proc_inst_sources,
)
from .utils import summarize_error_to_user, summarize_feedback, set_agent_model
from .event_queue_process import ProcessEventQueue, ProcessGPTEventQueue
from .chat_mode import ChatEventQueue, ChatRequest, ChatRequestContext, ChatStreamer, drain_sse_queue, persist_chat_to_db
from .context_api import REQUEST_KIND_PROCESS

load_dotenv()
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# ------------------------------ 커스텀 예외 ------------------------------
class ContextPreparationError(Exception):
    """컨텍스트 준비 실패를 상위 경계에서 단일 처리하기 위한 래퍼 예외."""
    def __init__(self, original: Exception, friendly: Optional[str] = None):
        super().__init__(f"{type(original).__name__}: {str(original)}")
        self.original = original
        self.friendly = friendly

from .event_coalescer import flush_events_now, enqueue_ui_event_coalesced

# ------------------------------ Request Context ------------------------------
@dataclass
class TodoListRowContext:
    row: Dict[str, Any]

class ProcessGPTRequestContext(RequestContext):
    def __init__(self, row: Dict[str, Any]):
        self.row = row
        self._task_id = str(row.get("id") or "")
        self._context_id = str(row.get("root_proc_inst_id") or row.get("proc_inst_id") or "")
        self._user_input = (row.get("query") or "").strip()
        self._message = self._user_input
        self._current_task = None
        self._task_state = row.get("draft_status") or ""
        self._extra_context: Dict[str, Any] = {}

    async def prepare_context(self) -> None:
        """익스큐터를 위한 컨텍스트 준비를 합니다."""

        effective_proc_inst_id = self.row.get("root_proc_inst_id") or self.row.get("proc_inst_id")
        tool_val = self.row.get("tool") or ""
        tenant_id = self.row.get("tenant_id") or ""
        user_ids = self.row.get("user_id") or ""

        try:
            # 데이터베이스 조회
            user_id_list = [u.strip() for u in (user_ids or '').split(',') if u.strip()]
            notify_task = fetch_email_users_by_proc_inst_id(effective_proc_inst_id)
            mcp_task = fetch_tenant_mcp(tenant_id)
            form_task = fetch_form_def(tool_val, tenant_id)
            users_task = fetch_users_grouped(user_id_list)
            sources_task = fetch_proc_inst_sources(effective_proc_inst_id)

            notify_emails, tenant_mcp, form_tuple, users_group, sources = await asyncio.gather(
                notify_task, mcp_task, form_task, users_task, sources_task
            )
            form_id, form_fields, form_html = form_tuple
            agents, users = users_group
            
            # 글로벌 에이전트 모델 설정
            set_agent_model(agents[0] if agents else None)
            
            logger.info("\n\n🔍 [데이터베이스 조회 결과]")
            
            # Users 정보
            if users:
                user_info = []
                for u in users[:5]:
                    name = u.get("name", u.get("username", "Unknown"))
                    email = u.get("email", "")
                    user_str = f"{name}({email})" if email else name
                    # None 값 제거
                    if user_str and user_str != "None":
                        user_info.append(user_str)
                logger.info("🔧 [Users 정보] user_info 리스트: %s", user_info)
                logger.info("• Users (%d명): %s%s", len(users), ", ".join(user_info), "..." if len(users) > 5 else "")
            else:
                logger.info("• Users: 없음")
            
            # Agents 정보
            if agents:
                agent_info = []
                for a in agents:
                    name = a.get("name", a.get("username", "Unknown"))
                    tools = a.get("tools", "")
                    tool_str = f"[{tools}]" if tools else ""
                    agent_str = f"{name}{tool_str}"
                    # None 값 제거
                    if agent_str and agent_str != "None":
                        agent_info.append(agent_str)
                logger.info("🔧 [Agents 정보] agent_info 리스트: %s", agent_info)
                logger.info("• Agents (%d개): %s%s", len(agents), ", ".join(agent_info), "..." if len(agents) > 5 else "")
            else:
                logger.info("• Agents: 없음")
            
            # Form 정보
            if form_fields:
                pretty_json = json.dumps(form_fields, ensure_ascii=False, separators=(',', ':'))
                logger.info("• Form: %s (%d개 필드) - %s", form_id, len(form_fields), pretty_json)
            else:
                logger.info("• Form: %s (필드 없음)", form_id)
            
            # Notify 정보
            if notify_emails:
                email_list = notify_emails.split(',') if ',' in notify_emails else [notify_emails]
                logger.info("• Notify (%d개): %s", len(email_list), 
                           ", ".join(email_list[:3]) + ("..." if len(email_list) > 3 else ""))
            else:
                logger.info("• Notify: 없음")
            
            # MCP 정보 - 상세 표시
            if tenant_mcp:
                logger.info("• %s 테넌트에 연결된 MCP 설정 정보가 존재합니다.", tenant_id)
            else:
                logger.info("• %s 테넌트에 연결된 MCP 설정 정보가 존재하지 않습니다.", tenant_id)
            
            # Sources 정보
            if sources:
                source_info = []
                for s in sources:
                    file_name = s.get("file_name", "")
                    file_path = s.get("file_path", "")
                    source_str = f"{file_name}"
                    if file_path:
                        source_str += f" ({file_path})"
                    if source_str:
                        source_info.append(source_str)
                logger.info("• Sources (%d개): %s", len(sources), ", ".join(source_info[:3]) + ("..." if len(sources) > 3 else ""))
            else:
                logger.info("• Sources: 없음")
            
            # 피드백 처리
            feedback_data = self.row.get("feedback")
            content_data = self.row.get("output") or self.row.get("draft")
            summarized_feedback = ""
            if feedback_data:
                logger.info("\n\n📝 [피드백 처리]")
                logger.info("• %d자 → AI 요약 중...", len(feedback_data))
                summarized_feedback = await summarize_feedback(feedback_data, content_data)
                logger.info("• 요약 완료: %d자", len(summarized_feedback))
            else:
                logger.info("• 피드백 없음")
            logger.info("✅ [피드백 처리 완료]")

            logger.info("sensitive_data: %s", self.row.get("sensitive_data") or "{}")
            
            # 컨텍스트 구성
            self._extra_context = {
                "id": self.row.get("id"),
                "proc_inst_id": effective_proc_inst_id,
                "root_proc_inst_id": self.row.get("root_proc_inst_id"),
                "activity_name": self.row.get("activity_name"),
                "agents": agents,
                "users": users,
                "tenant_mcp": tenant_mcp,
                "form_fields": form_fields,
                "form_html": form_html,
                "form_id": form_id,
                "notify_user_emails": notify_emails,
                "summarized_feedback": summarized_feedback,
                "sensitive_data": self.row.get("sensitive_data") or "{}",
                "sources": sources,
            }
            
            logger.info("\n\n🎉 [컨텍스트 준비 완료] 모든 데이터 준비됨")
            
        except Exception as e:
            logger.error("❌ [데이터 조회 실패] %s", str(e))
            raise ContextPreparationError(e)

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
        extras = dict(self._extra_context or {})
        extras.setdefault("request_kind", REQUEST_KIND_PROCESS)
        return {"row": self.row, "extras": extras}

    @property
    def metadata(self) -> Dict[str, Any]:
        """A2A 표준 metadata 접근자."""
        md = dict(self._extra_context or {})
        md.setdefault("request_kind", REQUEST_KIND_PROCESS)
        # 최소 호환: 외부 Executor가 필요하면 row도 참조 가능
        md.setdefault("row", self.row)
        return md

    @property
    def task_id(self) -> str:
        return self._task_id

    @property
    def context_id(self) -> str:
        return self._context_id

# ------------------------------ Agent Server ------------------------------
class ProcessGPTAgentServer:
    def __init__(self, agent_executor: AgentExecutor, agent_type: str):
        self.agent_executor = agent_executor
        self.agent_orch = agent_type
        self.is_running = False
        self._shutdown_event = asyncio.Event()
        self._current_todo_id: Optional[str] = None  # 진행 중 작업 추적(참고용)

    async def _install_signal_handlers(self):
        loop = asyncio.get_running_loop()
        try:
            loop.add_signal_handler(signal.SIGTERM, lambda: self._shutdown_event.set())
            loop.add_signal_handler(signal.SIGINT,  lambda: self._shutdown_event.set())
        except NotImplementedError:
            # Windows 등 일부 환경은 지원 안 됨
            pass

    async def run(self):
        self.is_running = True
        logger.info("\n\n🚀 ProcessGPT Agent Server START (agent=%s)\n", self.agent_orch)
        initialize_db()
        await self._install_signal_handlers()

        while self.is_running and not self._shutdown_event.is_set():
            try:
                row = await polling_pending_todos(self.agent_orch, get_consumer_id())

                if row:
                    logger.info("✅ [새 작업 발견] Task ID: %s", row.get("id"))
                    logger.info("• Activity: %s | Tool: %s | Tenant: %s", 
                               row.get("activity_name"), row.get("tool"), row.get("tenant_id"))
                    try:
                        self._current_todo_id = str(row.get("id"))
                        await self.process_todolist_item(row)
                    except Exception as e:
                        # 경계에서 처리(에러 이벤트 + FAILED 마킹) 후 예외 재전달됨.
                        logger.exception("process_todolist_item failed: %s", str(e))
                    finally:
                        self._current_todo_id = None
                    # 작업이 있었으므로 슬립 생략 → 즉시 다음 폴링
                    continue

                # 작업 없을 때만 10초 대기
                await asyncio.sleep(10)

            except Exception as e:
                # 폴링 자체 오류는 특정 작업에 귀속되지 않으므로 상태 마킹 대상 없음
                logger.exception("run loop error: %s", str(e))
                await asyncio.sleep(10)

        # 종료 시 남은 이벤트 강제 flush (오류로 간주하지 않음)
        try:
            await flush_events_now()
            logger.info("🧹 graceful shutdown: pending events flushed")
        except Exception as e:
            logger.exception("flush on shutdown failed: %s", str(e))

        logger.info("👋 Agent server stopped.")

    async def process_todolist_item(self, row: Dict[str, Any]):
        """
        경계 정책(최종본):
        - 어떤 예외든 여기에서 잡힘
        - 항상 단일 경로로:
          1) 사용자 친화 5줄 설명 생성
          2) event_type='error' 단건 이벤트 기록
          3) todolist를 FAILED로 마킹
          4) 예외 재전달(상위 루프는 죽지 않고 다음 폴링)
        """
        task_id = row.get("id")
        logger.info("\n🎯 [작업 처리 시작] Task ID: %s", task_id)
        
        friendly_text: Optional[str] = None

        try:
            # 1) 컨텍스트 준비 (실패 시 ContextPreparationError로 올라옴)
            context = ProcessGPTRequestContext(row)
            await context.prepare_context()

            # 2) 실행
            logger.info("\n\n🤖 [Agent Orchestrator 실행]")
            event_queue = ProcessEventQueue(str(task_id), self.agent_orch, row.get("proc_inst_id"))
            await self.agent_executor.execute(context, event_queue)
            event_queue.task_done()
            logger.info("\n\n🎉 [Agent Orchestrator 완료] Task ID: %s", task_id)

        except Exception as e:
            logger.error("❌ 작업 처리 중 오류 발생: %s", str(e))
            
            # 컨텍스트 실패라면 friendly가 없을 수 있어, 여기서 반드시 생성
            try:
                logger.info("📝 사용자 친화 오류 메시지 생성 중...")
                if isinstance(e, ContextPreparationError) and e.friendly:
                    friendly_text = e.friendly
                else:
                    friendly_text = await summarize_error_to_user(
                        e if not isinstance(e, ContextPreparationError) else e.original,
                        {
                            "task_id": task_id,
                            "proc_inst_id": row.get("proc_inst_id"),
                            "agent_orch": self.agent_orch,
                            "tool": row.get("tool"),
                        },
                    )
                logger.info("✅ 사용자 친화 오류 메시지 생성 완료")
            except Exception:
                logger.warning("⚠️ 사용자 친화 오류 메시지 생성 실패")
                # 요약 생성 실패 시에도 처리 계속
                friendly_text = None

            # 에러 이벤트 기록(단건). 실패해도 로그만 남기고 진행.
            logger.info("📤 오류 이벤트 기록 중...")
            payload: Dict[str, Any] = {
                "id": str(uuid.uuid4()),
                "job_id": "TASK_ERROR",
                "todo_id": str(task_id),
                "proc_inst_id": row.get("proc_inst_id"),
                "crew_type": "agent",
                "event_type": "error",
                "data": {
                    "name": "시스템 오류 알림",
                    "goal": "오류 원인과 대처 안내를 전달합니다.",
                    "agent_profile": "/images/chat-icon.png",
                    "friendly": friendly_text or "처리 중 오류가 발생했습니다. 로그를 확인해 주세요.",
                    "raw_error": f"{type(e).__name__}: {str(e)}" if not isinstance(e, ContextPreparationError) else f"{type(e.original).__name__}: {str(e.original)}",
                }
            }
            try:
                asyncio.create_task(record_event(payload))
                logger.info("✅ 오류 이벤트 기록 완료")
            except Exception:
                logger.exception("❌ 오류 이벤트 기록 실패")

            # 상태 FAILED 마킹
            logger.info("🏷️ 작업 상태 FAILED로 마킹 중...")
            try:
                await update_task_error(str(task_id))
                logger.info("✅ 작업 상태 FAILED 마킹 완료")
            except Exception:
                logger.exception("❌ 작업 상태 FAILED 마킹 실패")

            # 상위로 재전달하여 루프는 계속(죽지 않음)
            logger.error("🔄 오류 처리 완료 - 다음 작업으로 계속 진행")

    def stop(self):
        self.is_running = False
        self._shutdown_event.set()
        logger.info("ProcessGPT Agent Server stopping...")

    def mount_chat_sse(self, app: Any, *, path: str = "/chat/stream"):
        """Starlette/FastAPI 앱에 채팅 SSE 엔드포인트를 마운트합니다.

        - 코어 패키지에서는 Starlette/FastAPI를 강제 의존하지 않기 위해, import는 런타임에 수행합니다.
        - 채팅 모드는 Message-only 패턴을 기대하며, record_events_bulk 경로를 사용하지 않습니다.
        """
        try:
            # Optional dependency (installed via extras)
            from starlette.responses import StreamingResponse  # type: ignore[reportMissingImports]
        except Exception as e:  # pragma: no cover
            raise RuntimeError(
                "Starlette is required for mount_chat_sse(). Install with `process-gpt-agent-sdk[sse]`."
            ) from e

        async def handler(request):
            # chats 테이블 저장을 위해 DB 연결 필요
            initialize_db()
            try:
                body = await request.json()
            except Exception:
                body = {}

            req = ChatRequest(
                message=str(body.get("message") or body.get("text") or ""),
                tenant_id=str(body.get("tenant_id") or ""),
                user_uid=str(body.get("user_uid") or body.get("user_id") or ""),
                user_email=str(body.get("user_email") or ""),
                user_name=str(body.get("user_name") or ""),
                user_jwt=str(body.get("user_jwt") or ""),
                conversation_id=body.get("conversation_id"),
                file=body.get("file") if isinstance(body.get("file"), dict) else None,
                files=list(body.get("files") or []) if isinstance(body.get("files") or [], list) else [],
                file_count=int(body.get("file_count") or 0),
                stream=bool(body.get("stream") if body.get("stream") is not None else True),
                metadata=body.get("metadata") if isinstance(body.get("metadata"), dict) else {},
            )

            out_q: asyncio.Queue[Dict[str, Any]] = asyncio.Queue()
            streamer = ChatStreamer(out_q) if req.stream else None
            ctx = ChatRequestContext(req, streamer=streamer)
            q = ChatEventQueue(out_q, request=req, persist=persist_chat_to_db)

            # SSE contract: send initial metadata event
            await out_q.put({"event": "metadata", "data": {"conversation_id": ctx.context_id}})

            async def _run_executor():
                try:
                    await self.agent_executor.execute(ctx, q)
                    # 성공 종료는 Executor가 최종 Message를 enqueue하면(→ done/content) 그걸로 충분합니다.
                    # 최종 Message를 보내지 않는 Executor도 있을 수 있어, 그 경우에만 빈 done을 보냅니다.
                    if not getattr(q, "_sent_message", False):
                        await out_q.put({"event": "message", "data": {"type": "done"}})
                except Exception as ex:
                    await out_q.put(
                        {
                            "event": "message",
                            "data": {"type": "error", "error": f"{type(ex).__name__}: {str(ex)}"},
                        }
                    )

            asyncio.create_task(_run_executor())
            return StreamingResponse(drain_sse_queue(out_q), media_type="text/event-stream")

        # FastAPI: add_api_route, Starlette: add_route
        if hasattr(app, "add_api_route"):
            app.add_api_route(path, handler, methods=["POST"])
        elif hasattr(app, "add_route"):
            app.add_route(path, handler, methods=["POST"])
        else:  # pragma: no cover
            raise TypeError("Unsupported app type: expected FastAPI or Starlette-like app with add_route/add_api_route.")
