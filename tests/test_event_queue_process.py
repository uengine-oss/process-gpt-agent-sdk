import asyncio
import unittest
from unittest.mock import patch

from a2a.helpers import (
    new_text_artifact_update_event,
    new_text_message,
    new_text_status_update_event,
)
from a2a.types import Role, TaskState

from processgpt_agent_sdk.event_queue_process import ProcessEventQueue


class TestProcessEventQueueRouting(unittest.IsolatedAsyncioTestCase):
    """A2A 타입 = 라우팅 키 (SDK는 dumb transport):

    - TaskStatusUpdateEvent: events 테이블 저장 (state/text 무관, 받은 대로)
    - TaskArtifactUpdateEvent: todolist 저장 (last_chunk → is_final)
    - Message: 채팅 전용. silently ignore.

    필터링은 Executor 책임. SDK는 받은 이벤트를 모두 라우팅한다.
    """

    async def test_status_update_working_is_persisted(self):
        # 토큰 진행 단위든 lifecycle이든 Executor가 emit한 status update는 모두 저장
        with patch(
            "processgpt_agent_sdk.event_queue_process.enqueue_ui_event_coalesced",
            return_value=None,
        ) as mock_enqueue:
            calls = []

            async def _fake(payload):
                calls.append(payload)

            mock_enqueue.side_effect = _fake

            q = ProcessEventQueue(todolist_id="t1", agent_orch="x", proc_inst_id="p1")

            evt = new_text_status_update_event(
                task_id="t1", context_id="p1",
                state=TaskState.TASK_STATE_WORKING,
                text="안녕",
            )
            await q.enqueue_event(evt)

            await asyncio.sleep(0.05)
            self.assertEqual(len(calls), 1, "WORKING status update도 저장되어야 함")
            # job_id 가 NOT NULL 이므로 SDK 가 task_id 로 채워야 함
            self.assertIsNotNone(calls[0]["job_id"], "job_id 는 None 이면 안 됨 (DB NOT NULL 제약)")
            self.assertEqual(calls[0]["job_id"], "t1", "metadata에 job_id 가 없으면 task_id 로 fallback")
            # WORKING 은 자동 매핑 대상이 아님 (sub-event 와의 의미 충돌 방지)
            self.assertIsNone(
                calls[0]["event_type"],
                "WORKING 은 자동 매핑 안 함. event_type 은 NULL 또는 metadata 로 명시",
            )

    async def test_submitted_state_auto_maps_to_task_started(self):
        with patch(
            "processgpt_agent_sdk.event_queue_process.enqueue_ui_event_coalesced",
            return_value=None,
        ) as mock_enqueue:
            calls = []

            async def _fake(payload):
                calls.append(payload)

            mock_enqueue.side_effect = _fake

            q = ProcessEventQueue(todolist_id="t1", agent_orch="x", proc_inst_id="p1")

            evt = new_text_status_update_event(
                task_id="t1", context_id="p1",
                state=TaskState.TASK_STATE_SUBMITTED,
                text="시작",
            )
            await q.enqueue_event(evt)

            await asyncio.sleep(0.05)
            self.assertEqual(len(calls), 1)
            self.assertEqual(calls[0]["event_type"], "task_started")

    async def test_status_update_completed_is_persisted(self):
        with patch(
            "processgpt_agent_sdk.event_queue_process.enqueue_ui_event_coalesced",
            return_value=None,
        ) as mock_enqueue:
            calls = []

            async def _fake(payload):
                calls.append(payload)

            mock_enqueue.side_effect = _fake

            q = ProcessEventQueue(todolist_id="t1", agent_orch="x", proc_inst_id="p1")

            evt = new_text_status_update_event(
                task_id="t1", context_id="p1",
                state=TaskState.TASK_STATE_COMPLETED,
                text="안녕하세요",
            )
            await q.enqueue_event(evt)

            await asyncio.sleep(0.05)
            self.assertEqual(len(calls), 1)
            self.assertEqual(calls[0]["event_type"], "task_completed", "COMPLETED 는 자동 매핑")

    async def test_failed_state_auto_maps_to_error(self):
        with patch(
            "processgpt_agent_sdk.event_queue_process.enqueue_ui_event_coalesced",
            return_value=None,
        ) as mock_enqueue:
            calls = []

            async def _fake(payload):
                calls.append(payload)

            mock_enqueue.side_effect = _fake

            q = ProcessEventQueue(todolist_id="t1", agent_orch="x", proc_inst_id="p1")

            evt = new_text_status_update_event(
                task_id="t1", context_id="p1",
                state=TaskState.TASK_STATE_FAILED,
                text="boom",
            )
            await q.enqueue_event(evt)

            await asyncio.sleep(0.05)
            self.assertEqual(len(calls), 1)
            self.assertEqual(calls[0]["event_type"], "error")

    async def test_metadata_event_type_overrides_state_mapping(self):
        # metadata 가 명시되면 자동 매핑보다 우선 (explicit > implicit)
        with patch(
            "processgpt_agent_sdk.event_queue_process.enqueue_ui_event_coalesced",
            return_value=None,
        ) as mock_enqueue:
            calls = []

            async def _fake(payload):
                calls.append(payload)

            mock_enqueue.side_effect = _fake

            q = ProcessEventQueue(todolist_id="t1", agent_orch="x", proc_inst_id="p1")

            evt = new_text_status_update_event(
                task_id="t1", context_id="p1",
                state=TaskState.TASK_STATE_WORKING,
                text='{"tool": "search"}',
            )
            evt.metadata.update({"event_type": "tool_usage_started"})
            await q.enqueue_event(evt)

            await asyncio.sleep(0.05)
            self.assertEqual(len(calls), 1)
            self.assertEqual(calls[0]["event_type"], "tool_usage_started")

    async def test_human_input_required_status_is_persisted_as_human_asked(self):
        with patch(
            "processgpt_agent_sdk.event_queue_process.enqueue_ui_event_coalesced",
            return_value=None,
        ) as mock_enqueue:
            calls = []

            async def _fake(payload):
                calls.append(payload)

            mock_enqueue.side_effect = _fake

            q = ProcessEventQueue(todolist_id="t1", agent_orch="x", proc_inst_id="p1")

            evt = new_text_status_update_event(
                task_id="t1", context_id="p1",
                state=TaskState.TASK_STATE_INPUT_REQUIRED,
                text='{"q": "qty?"}',
            )
            await q.enqueue_event(evt)

            await asyncio.sleep(0.05)
            self.assertEqual(len(calls), 1)
            self.assertEqual(calls[0]["event_type"], "human_asked")

    async def test_artifact_event_is_persisted_with_is_final_flag(self):
        with patch(
            "processgpt_agent_sdk.event_queue_process.save_task_result",
            return_value=None,
        ) as mock_save:
            calls = []

            async def _fake(*args, **kwargs):
                calls.append((args, kwargs))

            mock_save.side_effect = _fake

            q = ProcessEventQueue(todolist_id="t1", agent_orch="x", proc_inst_id="p1")

            evt = new_text_artifact_update_event(
                task_id="t1", context_id="p1",
                name="assistant_response",
                text="최종결과",
                last_chunk=True,
            )
            await q.enqueue_event(evt)

            await asyncio.sleep(0.05)
            self.assertEqual(len(calls), 1)
            args, kwargs = calls[0]
            # save_task_result(todolist_id, content, is_final)
            self.assertEqual(args[0], "t1")
            self.assertEqual(args[1], "최종결과")
            self.assertEqual(args[2], True)

    async def test_final_artifact_auto_emits_crew_completed(self):
        # last_chunk=True artifact 가 처리되면 todolist 저장 + crew_completed 자동 발행
        with patch(
            "processgpt_agent_sdk.event_queue_process.save_task_result",
            return_value=None,
        ) as mock_save, patch(
            "processgpt_agent_sdk.event_queue_process.enqueue_ui_event_coalesced",
            return_value=None,
        ) as mock_enqueue:
            save_calls = []
            enqueue_calls = []

            async def _fake_save(*args, **kwargs):
                save_calls.append((args, kwargs))

            async def _fake_enqueue(payload):
                enqueue_calls.append(payload)

            mock_save.side_effect = _fake_save
            mock_enqueue.side_effect = _fake_enqueue

            q = ProcessEventQueue(todolist_id="t1", agent_orch="x", proc_inst_id="p1")

            evt = new_text_artifact_update_event(
                task_id="t1", context_id="p1",
                name="assistant_response",
                text="최종결과",
                last_chunk=True,
            )
            await q.enqueue_event(evt)

            await asyncio.sleep(0.05)
            self.assertEqual(len(save_calls), 1, "todolist 저장 1회")
            self.assertEqual(len(enqueue_calls), 1, "crew_completed 자동 발행 1회")
            self.assertEqual(enqueue_calls[0]["event_type"], "crew_completed")

    async def test_non_final_artifact_does_not_emit_crew_completed(self):
        # last_chunk=False 면 crew_completed 발행하지 않음
        with patch(
            "processgpt_agent_sdk.event_queue_process.save_task_result",
            return_value=None,
        ) as mock_save, patch(
            "processgpt_agent_sdk.event_queue_process.enqueue_ui_event_coalesced",
            return_value=None,
        ) as mock_enqueue:
            save_calls = []
            enqueue_calls = []

            async def _fake_save(*args, **kwargs):
                save_calls.append((args, kwargs))

            async def _fake_enqueue(payload):
                enqueue_calls.append(payload)

            mock_save.side_effect = _fake_save
            mock_enqueue.side_effect = _fake_enqueue

            q = ProcessEventQueue(todolist_id="t1", agent_orch="x", proc_inst_id="p1")

            evt = new_text_artifact_update_event(
                task_id="t1", context_id="p1",
                name="assistant_response",
                text="중간청크",
                last_chunk=False,
            )
            await q.enqueue_event(evt)

            await asyncio.sleep(0.05)
            self.assertEqual(len(save_calls), 1)
            self.assertEqual(enqueue_calls, [], "중간 청크는 crew_completed 발행 안 함")

    async def test_crew_completed_is_idempotent(self):
        # last_chunk=True artifact + framework 의 task_done() 둘 다 호출돼도 1회만 발행
        with patch(
            "processgpt_agent_sdk.event_queue_process.save_task_result",
            return_value=None,
        ) as mock_save, patch(
            "processgpt_agent_sdk.event_queue_process.enqueue_ui_event_coalesced",
            return_value=None,
        ) as mock_enqueue:
            enqueue_calls = []

            async def _fake_save(*args, **kwargs):
                pass

            async def _fake_enqueue(payload):
                enqueue_calls.append(payload)

            mock_save.side_effect = _fake_save
            mock_enqueue.side_effect = _fake_enqueue

            q = ProcessEventQueue(todolist_id="t1", agent_orch="x", proc_inst_id="p1")

            evt = new_text_artifact_update_event(
                task_id="t1", context_id="p1",
                name="assistant_response",
                text="결과",
                last_chunk=True,
            )
            await q.enqueue_event(evt)
            q.task_done()  # framework 의 안전망 호출

            await asyncio.sleep(0.05)
            crew_completed = [p for p in enqueue_calls if p.get("event_type") == "crew_completed"]
            self.assertEqual(len(crew_completed), 1, "두 진입점에서 호출돼도 1회만 발행")

    async def test_task_done_emits_crew_completed_when_no_final_artifact(self):
        # Executor 가 final artifact 를 emit 하지 않은 경우, framework 의 task_done() 이 발행
        with patch(
            "processgpt_agent_sdk.event_queue_process.enqueue_ui_event_coalesced",
            return_value=None,
        ) as mock_enqueue:
            enqueue_calls = []

            async def _fake_enqueue(payload):
                enqueue_calls.append(payload)

            mock_enqueue.side_effect = _fake_enqueue

            q = ProcessEventQueue(todolist_id="t1", agent_orch="x", proc_inst_id="p1")
            q.task_done()

            await asyncio.sleep(0.05)
            self.assertEqual(len(enqueue_calls), 1)
            self.assertEqual(enqueue_calls[0]["event_type"], "crew_completed")

    async def test_message_is_silently_ignored(self):
        # Message 는 채팅 전용이므로 프로세스 큐에서 무시
        with patch(
            "processgpt_agent_sdk.event_queue_process.enqueue_ui_event_coalesced",
            return_value=None,
        ) as mock_enqueue, patch(
            "processgpt_agent_sdk.event_queue_process.save_task_result",
            return_value=None,
        ) as mock_save:
            enqueue_calls = []
            save_calls = []

            async def _fake_enqueue(payload):
                enqueue_calls.append(payload)

            async def _fake_save(*args, **kwargs):
                save_calls.append((args, kwargs))

            mock_enqueue.side_effect = _fake_enqueue
            mock_save.side_effect = _fake_save

            q = ProcessEventQueue(todolist_id="t1", agent_orch="x", proc_inst_id="p1")

            await q.enqueue_event(new_text_message("hi", role=Role.ROLE_AGENT))

            await asyncio.sleep(0.05)
            self.assertEqual(enqueue_calls, [])
            self.assertEqual(save_calls, [])


