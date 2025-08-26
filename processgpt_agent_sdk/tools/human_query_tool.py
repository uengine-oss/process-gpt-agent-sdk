from __future__ import annotations

# =============================================================================
# Imports
# =============================================================================

import time
import uuid
from typing import Optional, List, Literal, Type, Dict, Any
from datetime import datetime, timezone

from pydantic import BaseModel, Field
from crewai.tools import BaseTool

from ..utils.context_manager import todo_id_var, proc_id_var, all_users_var
from ..utils.logger import write_log_message, handle_application_error
from ..core.database import fetch_human_response_sync, save_notification, initialize_db, get_db_client


# =============================================================================
# 입력 스키마
# =============================================================================
class HumanQuerySchema(BaseModel):
    """사용자 확인/추가정보 요청 입력 스키마."""

    role: str = Field(..., description="누구에게(역할 또는 대상)")
    text: str = Field(..., description="질의 내용")
    type: Literal["text", "select", "confirm"] = Field(
        default="text", description="질의 유형: 자유 텍스트, 선택형, 확인 여부"
    )
    options: Optional[List[str]] = Field(
        default=None, description="type이 select일 때 선택지 목록"
    )


# =============================================================================
# HumanQueryTool
# 설명: 보안/모호성 관련 질문을 사용자에게 전송하고 응답을 동기 대기
# =============================================================================
class HumanQueryTool(BaseTool):
    """사람에게 질문을 보내고 응답을 동기적으로 대기한다."""

    name: str = "human_asked"
    description: str = (
        "👀 질문은 반드시 '매우 구체적이고 세부적'으로 작성해야 합니다.\n"
        "- 목적, 대상, 범위/경계, 입력/출력 형식, 성공/실패 기준, 제약조건(보안/권한/시간/용량),\n"
        "  필요한 식별자/예시/반례까지 모두 명시하세요. 추측으로 진행하지 말고 누락 정보를 반드시 질문하세요.\n\n"
        "[1] 언제 사용해야 하나\n"
        "1. 보안에 민감한 정보(개인정보/인증정보/비밀키 등)를 다루거나 외부로 전송할 때\n"
        "2. 데이터베이스에 '저장/수정/삭제' 작업을 수행할 때 (읽기 전용 조회는 제외)\n"
        "3. 요구사항 및 작업지시사항이 모호·불완전·추정에 의존하거나, 전제조건/매개변수가 불명확할 때\n"
        "4. 외부 시스템 연동, 파일 생성/이동/삭제 등 시스템 상태를 바꾸는 작업일 때\n"
        "⛔ 위 조건에 해당하면 이 도구 없이 진행 금지\n\n"
        "[2] 응답 타입과 작성 방식 (항상 JSON으로 질의 전송)\n"
        "- 공통 형식: { role: <누구에게>, text: <질의>, type: <text|select|confirm>, options?: [선택지...] }\n"
        "- 질의 작성 가이드(반드시 포함): 5W1H, 목적/맥락, 선택 이유 또는 승인 근거, 기본값/제약,\n"
        "  입력/출력 형식과 예시, 반례/실패 시 처리, 보안/권한/감사 로그 요구사항, 마감/우선순위\n\n"
        "// 1) type='text' — 정보 수집(모호/불완전할 때 필수)\n"
        "{\n"
        '  "role": "user",\n'
        '  "text": "어떤 DB 테이블/스키마/키로 저장할까요? 입력값 예시/형식, 실패 시 처리, 보존 기간까지 구체히 알려주세요.",\n'
        '  "type": "text"\n'
        "}\n\n"
        "// 2) type='select' — 여러 옵션 중 선택(옵션은 상호배타적, 명확/완전하게 제시)\n"
        "{\n"
        '  "role": "system",\n'
        '  "text": "배포 환경을 선택하세요. 선택 근거(위험/롤백/감사 로그)를 함께 알려주세요.",\n'
        '  "type": "select",\n'
        '  "options": ["dev", "staging", "prod"]\n'
        "}\n\n"  
        "// 3) type='confirm' — 보안/DB 변경 등 민감 작업 승인(필수)\n"
        "{\n"
        '  "role": "user",\n'
        '  "text": "DB에서 주문 상태를 shipped로 업데이트합니다. 대상: order_id=..., 영향 범위: ...건, 롤백: ..., 진행 승인하시겠습니까?",\n'
        '  "type": "confirm"\n'
        "}\n\n"
        "타입 선택 규칙\n"
        "- text: 모호/누락 정보가 있을 때 먼저 세부사항을 수집 (여러 번 질문 가능)\n"
        "- select: 옵션이 둘 이상이면 반드시 options로 제시하고, 선택 기준을 text에 명시\n"
        "- confirm: DB 저장/수정/삭제, 외부 전송, 파일 조작 등은 승인 후에만 진행\n\n"
        "[3] 주의사항\n"
        "- 이 도구 없이 민감/변경 작업을 임의로 진행 금지.\n"
        "- select 타입은 반드시 'options'를 포함.\n"
        "- confirm 응답에 따라: ✅ 승인 → 즉시 수행 / ❌ 거절 → 즉시 중단(건너뛰기).\n"
        "- 애매하면 추가 질문을 반복하고, 충분히 구체화되기 전에는 실행하지 말 것.\n"
        "- 민감 정보는 최소한만 노출하고 필요 시 마스킹/요약.\n"
        "- 예시를 그대로 사용하지 말고 컨텍스트에 맞게 반드시 자연스러운 질의를 재작성하세요.\n"
        "- 타임아웃/미응답 시 '사용자 미응답 거절'을 반환하며, 후속 변경 작업을 중단하는 것이 안전.\n"
        "- 한 번에 하나의 주제만 질문(여러 주제면 질문을 분리). 한국어 존댓말 사용, 간결하되 상세하게."
    )
    args_schema: Type[HumanQuerySchema] = HumanQuerySchema

    # 선택적 컨텍스트(없어도 동작). ContextVar가 우선 사용됨
    _tenant_id: Optional[str] = None
    _user_id: Optional[str] = None
    _todo_id: Optional[int] = None
    _proc_inst_id: Optional[str] = None

    def __init__(
        self,
        tenant_id: Optional[str] = None,
        user_id: Optional[str] = None,
        todo_id: Optional[int] = None,
        proc_inst_id: Optional[str] = None,
        agent_name: Optional[str] = None,
        **kwargs,
    ):
        """도구 실행 컨텍스트(테넌트/유저/프로세스)를 옵션으로 설정."""
        super().__init__(**kwargs)
        self._tenant_id = tenant_id
        self._user_id = user_id
        self._todo_id = todo_id
        self._proc_inst_id = proc_inst_id
        self._agent_name = agent_name

    # =============================================================================
    # 동기 실행
    # 설명: 질문 이벤트를 저장하고 DB 폴링으로 응답을 기다린다
    # =============================================================================
    def _run(
        self, role: str, text: str, type: str = "text", options: Optional[List[str]] = None
    ) -> str:
        """질문 이벤트를 기록하고 최종 응답 문자열을 반환."""
        try:
            agent_name = getattr(self, "_agent_name", None)
            write_log_message(f"HumanQueryTool 실행: role={role}, agent_name={agent_name}, type={type}, options={options}")
            query_id = f"human_asked_{uuid.uuid4()}"

            payload: Dict[str, Any] = {
                "role": role,
                "text": text,
                "type": type,
                "options": options or [],
            }

            todo_id = todo_id_var.get() or self._todo_id
            proc_inst_id = proc_id_var.get() or self._proc_inst_id

            payload_with_status = {
                **payload,
                "status": "ASKED",
                "agent_profile": "/images/chat-icon.png"
            }
            
            initialize_db()
            supabase = get_db_client()
            record = {
                "id": str(uuid.uuid4()),
                "job_id": query_id,
                "todo_id": str(todo_id) if todo_id is not None else None,
                "proc_inst_id": str(proc_inst_id) if proc_inst_id is not None else None,
                "event_type": "human_asked",
                "crew_type": "action",
                "data": payload_with_status,
                "timestamp": datetime.now(timezone.utc).isoformat(),
            }
            supabase.table("events").insert(record).execute()

            try:
                tenant_id = self._tenant_id
                target_emails_csv = all_users_var.get() or ""
                if target_emails_csv and target_emails_csv.strip():
                    write_log_message(f"알림 저장 시도: target_emails_csv={target_emails_csv}, tenant_id={tenant_id}")
                    save_notification(
                        title=text,
                        notif_type="workitem_bpm",
                        description=agent_name,
                        user_ids_csv=target_emails_csv,
                        tenant_id=tenant_id,
                        url=f"/todolist/{todo_id}",
                        from_user_id=agent_name,
                    )
                else:
                    write_log_message("알림 저장 생략: 대상 이메일 없음")
            except Exception as e:
                handle_application_error("알림저장HumanTool", e, raise_error=False)

            answer = self._wait_for_response(query_id)
            return answer
        except Exception as e:
            handle_application_error("HumanQueryTool", e, raise_error=False)
            return "사용자 미응답 거절"

    # =============================================================================
    # 응답 대기
    # 설명: events 테이블에서 human_response를 폴링하여 응답을 가져온다
    # =============================================================================
    def _wait_for_response(
        self, job_id: str, timeout_sec: int = 180, poll_interval_sec: int = 5
    ) -> str:
        """DB 폴링으로 사람의 응답을 기다려 문자열로 반환."""
        deadline = time.time() + timeout_sec

        while time.time() < deadline:
            try:
                write_log_message(f"HumanQueryTool 응답 폴링: {job_id}")
                event = fetch_human_response_sync(job_id=job_id)
                if event:
                    write_log_message(f"HumanQueryTool 응답 수신: {event}")
                    data = event.get("data") or {}
                    answer = (data or {}).get("answer")
                    if isinstance(answer, str):
                        write_log_message("사람 응답 수신 완료")
                        return answer
                    return str(data)

            except Exception as e:
                write_log_message(f"인간 응답 대기 중... (오류: {str(e)[:100]})")
            time.sleep(poll_interval_sec)
        return "사용자 미응답 거절"

