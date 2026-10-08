"""작업 점유의 만료 시한(lease)을 SDK 쪽에서 고정한다.

여기서 지키는 것은 네 가지다.

1. 집을 때 lease 를 **반드시** 건다. 이 인자를 빼먹으면 RPC 는 만료 없는 점유로
   집어, 워커가 죽었을 때 그 작업은 아무도 회수하지 못한다(고아 STARTED).
2. 수행 중에는 lease 를 **계속 연장한다**. 멈추면 살아서 일하는 중인 작업이
   회수되어 두 번 수행된다.
3. 연장이 거절되면(= 다른 워커가 이미 회수) 진행 중인 실행을 **버린다**.
   계속 돌면 같은 작업이 두 곳에서 끝까지 수행되고, 늦게 끝난 쪽이 앞을 덮는다.
4. 연장은 이벤트 루프에 의존하지 않는다. 익스큐터가 동기 호출로 루프를 붙잡는
   동안에도 heartbeat 이 돌아야 한다.

lease 를 되돌리면 1·2·3·4 가 전부 깨진다.
"""

import asyncio
import os
import threading
import time
import unittest
from unittest.mock import patch

from processgpt_agent_sdk import database
from processgpt_agent_sdk.lease import (
    DEFAULT_LEASE_SECONDS,
    DEFAULT_MAX_CLAIMS,
    LeaseKeeper,
    heartbeat_seconds,
    lease_seconds,
    max_claims,
)


class _FakeResponse:
    def __init__(self, data):
        self.data = data


class _FakeClient:
    """supabase 클라이언트 대역. rpc 호출 인자를 그대로 모아 둔다."""

    def __init__(self, rpc_returns=None):
        self.rpc_calls = []
        self.table_calls = []
        self._rpc_returns = rpc_returns or {}

    def rpc(self, name, params):
        self.rpc_calls.append((name, params))
        data = self._rpc_returns.get(name, [])
        if callable(data):
            data = data(params)
        return _FakeExecutable(data)

    def table(self, name):
        return _FakeTable(name, self.table_calls)


class _FakeExecutable:
    def __init__(self, data):
        self._data = data

    def execute(self):
        return _FakeResponse(self._data)


class _FakeTable:
    def __init__(self, name, sink):
        self.name = name
        self.sink = sink
        self.payload = None

    def update(self, payload):
        self.payload = payload
        return self

    def eq(self, *_a, **_k):
        return self

    def select(self, *_a, **_k):
        return self

    def limit(self, *_a, **_k):
        return self

    def execute(self):
        self.sink.append((self.name, self.payload))
        return _FakeResponse([])


