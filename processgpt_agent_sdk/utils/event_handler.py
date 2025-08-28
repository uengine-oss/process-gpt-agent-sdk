from __future__ import annotations

from typing import Any, Dict
import uuid

from a2a.server.events import Event
from .logger import handle_application_error, write_log_message
from ..core.database import record_event, save_task_result
from ..tools.safe_tool_loader import SafeToolLoader


# =============================================================================
# 이벤트 변환: Event 또는 dict를 표준 dict로 통일
# =============================================================================

def convert_event_to_dictionary(event: Event) -> Dict[str, Any]:
	"""Event/dict를 표준 dict로 변환한다."""
	try:
		# 이미 dict로 전달된 경우 그대로 사용
		if isinstance(event, dict):
			return event
		# Event 객체면 공개 필드만 추출
		if hasattr(event, "__dict__"):
			return {k: v for k, v in event.__dict__.items() if not k.startswith("_")}
		# 알 수 없는 타입은 문자열로 보존
		return {"type": "event", "data": str(event)}
	except Exception as e:
		handle_application_error("event dict 변환 실패", e, raise_error=False)
		return {"type": "event", "data": str(event)}


# =============================================================================
# 이벤트 처리: type에 따라 저장 위치 분기
# =============================================================================

async def process_event_message(todo: Dict[str, Any], event: Event) -> None:
	"""이벤트 타입별로 todolist/events에 저장하거나 리소스 정리."""
	try:
		data = convert_event_to_dictionary(event)
		evt_type = str(data.get("type") or data.get("event_type") or "").lower()

		# done: 종료 이벤트 → 기록 후 MCP 정리
		if evt_type == "done":
			payload = data.get("data") or {}
			if isinstance(payload, dict) and "id" not in payload:
				payload["id"] = str(uuid.uuid4())
			await record_event(payload)
			try:
				SafeToolLoader.shutdown_all_adapters()
				write_log_message("MCP 리소스 정리 완료")
			except Exception as ce:
				handle_application_error("MCP 리소스 정리 실패", ce, raise_error=False)
			return

		# output: 결과 저장만 수행
		if evt_type == "output":
			payload = data.get("data") or {}
			is_final = bool(payload.get("final") or payload.get("is_final")) if isinstance(payload, dict) else False
			content = payload.get("content") or payload.get("data") if isinstance(payload, dict) else payload
			await save_task_result(str(todo.get("id")), content, final=is_final)
			return
		
		# event : 일반 이벤트 저장 (워커 데이터 그대로 보존)
		if evt_type == "event":
			payload = data.get("data") or {}
			if isinstance(payload, dict) and "id" not in payload:
				payload["id"] = str(uuid.uuid4())
			await record_event(payload)
			return

	except Exception as e:
		handle_application_error("process_event_message 처리 실패", e, raise_error=False)
