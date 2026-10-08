"""이벤트 묶음 저장이 한 건 때문에 전부를 잃지 않는다.

저장소가 거절하는 이벤트(예: enum 에 없는 event_type) 하나가 묶음에 섞이면 묶음
저장 RPC 가 통째로 실패한다. 그러면 같은 묶음의 작업 실패 알림(error)까지 사라져
사용자는 작업이 왜 실패했는지 볼 수 없었다.
"""

import asyncio
import unittest
from unittest.mock import patch

from processgpt_agent_sdk import database


class _Rejecting(Exception):
    pass


class _FakeClient:
    """event_type 이 notice 인 이벤트가 든 묶음은 거절한다(실제 enum 위반과 같다)."""

    def __init__(self):
        self.stored = []
        self.calls = 0

    def rpc(self, name, params):
        assert name == "record_events_bulk"
        client = self

        class _Q:
            def execute(self_inner):
                client.calls += 1
                events = params["p_events"]
                if any(e.get("event_type") == "notice" for e in events):
                    raise _Rejecting('invalid input value for enum event_type_enum: "notice"')
                client.stored.extend(events)
                return type("R", (), {"data": None})()

        return _Q()


async def _no_sleep(*_a, **_k):
    return None


class EventsBulkFallbackTest(unittest.TestCase):
    def _record(self, events):
        client = _FakeClient()
        with patch.object(database, "get_db_client", lambda: client), \
             patch.object(database.asyncio, "sleep", _no_sleep):
            asyncio.run(database.record_events_bulk(events))
        return client

    def test_one_rejected_event_does_not_take_the_rest_with_it(self):
        client = self._record([
            {"id": "1", "event_type": "notice", "data": "스킬 일부를 불러오지 못했습니다"},
            {"id": "2", "event_type": "error", "data": {"error": "출력 형식이 맞지 않습니다"}},
            {"id": "3", "event_type": "task_started", "data": {}},
        ])
        self.assertEqual(sorted(e["event_type"] for e in client.stored), ["error", "task_started"])

    def test_a_clean_batch_is_stored_in_one_call(self):
        client = self._record([
            {"id": "1", "event_type": "task_started", "data": {}},
            {"id": "2", "event_type": "task_completed", "data": {}},
        ])
        self.assertEqual(client.calls, 1)
        self.assertEqual(len(client.stored), 2)

    def test_a_single_rejected_event_is_not_retried_one_by_one(self):
        client = self._record([{"id": "1", "event_type": "notice", "data": ""}])
        self.assertEqual(client.stored, [])
        self.assertEqual(client.calls, 3)  # 묶음 재시도만


if __name__ == "__main__":
    unittest.main()