# ============================================================================
# 기본값
# ============================================================================
class LeaseDefaultsTest(unittest.TestCase):
    def setUp(self):
        for key in ("TASK_LEASE_SECONDS", "TASK_LEASE_HEARTBEAT_SECONDS", "TASK_MAX_CLAIMS"):
            os.environ.pop(key, None)

    tearDown = setUp

    def test_heartbeat_is_a_quarter_of_the_lease(self):
        """연장 주기는 lease 의 1/4 이다.

        연속 세 번 실패해도(일시적 DB 오류) lease 가 남아 있어야 한다. 1/2 로 두면
        한 번 놓치는 것만으로 만료에 닿고, 멀쩡한 작업이 회수된다.
        """
        self.assertEqual(lease_seconds(), DEFAULT_LEASE_SECONDS)
        self.assertEqual(heartbeat_seconds(), DEFAULT_LEASE_SECONDS // 4)
        self.assertLessEqual(heartbeat_seconds() * 3, lease_seconds())

    def test_env_overrides_keep_the_ratio_when_only_lease_is_set(self):
        os.environ["TASK_LEASE_SECONDS"] = "20"
        self.assertEqual(lease_seconds(), 20)
        self.assertEqual(heartbeat_seconds(), 5)

    def test_explicit_heartbeat_wins(self):
        os.environ["TASK_LEASE_SECONDS"] = "20"
        os.environ["TASK_LEASE_HEARTBEAT_SECONDS"] = "3"
        self.assertEqual(heartbeat_seconds(), 3)

    def test_garbage_env_falls_back_instead_of_crashing_the_worker(self):
        os.environ["TASK_LEASE_SECONDS"] = "글자"
        os.environ["TASK_MAX_CLAIMS"] = "-1"
        self.assertEqual(lease_seconds(), DEFAULT_LEASE_SECONDS)
        self.assertEqual(max_claims(), DEFAULT_MAX_CLAIMS)


# ============================================================================
# 1. 집을 때 lease 를 건다
# ============================================================================
class PollingPassesLeaseTest(unittest.TestCase):
    def setUp(self):
        for key in ("TASK_LEASE_SECONDS", "TASK_LEASE_HEARTBEAT_SECONDS", "TASK_MAX_CLAIMS"):
            os.environ.pop(key, None)

    tearDown = setUp

    def _poll(self, client):
        with patch.object(database, "get_db_client", return_value=client):
            return asyncio.run(database.polling_pending_todos("bench-agent", "w1"))

    def test_claim_requests_a_lease(self):
        """p_lease_seconds 없이 집으면 그 작업은 워커가 죽어도 회수되지 않는다."""
        client = _FakeClient()
        self._poll(client)

        name, params = client.rpc_calls[0]
        self.assertEqual(name, "fetch_pending_task")
        self.assertEqual(params["p_lease_seconds"], DEFAULT_LEASE_SECONDS)
        self.assertEqual(params["p_max_claims"], DEFAULT_MAX_CLAIMS)

    def test_claim_lease_follows_the_env(self):
        os.environ["TASK_LEASE_SECONDS"] = "20"
        os.environ["TASK_MAX_CLAIMS"] = "2"
        client = _FakeClient()
        self._poll(client)

        _, params = client.rpc_calls[0]
        self.assertEqual(params["p_lease_seconds"], 20)
        self.assertEqual(params["p_max_claims"], 2)


    def test_claim_and_heartbeat_use_the_same_consumer_name(self):
        """집을 때의 이름과 연장할 때의 이름이 같아야 한다.

        `renew_task_lease` 는 consumer 가 정확히 같을 때만 연장한다. 어긋나면
        집은 워커가 자기 점유를 "남의 것" 으로 보고 매번 하던 일을 버린다.
        """
        client = _FakeClient()
        with patch.object(database, "get_db_client", return_value=client):
            asyncio.run(database.polling_pending_todos("bench-agent", ""))

        _, params = client.rpc_calls[0]
        self.assertEqual(params["p_consumer"], database.get_consumer_id())


# ============================================================================
# 2·3. 연장과 펜싱
# ============================================================================
class LeaseKeeperTest(unittest.TestCase):
    def test_keeps_renewing_while_the_work_runs(self):
        calls = []
        done = threading.Event()

        def renew(todo_id, consumer, lease_sec):
            calls.append((todo_id, consumer, lease_sec))
            if len(calls) >= 3:
                done.set()
            return {"renewed": True, "reason": "ok"}

        keeper = LeaseKeeper("todo-1", "w1", renew, lease_sec=20, interval_sec=0.02).start()
        self.assertTrue(done.wait(3.0), "heartbeat 이 돌지 않았다")
        keeper.stop()

        self.assertGreaterEqual(len(calls), 3)
        self.assertEqual(calls[0], ("todo-1", "w1", 20))
        self.assertFalse(keeper.lost)

    def test_renews_from_its_own_thread_so_a_blocked_event_loop_cannot_stall_it(self):
        """익스큐터가 루프를 붙잡고 있어도 연장은 계속된다.

        연장을 asyncio 태스크로 두면 여기서 멈춘다. 그 사이 lease 가 만료되면
        살아서 일하는 중인 작업이 회수되어 두 번 수행된다.
        """
        beats = []

        def renew(*_a):
            beats.append(time.monotonic())
            return {"renewed": True, "reason": "ok"}

        async def main():
            keeper = LeaseKeeper("todo-1", "w1", renew, lease_sec=20, interval_sec=0.02).start()
            # 코루틴이 아니라 동기 sleep 으로 루프를 막는다.
            time.sleep(0.3)
            keeper.stop()

        asyncio.run(main())
        self.assertGreaterEqual(len(beats), 3, "루프가 막힌 동안 heartbeat 이 멈췄다")

    def test_not_owner_marks_the_lease_lost_and_calls_back(self):
        """회수된 사실을 알게 되면 하던 일을 버려야 한다(펜싱)."""
        lost = []

        def renew(*_a):
            return {"renewed": False, "reason": "not_owner", "consumer": "other-worker"}

        keeper = LeaseKeeper(
            "todo-1", "w1", renew, on_lost=lost.append, lease_sec=20, interval_sec=0.01
        ).start()
        for _ in range(200):
            if keeper.lost:
                break
            time.sleep(0.01)
        keeper.stop()

        self.assertTrue(keeper.lost)
        self.assertEqual(keeper.lost_reason, "not_owner")
        self.assertEqual(lost, ["not_owner"])

    def test_not_started_stops_renewing_without_declaring_a_loss(self):
        """사람 답변 대기(HUMAN_ASKED)나 완료로 넘어간 작업은 버릴 일이 아니다."""
        lost = []
        calls = []

        def renew(*_a):
            calls.append(1)
            return {"renewed": False, "reason": "not_started", "draft_status": "HUMAN_ASKED"}

        keeper = LeaseKeeper(
            "todo-1", "w1", renew, on_lost=lost.append, lease_sec=20, interval_sec=0.01
        ).start()
        time.sleep(0.2)
        keeper.stop()

        self.assertFalse(keeper.lost)
        self.assertEqual(lost, [])
        self.assertEqual(len(calls), 1, "연장할 점유가 없는데도 계속 두드렸다")

    def test_transient_failure_does_not_abandon_the_work(self):
        """DB 가 잠깐 흔들린 것으로 멀쩡한 작업을 중단하지 않는다.

        lease 는 주기의 4배라 연속 세 번까지 여유가 있다.
        """
        calls = []

        def renew(*_a):
            calls.append(1)
            if len(calls) <= 2:
                raise RuntimeError("일시적 연결 실패")
            return {"renewed": True, "reason": "ok"}

        keeper = LeaseKeeper("todo-1", "w1", renew, lease_sec=20, interval_sec=0.01).start()
        time.sleep(0.2)
        keeper.stop()

        self.assertFalse(keeper.lost)
        self.assertGreater(len(calls), 3, "실패 뒤 재시도가 없었다")


# ============================================================================
# renew/release 의 RPC 계약
# ============================================================================
class RenewAndReleaseTest(unittest.TestCase):
    def test_renew_calls_the_rpc_and_returns_its_verdict(self):
        client = _FakeClient(
            {"renew_task_lease": {"renewed": True, "reason": "ok", "lease_until": "2026-10-02T00:00:20Z"}}
        )
        with patch.object(database, "get_db_client", return_value=client):
            out = database.renew_task_lease_sync("todo-1", "w1", 20)

        name, params = client.rpc_calls[0]
        self.assertEqual(name, "renew_task_lease")
        self.assertEqual(params, {"p_todo_id": "todo-1", "p_consumer": "w1", "p_lease_seconds": 20})
        self.assertTrue(out["renewed"])

    def test_renew_unwraps_a_single_row_response(self):
        """PostgREST 가 스칼라를 리스트로 싸서 주는 경우."""
        client = _FakeClient({"renew_task_lease": [{"renewed": False, "reason": "not_owner"}]})
        with patch.object(database, "get_db_client", return_value=client):
            out = database.renew_task_lease_sync("todo-1", "w1", 20)
        self.assertEqual(out["reason"], "not_owner")

    def test_renew_is_synchronous(self):
        """LeaseKeeper 는 자기 스레드에서 이것을 부른다 — 코루틴이면 안 된다."""
        self.assertFalse(asyncio.iscoroutinefunction(database.renew_task_lease_sync))

    def test_release_clears_the_lease_through_the_rpc(self):
        client = _FakeClient({"release_task_lease": True})
        with patch.object(database, "get_db_client", return_value=client):
            ok = asyncio.run(database.release_task_lease("todo-1", "w1"))

        name, params = client.rpc_calls[0]
        self.assertEqual(name, "release_task_lease")
        self.assertEqual(params, {"p_todo_id": "todo-1", "p_consumer": "w1"})
        self.assertTrue(ok)

    def test_failure_marking_also_drops_the_lease(self):
        client = _FakeClient()
        with patch.object(database, "get_db_client", return_value=client):
            asyncio.run(database.update_task_error("todo-1"))

        _, payload = client.table_calls[0]
        self.assertEqual(payload["draft_status"], "FAILED")
        self.assertIsNone(payload["lease_until"])


# ============================================================================
# 4. 프레임워크 배선: 수행 전체가 lease 아래에서 돈다
# ============================================================================
class _SlowExecutor:
    """취소될 때까지 도는 익스큐터 대역."""

    def __init__(self, seconds=5.0):
        self.seconds = seconds
        self.finished = False
        self.cancelled = False

    async def execute(self, _context, _queue):
        try:
            await asyncio.sleep(self.seconds)
            self.finished = True
        except asyncio.CancelledError:
            self.cancelled = True
            raise

    async def cancel(self, _context, _queue):
        self.cancelled = True


class FrameworkLeaseWiringTest(unittest.TestCase):
    """process_todolist_item 이 lease 를 쥐고 일하는지 본다."""

    def _run(self, renew_results, exec_seconds=5.0):
        from processgpt_agent_sdk import processgpt_agent_framework as fw

        executor = _SlowExecutor(exec_seconds)
        server = fw.ProcessGPTAgentServer(executor, "bench-agent", tenant_auth=False)
        row = {
            "id": "11111111-1111-1111-1111-111111111111",
            "tenant_id": "bench",
            "agent_mode": "DRAFT",
            "proc_inst_id": "p1",
        }
        renewals = []

        def renew(todo_id, consumer, lease_sec):
            renewals.append((todo_id, consumer, lease_sec))
            return renew_results[min(len(renewals) - 1, len(renew_results) - 1)]

        released = []

        async def fake_prepare(self):
            return None

        with patch.object(fw, "renew_task_lease_sync", renew), \
             patch.object(fw, "release_task_lease", lambda *a: _await_true(released, a)), \
             patch.object(fw.ProcessGPTRequestContext, "prepare_context", fake_prepare), \
             patch.object(fw, "ProcessEventQueue", _NoopQueue), \
             patch.object(fw, "fail_task_if_still_started", lambda *a: _await_false()), \
             patch.object(fw, "get_consumer_id", lambda: "w1"), \
             patch.dict(os.environ, {"TASK_LEASE_SECONDS": "20", "TASK_LEASE_HEARTBEAT_SECONDS": "1"}):
            asyncio.run(server.process_todolist_item(row))

        return executor, renewals, released

    def test_losing_the_lease_cancels_the_running_work(self):
        """다른 워커가 회수하면 이쪽 실행은 멈춘다 — 동시 수행이 생기지 않는다."""
        executor, renewals, released = self._run(
            [{"renewed": False, "reason": "not_owner", "consumer": "w2"}], exec_seconds=30.0
        )
        self.assertTrue(executor.cancelled)
        self.assertFalse(executor.finished)
        self.assertEqual(renewals[0][1], "w1")
        # 점유자가 남인데 해제까지 하면, 그쪽의 lease 를 끊어 버린다.
        self.assertEqual(released, [])

    def test_losing_the_lease_during_context_prep_skips_execution(self):
        """컨텍스트 준비 중에 회수당하면 실행 자체를 시작하지 않는다.

        그 시점에는 취소로 막을 exec_task 가 아직 없다. 그냥 시작해 버리면
        회수한 워커와 나란히 같은 작업을 수행한다.
        """
        from processgpt_agent_sdk import processgpt_agent_framework as fw

        executor = _SlowExecutor(5.0)
        server = fw.ProcessGPTAgentServer(executor, "bench-agent", tenant_auth=False)
        row = {"id": "22222222-2222-2222-2222-222222222222", "tenant_id": "bench",
               "agent_mode": "DRAFT", "proc_inst_id": "p1"}

        def renew(*_a):
            return {"renewed": False, "reason": "not_owner", "consumer": "w2"}

        async def slow_prepare(self):
            # 준비가 heartbeat 한 주기(아래 1초)보다 오래 걸리는 상황.
            # 더 짧으면 준비가 끝난 뒤에 회수를 알게 되어, 이 테스트가 노리는
            # "아직 exec_task 가 없는 구간" 이 아니라 평소의 취소 경로가 돈다.
            await asyncio.sleep(2.5)

        released = []
        with patch.object(fw, "renew_task_lease_sync", renew), \
             patch.object(fw, "release_task_lease", lambda *a: _await_true(released, a)), \
             patch.object(fw.ProcessGPTRequestContext, "prepare_context", slow_prepare), \
             patch.object(fw, "ProcessEventQueue", _NoopQueue), \
             patch.object(fw, "fail_task_if_still_started", lambda *a: _await_false()), \
             patch.object(fw, "get_consumer_id", lambda: "w1"), \
             patch.dict(os.environ, {"TASK_LEASE_SECONDS": "20",
                                     "TASK_LEASE_HEARTBEAT_SECONDS": "1"}):
            asyncio.run(server.process_todolist_item(row))

        self.assertFalse(executor.finished)
        self.assertFalse(executor.cancelled, "실행이 시작된 적이 없어야 한다")
        self.assertEqual(released, [])

    def test_normal_completion_releases_the_lease(self):
        executor, _renewals, released = self._run(
            [{"renewed": True, "reason": "ok"}], exec_seconds=0.05
        )
        self.assertTrue(executor.finished)
        self.assertEqual(len(released), 1)
        self.assertEqual(released[0][:2], ("11111111-1111-1111-1111-111111111111", "w1"))


async def _await_false():
    return False


def _await_true(sink, args):
    sink.append(args)

    async def _coro():
        return True

    return _coro()


class _NoopQueue:
    def __init__(self, *_a, **_k):
        pass

    ended_in_failure = False

    def task_done(self):
        pass

    async def drain(self):
        pass

    async def enqueue_event(self, *_a, **_k):
        pass


if __name__ == "__main__":
    unittest.main()
