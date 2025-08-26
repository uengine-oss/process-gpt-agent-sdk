import asyncio
from typing import Any, Dict

from a2a.server.agent_execution import AgentExecutor, RequestContext
from a2a.server.events import EventQueue, Event

from .core.database import (
	fetch_human_users_by_proc_inst_id,
	initialize_db,
	get_consumer_id,
	polling_pending_todos,
	fetch_done_data,
	fetch_agent_data,
	fetch_form_types,
	fetch_task_status,
	fetch_tenant_mcp_config,
	update_task_error,
)

from .utils.logger import handle_application_error, write_log_message
from .utils.summarizer import summarize_async
from .utils.event_handler import process_event_message
from .utils.context_manager import set_context, reset_context


class ProcessGPTAgentServer:
	"""ProcessGPT 핵심 서버

	- 단일 실행기 모델: 실행기는 하나이며, 작업별 분기는 실행기 내부 로직에 위임합니다.
	- 폴링은 타입 필터 없이(빈 값) 가져온 뒤, 작업 레코드의 정보로 처리합니다.
	"""

	def __init__(self, executor: AgentExecutor, polling_interval: int = 5, agent_orch: str = ""):
		self.polling_interval = polling_interval
		self.is_running = False
		self._executor: AgentExecutor = executor
		self.cancel_check_interval: float = 0.5
		self.agent_orch: str = agent_orch or ""
		initialize_db()

	async def run(self) -> None:
		self.is_running = True
		write_log_message("ProcessGPT 서버 시작")
		
		while self.is_running:
			try:
				task_record = await polling_pending_todos(self.agent_orch, get_consumer_id())
				if not task_record:
					await asyncio.sleep(self.polling_interval)
					continue

				task_id = task_record["id"]
				write_log_message(f"[JOB START] task_id={task_id}")

				try:
					prepared_data = await self._prepare_service_data(task_record)
					write_log_message(f"[RUN] 서비스 데이터 준비 완료 [task_id={task_id} agent={prepared_data.get('agent_orch','')}]")

					await self._execute_with_cancel_watch(task_record, prepared_data)
					write_log_message(f"[RUN] 서비스 실행 완료 [task_id={task_id} agent={prepared_data.get('agent_orch','')}]")
				except Exception as job_err:
					handle_application_error("작업 처리 오류", job_err, raise_error=False)
					try:
						await update_task_error(str(task_id))
					except Exception as upd_err:
						handle_application_error("FAILED 상태 업데이트 실패", upd_err, raise_error=False)
					# 다음 루프로 진행
					continue
				
			except Exception as e:
				handle_application_error("폴링 루프 오류", e, raise_error=False)
				await asyncio.sleep(self.polling_interval)

	def stop(self) -> None:
		self.is_running = False
		write_log_message("ProcessGPT 서버 중지")

	async def _prepare_service_data(self, task_record: Dict[str, Any]) -> Dict[str, Any]:
		done_outputs = await fetch_done_data(task_record.get("proc_inst_id"))
		write_log_message(f"[PREP] done_outputs → {done_outputs}")
		feedbacks = task_record.get("feedback")

		agent_list = await fetch_agent_data(str(task_record.get("user_id", "")))
		write_log_message(f"[PREP] agent_list → {agent_list}")

		mcp_config = await fetch_tenant_mcp_config(str(task_record.get("tenant_id", "")))
		write_log_message(f"[PREP] mcp_config(툴) → {mcp_config}")

		form_id, form_types, form_html = await fetch_form_types(
			str(task_record.get("tool", "")),
			str(task_record.get("tenant_id", ""))
		)
		write_log_message(f"[PREP] form → id={form_id} types={form_types}")

		output_summary, feedback_summary = await summarize_async(
			done_outputs or [], feedbacks or "", task_record.get("description", "")
		)
		write_log_message(f"[PREP] summary → output={output_summary} feedback={feedback_summary}")


		all_users = await fetch_human_users_by_proc_inst_id(task_record.get("proc_inst_id"))
		write_log_message(f"[PREP] all_users → {all_users}")

		prepared: Dict[str, Any] = {
			"task_id": str(task_record.get("id")),
			"proc_inst_id": task_record.get("proc_inst_id"),
			"agent_list": agent_list or [],
			"mcp_config": mcp_config,
			"form_id": form_id,
			"form_types": form_types or [],
			"form_html": form_html or "",
			"activity_name": str(task_record.get("activity_name", "")),
			"message": str(task_record.get("description", "")),
			"agent_orch": str(task_record.get("agent_orch", "")),
			"done_outputs": done_outputs or [],
			"output_summary": output_summary or "",
			"feedback_summary": feedback_summary or "",
			"all_users": all_users or "",
		}

		return prepared

	async def _execute_with_cancel_watch(self, task_record: Dict[str, Any], prepared_data: Dict[str, Any]) -> None:
		executor = self._executor

		context = ProcessGPTRequestContext(prepared_data)
		event_queue = ProcessGPTEventQueue(task_record)

		# 실행 전 컨텍스트 변수 설정 (CrewAI 전역 리스너 등에서 활용)
		try:
			set_context(
				todo_id=str(task_record.get("id")),
				proc_inst_id=str(task_record.get("proc_inst_id") or ""),
				crew_type=str(prepared_data.get("agent_orch") or ""),
				form_id=str(prepared_data.get("form_id") or ""),
				all_users=str(prepared_data.get("all_users") or ""),
			)
		except Exception as e:
			handle_application_error("컨텍스트 설정 실패", e, raise_error=False)

		write_log_message(f"[EXEC START] task_id={task_record.get('id')} agent={prepared_data.get('agent_orch','')}")
		execute_task = asyncio.create_task(executor.execute(context, event_queue))
		cancel_watch_task = asyncio.create_task(self._watch_cancellation(task_record, executor, context, event_queue, execute_task))
		
		try:
			done, pending = await asyncio.wait(
				[cancel_watch_task, execute_task], 
				return_when=asyncio.FIRST_COMPLETED
			)
			for task in pending:
				task.cancel()
				
		except Exception as e:
			handle_application_error("서비스 실행 오류", e, raise_error=False)
			cancel_watch_task.cancel()
			execute_task.cancel()
		finally:
			# 컨텍스트 정리
			try:
				reset_context()
			except Exception as e:
				handle_application_error("컨텍스트 리셋 실패", e, raise_error=False)
			try:
				await event_queue.close()
			except Exception as e:
				handle_application_error("이벤트 큐 종료 실패", e, raise_error=False)
			write_log_message(f"[EXEC END] task_id={task_record.get('id')} agent={prepared_data.get('agent_orch','')}")

	async def _watch_cancellation(self, task_record: Dict[str, Any], executor: AgentExecutor, context: RequestContext, event_queue: EventQueue, execute_task: asyncio.Task) -> None:
		todo_id = str(task_record.get("id"))
		
		while True:
			await asyncio.sleep(self.cancel_check_interval)
			
			status = await fetch_task_status(todo_id)
			normalized = (status or "").strip().lower()
			if normalized in ("cancelled", "fb_requested"):
				write_log_message(f"작업 취소 감지: {todo_id}, 상태: {status}")
				
				try:
					await executor.cancel(context, event_queue)
				except Exception as e:
					handle_application_error("취소 처리 실패", e, raise_error=False)
				finally:
					try:
						execute_task.cancel()
					except Exception as e:
						handle_application_error("실행 태스크 즉시 취소 실패", e, raise_error=False)
					try:
						await event_queue.close()
					except Exception as e:
						handle_application_error("취소 후 이벤트 큐 종료 실패", e, raise_error=False)
				break