if __name__ == "__main__":
    unittest.main()


class TestProcessEventQueueHumanAsked(unittest.IsolatedAsyncioTestCase):
    """사람에게 물어 놓고 끝난 실행은 완료가 아니다.

    deepagents 는 질문할 때 INPUT_REQUIRED 상태 뒤에 질문 본문을 last_chunk
    아티팩트로 보낸다. 이것을 결과로 저장하면 COMPLETE 모드에서는 질문이
    산출물로 SUBMITTED 되어 프로세스가 다음 단계로 넘어간다.
    """

    def setUp(self):
        self.saves, self.marks, self.events = [], [], []

        async def _save(*args, **kwargs):
            self.saves.append(args)

        async def _mark(todo_id):
            self.marks.append(todo_id)

        async def _enqueue(payload):
            self.events.append(payload)

        for name, fake in (
            ("save_task_result", _save),
            ("mark_task_human_asked", _mark),
            ("enqueue_ui_event_coalesced", _enqueue),
        ):
            p = patch(f"processgpt_agent_sdk.event_queue_process.{name}", side_effect=fake)
            p.start()
            self.addCleanup(p.stop)

        self.q = ProcessEventQueue(todolist_id="t1", agent_orch="x", proc_inst_id="p1")

    async def _status(self, state):
        await self.q.enqueue_event(
            new_text_status_update_event(task_id="t1", context_id="p1", state=state, text="{}")
        )

    async def _final(self, text):
        await self.q.enqueue_event(
            new_text_artifact_update_event(
                task_id="t1", context_id="p1", name="assistant_response", text=text, last_chunk=True
            )
        )

    def _crew_completed(self):
        return [e for e in self.events if e.get("event_type") == "crew_completed"]

    async def test_question_artifact_is_not_saved_as_result(self):
        await self._status(TaskState.TASK_STATE_INPUT_REQUIRED)
        await self._final("결재 금액 상한이 얼마입니까?")
        self.q.task_done()
        await asyncio.sleep(0.05)

        self.assertEqual(self.saves, [], "질문은 결과로 저장하지 않는다")
        self.assertEqual(self.marks, ["t1"], "사람 답변 대기로 1회 표시")
        self.assertEqual(self._crew_completed(), [], "끝난 게 아니므로 crew_completed 없음")
        self.assertIn("human_asked", [e.get("event_type") for e in self.events], "질문 이벤트는 남는다")

    async def test_question_without_artifact_still_waits(self):
        # cli-agent 처럼 상태만 내고 끝나는 실행도 STARTED 로 남지 않아야 한다.
        await self._status(TaskState.TASK_STATE_INPUT_REQUIRED)
        self.q.task_done()
        await asyncio.sleep(0.05)

        self.assertEqual(self.marks, ["t1"])
        self.assertEqual(self._crew_completed(), [])

    async def test_answered_in_same_run_then_final_is_result(self):
        # 같은 실행 안에서 답을 받아 이어 갔으면 마지막 아티팩트는 진짜 결과다.
        await self._status(TaskState.TASK_STATE_INPUT_REQUIRED)
        await self._status(TaskState.TASK_STATE_WORKING)
        await self._final("최종결과")
        self.q.task_done()
        await asyncio.sleep(0.05)

        self.assertEqual(self.marks, [])
        self.assertEqual([a[1:] for a in self.saves], [("최종결과", True)])
        self.assertEqual(len(self._crew_completed()), 1)

    async def test_progress_chunk_while_waiting_is_still_saved(self):
        await self._status(TaskState.TASK_STATE_INPUT_REQUIRED)
        await self.q.enqueue_event(
            new_text_artifact_update_event(
                task_id="t1", context_id="p1", name="assistant_response", text="중간", last_chunk=False
            )
        )
        await asyncio.sleep(0.05)
        self.assertEqual([a[1:] for a in self.saves], [("중간", False)])
