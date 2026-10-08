"""실행이 종결 상태를 남기지 않고 반환해도 작업이 STARTED 고아로 남지 않는다.

Executor 가 실패 상태(FAILED)만 보내고 예외 없이 반환하거나, 결과 없이 반환하면
행은 STARTED 인 채 점유만 풀려 아무도 다시 집지 않았다(cli-agent 의 출력 형식
불일치 경로에서 실제로 그랬다). 그리고 그 실행에 crew_completed 가 남아 화면은
완료로 보였다.
"""

import asyncio
import os
import unittest
from unittest.mock import patch

from a2a.helpers import new_text_artifact_update_event, new_text_status_update_event
from a2a.types import TaskState

from processgpt_agent_sdk import event_queue_process as eqp
from processgpt_agent_sdk.event_queue_process import ProcessEventQueue

TODO = "33333333-3333-3333-3333-333333333333"


def _failed(text="출력 형식이 맞지 않습니다"):
    return new_text_status_update_event(
        task_id=TODO, context_id="p1", state=TaskState.TASK_STATE_FAILED, text=text,
    )


def _final(text="결과"):
    return new_text_artifact_update_event(
        task_id=TODO, context_id="p1", name="result", text=text, last_chunk=True,
    )


class _Sink:
    """큐가 내보내는 DB 쓰기를 기록한다."""

    def __init__(self, save_delay=0.0):
        self.events = []
        self.saved = []
        self.save_delay = save_delay

    async def enqueue(self, payload):
        self.events.append(payload)

    async def save(self, todo_id, content, final):
        await asyncio.sleep(self.save_delay)
        self.saved.append((todo_id, final))

    async def human(self, todo_id):
        pass

    def patches(self):
        return (
            patch.object(eqp, "enqueue_ui_event_coalesced", self.enqueue),
            patch.object(eqp, "save_task_result", self.save),
            patch.object(eqp, "mark_task_human_asked", self.human),
        )

    def event_types(self):
        return [e.get("event_type") for e in self.events]


class QueueFailureTrackingTest(unittest.IsolatedAsyncioTestCase):
    async def _run(self, events, sink=None):
        sink = sink or _Sink()
        p1, p2, p3 = sink.patches()
        with p1, p2, p3:
            q = ProcessEventQueue(TODO, "agent", "p1")
            for e in events:
                await q.enqueue_event(e)
            q.task_done()
            await q.drain()
            await asyncio.sleep(0.01)
        return q, sink

    async def test_failure_without_result_is_not_a_completion(self):
        q, sink = await self._run([_failed()])
        self.assertTrue(q.ended_in_failure)
        self.assertIn("error", sink.event_types())
        self.assertNotIn("crew_completed", sink.event_types(), "실패한 실행을 완료로 표시하면 안 된다")

    async def test_result_after_failure_report_is_a_completion(self):
        """실패를 보고했다가 이어서 결과를 냈으면 그 결과가 최종이다."""
        q, sink = await self._run([_failed(), _final()])
        self.assertFalse(q.ended_in_failure)
        self.assertEqual(sink.saved, [(TODO, True)])
        self.assertIn("crew_completed", sink.event_types())

    async def test_normal_result_is_unchanged(self):
        q, sink = await self._run([_final()])
        self.assertFalse(q.ended_in_failure)
        self.assertIn("crew_completed", sink.event_types())

    async def test_drain_waits_for_the_result_to_be_saved(self):
        """drain 이 돌아오면 결과 저장이 끝나 있다 — 그 뒤의 행 판정이 오판하지 않는다."""
        sink = _Sink(save_delay=0.3)
        p1, p2, p3 = sink.patches()
        with p1, p2, p3:
            q = ProcessEventQueue(TODO, "agent", "p1")
            await q.enqueue_event(_final())
            self.assertEqual(sink.saved, [])
            await q.drain()
            self.assertEqual(sink.saved, [(TODO, True)])