class ProcessGPTRequestContext(RequestContext):
	def __init__(self, prepared_data: Dict[str, Any]):
		self._prepared_data = prepared_data
		self._message = prepared_data.get("message", "")
		self._current_task = None

	def get_user_input(self) -> str:
		return self._message

	@property
	def message(self) -> str:
		return self._message

	@property
	def current_task(self):
		return getattr(self, "_current_task", None)

	def get_context_data(self) -> Dict[str, Any]:
		return self._prepared_data


class ProcessGPTEventQueue(EventQueue):
	def __init__(self, task_record: Dict[str, Any]):
		self.todo = task_record
		super().__init__()

	def enqueue_event(self, event: Event):
		try:
			try:
				super().enqueue_event(event)
			except Exception as e:
				handle_application_error("이벤트 큐 삽입 실패", e, raise_error=False)

			self._create_bg_task(process_event_message(self.todo, event), "process_event_message")
		except Exception as e:
			handle_application_error("이벤트 저장 실패", e, raise_error=False)
		
	def task_done(self) -> None:
		try:
			write_log_message(f"태스크 완료: {self.todo['id']}")
		except Exception as e:
			handle_application_error("태스크 완료 처리 실패", e, raise_error=False)

	async def close(self) -> None:
		pass

	def _create_bg_task(self, coro: Any, label: str) -> None:
		try:
			task = asyncio.create_task(coro)
			def _cb(t: asyncio.Task):
				exc = t.exception()
				if exc:
					handle_application_error(f"백그라운드 태스크 오류({label})", exc, raise_error=False)
			task.add_done_callback(_cb)
		except Exception as e:
			handle_application_error(f"백그라운드 태스크 생성 실패({label})", e, raise_error=False)
