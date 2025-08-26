from __future__ import annotations

import json
import uuid
from datetime import datetime, timezone
from typing import Any, Dict

from a2a.server.events import Event
from .logger import handle_application_error, write_log_message
from ..core.database import record_event, save_task_result
from ..tools.safe_tool_loader import SafeToolLoader


def convert_event_to_dictionary(event: Event) -> Dict[str, Any]:
	try:
		if hasattr(event, "__dict__"):
			return {k: v for k, v in event.__dict__.items() if not k.startswith("_")}
		return {"event": str(event)}
	except Exception as e:
		handle_application_error("event dict 변환 실패", e, raise_error=False)
		return {"event": str(event)}


async def process_event_message(todo: Dict[str, Any], event: Event) -> None:
	"""이벤트(dict)와 출력(output)을 구분해 처리.

	- "event": 일반 이벤트 → events 테이블 저장
	- "output": 실행 결과 → save_task_result로 중간/최종 여부에 따라 저장
	- "done": 실행 종료 신호 → 이벤트 기록 후 MCP 리소스 정리
	"""
	try:
		data = convert_event_to_dictionary(event)
		evt_type = str(data.get("type") or data.get("event_type") or "").lower()

		# done: 종료 이벤트 → 기록 후 MCP 정리
		if evt_type == "done":
			normalized = {
				"id": str(uuid.uuid4()),
				"timestamp": datetime.now(timezone.utc).isoformat(),
				**data,
			}
			await record_event(todo, normalized, event_type="done")
			try:
				SafeToolLoader.shutdown_all_adapters()
				write_log_message("MCP 리소스 정리 완료")
			except Exception as ce:
				handle_application_error("MCP 리소스 정리 실패", ce, raise_error=False)
			return

		# output: 결과 저장만 수행
		if evt_type == "output":
			payload = data.get("data") or data.get("payload") or {}
			is_final = bool(payload.get("final") or payload.get("is_final"))
			content = payload.get("content") if isinstance(payload, dict) else payload
			await save_task_result(str(todo.get("id")), content, final=is_final)
			return

		# event: 일반 이벤트 저장
		normalized = {
			"id": str(uuid.uuid4()),
			"timestamp": datetime.now(timezone.utc).isoformat(),
			**data,
		}
		await record_event(todo, normalized, event_type="event")
	except Exception as e:
		handle_application_error("process_event_message 처리 실패", e, raise_error=False)