class _ScriptedExecutor:
    def __init__(self, events):
        self.events = events

    async def execute(self, _context, queue):
        for e in self.events:
            await queue.enqueue_event(e)

    async def cancel(self, _context, _queue):
        pass


class FrameworkClosesUnfinishedExecutionTest(unittest.TestCase):
    """process_todolist_item 이 실행 뒤 STARTED 로 남은 내 작업을 종결한다."""

    def _run(self, events, *, still_started=True, save_delay=0.0):
        from processgpt_agent_sdk import processgpt_agent_framework as fw

        server = fw.ProcessGPTAgentServer(_ScriptedExecutor(events), "agent", tenant_auth=False)
        row = {"id": TODO, "tenant_id": "t", "agent_mode": "DRAFT", "proc_inst_id": "p1"}
        sink = _Sink(save_delay=save_delay)
        calls = {"fail": [], "error_events": [], "update_task_error": []}

        async def fail_if_started(todo_id, consumer):
            calls["fail"].append((todo_id, consumer, list(sink.saved)))
            return still_started

        async def record_event(payload):
            calls["error_events"].append(payload)

        async def update_task_error(todo_id):
            calls["update_task_error"].append(todo_id)

        async def release(*_a):
            return True

        async def prepare(_self):
            return None

        p1, p2, p3 = sink.patches()
        with p1, p2, p3, \
             patch.object(fw, "fail_task_if_still_started", fail_if_started), \
             patch.object(fw, "record_event", record_event), \
             patch.object(fw, "update_task_error", update_task_error), \
             patch.object(fw, "release_task_lease", release), \
             patch.object(fw, "renew_task_lease_sync", lambda *a: {"renewed": True, "reason": "ok"}), \
             patch.object(fw.ProcessGPTRequestContext, "prepare_context", prepare), \
             patch.object(fw, "get_consumer_id", lambda: "w1"), \
             patch.dict(os.environ, {"TASK_LEASE_SECONDS": "20"}):

            async def main():
                await server.process_todolist_item(row)
                await asyncio.sleep(0.05)  # 오류 이벤트 기록 태스크

            asyncio.run(main())
        return calls, sink

    def test_failure_report_without_result_ends_the_task_as_failed(self):
        calls, sink = self._run([_failed()])
        self.assertEqual([c[:2] for c in calls["fail"]], [(TODO, "w1")])
        # 실행기가 이미 실패 이벤트를 남겼다. 프레임워크가 하나 더 남기지 않는다.
        self.assertEqual(calls["error_events"], [])
        self.assertNotIn("crew_completed", sink.event_types())
        # 예외 경로가 아니다.
        self.assertEqual(calls["update_task_error"], [])

    def test_silent_return_ends_the_task_as_failed_and_says_why(self):
        calls, _sink = self._run([])
        self.assertEqual(len(calls["fail"]), 1)
        self.assertEqual(len(calls["error_events"]), 1)
        self.assertEqual(calls["error_events"][0]["event_type"], "error")
        self.assertIn("ExecutorReturnedWithoutResult", calls["error_events"][0]["data"]["raw_error"])

    def test_closing_is_judged_after_the_result_is_saved(self):
        """결과 저장이 끝난 뒤에 판정한다. 그 전에 보면 COMPLETED 될 작업을 고아로 오판한다."""
        calls, _sink = self._run([_final()], still_started=False, save_delay=0.3)
        self.assertEqual(len(calls["fail"]), 1)
        _todo, _consumer, saved_at_check = calls["fail"][0]
        self.assertEqual(saved_at_check, [(TODO, True)])
        self.assertEqual(calls["error_events"], [])

    def test_completed_task_is_left_alone(self):
        """종결 RPC 가 바꾼 것이 없으면(이미 COMPLETED) 아무 이벤트도 더하지 않는다."""
        calls, sink = self._run([_final()], still_started=False)
        self.assertEqual(calls["error_events"], [])
        self.assertIn("crew_completed", sink.event_types())


if __name__ == "__main__":
    unittest.main()
