"""워커가 종료 신호로 실행 도중 멈추면, 그 작업은 다른 워커가 재점유할 수 있어야 한다.

스펙: infra/process-gpt/openspec/changes/workitem-resume-default
      specs/agent-sdk_workitem-resume-signal  RS-2.3

2026-10-07 kind e2e 에서 찾았다. 파드를 지우면(롤링 배포·스케일 다운과 같은 경로)
서버가 SIGTERM 을 받아 폴링 태스크를 취소한다. 실행 중이던 작업은 사용자 취소와 같은
경로로 끝나 finally 에서 점유를 해제했고(lease_until=NULL), 행은 STARTED 로 남았다.
lease 가 없는 STARTED 행은 점유 RPC 가 회수하지 않으므로 그 작업은 영원히 멈춘다.
"""

import asyncio
import os
import unittest
from unittest.mock import patch

from processgpt_agent_sdk import processgpt_agent_framework as fw


class _SlowExecutor:
    def __init__(self):
        self.started = asyncio.Event()
        self.cancel_called = False

    async def execute(self, context, event_queue):
        self.started.set()
        await asyncio.sleep(60)

    async def cancel(self, context, event_queue):
        self.cancel_called = True


class _NoopQueue:
    ended_in_failure = False

    def __init__(self, *_a, **_k):
        pass

    def task_done(self):
        pass

    async def drain(self):
        pass

    async def enqueue_event(self, *_a, **_k):
        pass


ROW = {"id": "33333333-3333-3333-3333-333333333333", "tenant_id": "t", "agent_mode": "COMPLETE",
       "proc_inst_id": "p1", "claim_count": 1}


def _run(scenario, *, status_after_start=""):
    executor = _SlowExecutor()
    server = fw.ProcessGPTAgentServer(executor, "bench-agent", tenant_auth=False)
    released = []
    status = {"value": "STARTED"}

    async def fake_prepare(self):
        return None

    async def fake_release(todo_id, consumer):
        released.append((todo_id, consumer))
        return True

    async def fake_status(_todo_id):
        return status["value"]

    async def main():
        task = asyncio.create_task(server.process_todolist_item(dict(ROW)))
        await asyncio.wait_for(executor.started.wait(), 5)
        if scenario == "shutdown":
            task.cancel()          # 서버 종료: 바깥 폴링 태스크가 취소된다
        else:
            status["value"] = status_after_start   # 사용자가 화면에서 취소
        try:
            await asyncio.wait_for(task, 10)
        except asyncio.CancelledError:
            pass

    with patch.object(fw, "renew_task_lease_sync", lambda *a: {"renewed": True, "reason": "ok"}), \
         patch.object(fw, "release_task_lease", fake_release), \
         patch.object(fw, "fetch_todo_draft_status", fake_status), \
         patch.object(fw.ProcessGPTRequestContext, "prepare_context", fake_prepare), \
         patch.object(fw, "ProcessEventQueue", _NoopQueue), \
         patch.object(fw, "get_consumer_id", lambda: "w1"), \
         patch.dict(os.environ, {"TASK_LEASE_SECONDS": "20", "TASK_LEASE_HEARTBEAT_SECONDS": "1"}):
        asyncio.run(main())
    return executor, released


class ShutdownLeavesWorkReclaimableTest(unittest.TestCase):
    def test_rs_2_3_shutdown_mid_run_keeps_the_lease_so_another_worker_reclaims(self):
        _executor, released = _run("shutdown")
        # 점유를 비우면 STARTED + lease 없음 = 아무도 회수하지 않는 고아가 된다.
        # 비우지 않으면 lease 가 만료된 뒤 다른 워커가 claim_count=2(reclaim)로 집는다.
        self.assertEqual(released, [])

    def test_user_cancel_still_releases(self):
        executor, released = _run("user_cancel", status_after_start="CANCELLED")
        self.assertTrue(executor.cancel_called)
        self.assertEqual(released, [(ROW["id"], "w1")])



class ShutdownStopsPollingTest(unittest.TestCase):
    def test_rs_2_3_shutdown_stops_the_polling_loop_instead_of_claiming_again(self):
        """종료 신호로 실행이 끊기면 그 워커는 더 집지 않는다.

        2026-10-07 kind e2e: 취소를 삼키고 정상 반환해서 폴링 루프가 계속 돌았다. 종료 중인
        파드가 lease 만료(20초) 뒤 자기 작업을 다시 집었다(claim_count 2 → 실제 종료 뒤 3).
        """
        executor = _SlowExecutor()
        server = fw.ProcessGPTAgentServer(executor, "bench-agent", tenant_auth=False)
        polls = []

        async def poll(_orch, _consumer):
            polls.append(1)
            return dict(ROW) if len(polls) == 1 else None

        async def fake_prepare(self):
            return None

        async def fake_release(*_a):
            return True

        async def fake_status(_todo_id):
            return "STARTED"

        async def noop(*_a, **_k):
            return None

        async def main():
            run = asyncio.create_task(server.run())
            await asyncio.wait_for(executor.started.wait(), 5)
            run.cancel()                      # 파드 종료: 폴링 태스크가 취소된다
            done, _ = await asyncio.wait({run}, timeout=5)
            return run in done, run.cancelled()

        with patch.object(fw, "initialize_db", lambda: None), \
             patch.object(fw, "polling_pending_todos", poll), \
             patch.object(fw, "flush_events_now", noop), \
             patch.object(fw, "renew_task_lease_sync", lambda *a: {"renewed": True, "reason": "ok"}), \
             patch.object(fw, "release_task_lease", fake_release), \
             patch.object(fw, "fetch_todo_draft_status", fake_status), \
             patch.object(fw.ProcessGPTRequestContext, "prepare_context", fake_prepare), \
             patch.object(fw, "ProcessEventQueue", _NoopQueue), \
             patch.object(fw, "get_consumer_id", lambda: "w1"), \
             patch.dict(os.environ, {"TASK_LEASE_SECONDS": "20", "TASK_LEASE_HEARTBEAT_SECONDS": "1"}):
            finished, cancelled = asyncio.run(main())
        self.assertTrue(finished, "폴링 루프가 끝나야 한다")
        self.assertTrue(cancelled, "취소가 삼켜지지 않고 끝까지 전달된다")
        self.assertEqual(len(polls), 1, "종료 중에 다시 폴링하지 않는다")


if __name__ == "__main__":
    unittest.main()
