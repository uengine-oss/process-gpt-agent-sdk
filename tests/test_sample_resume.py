"""재개 샘플 에이전트(sample_server/resume_executor.py)가 재개 사유대로 동작한다.

실행 문맥은 SDK 가 점유한 행으로 만드는 것과 같게 만든다(``extras["resume"]`` =
``resume_info_from_row(row)``). 실제 DB·프로세스 kill 로 같은 네 상황을 보는 것은
sample_server/e2e/ 이다.

판정은 작업 공간의 steps.log 로 한다 — 단계를 수행할 때마다 한 줄이 남으므로, 같은
단계가 두 줄이면 그 일을 두 번 한 것이다.
"""

import asyncio
import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from a2a.types import TaskArtifactUpdateEvent, TaskState, TaskStatusUpdateEvent

from processgpt_agent_sdk import continuation_prompt, resume_info_from_row

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "sample_server"))
import resume_executor as rx  # noqa: E402

TODO = "00000000-0000-0000-0000-0000000000s1"
TASK = "지출결의서를 작성하세요"
FORM_ID = "expense_form"


class _Context:
    """SDK 가 Executor 에 넘기는 실행 문맥 중 샘플이 쓰는 부분."""

    def __init__(self, row):
        self.task_id = row["id"]
        self.context_id = "proc-1"
        self._data = {"row": row, "extras": {
            "resume": resume_info_from_row(row).to_dict(),
            "form_id": FORM_ID,
            "form_fields": [{"key": "document_result", "text": "결과", "type": "textarea"}],
        }}

    def get_context_data(self):
        return self._data

    def get_user_input(self):
        return TASK


class _Queue:
    def __init__(self):
        self.events = []

    async def enqueue_event(self, event):
        self.events.append(event)

    def states(self):
        return [e.status.state for e in self.events if isinstance(e, TaskStatusUpdateEvent)]

    def artifact(self):
        finals = [e for e in self.events if isinstance(e, TaskArtifactUpdateEvent) and e.last_chunk]
        return json.loads(finals[-1].artifact.parts[0].text) if finals else None

    def event_types(self):
        return [dict(e.metadata).get("event_type") for e in self.events if isinstance(e, TaskStatusUpdateEvent)]


def _row(claim_count=1, feedback=None):
    return {"id": TODO, "draft_status": "STARTED", "claim_count": claim_count, "feedback": feedback}


def _feedback(content, kind):
    return [{"time": "2026-10-08T00:00:00Z", "content": content, "user_id": "u1", "kind": kind}]


class SampleResumeTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.env = {"SAMPLE_WORKSPACE": self.tmp.name, "SAMPLE_STEP_SECONDS": "0", "SAMPLE_ASK_BEFORE_STEP": ""}

    def run_once(self, row, **env):
        with patch.dict("os.environ", {**self.env, **env}):
            executor = rx.ResumableExecutor()
        queue = _Queue()
        asyncio.run(executor.execute(_Context(row), queue))
        queue.result = lambda: rx.Workspace(Path(self.tmp.name), TODO).load().get("result") if queue.artifact() else None
        return queue

    def log_lines(self):
        path = Path(self.tmp.name) / TODO / "steps.log"
        return path.read_text(encoding="utf-8").splitlines() if path.exists() else []

    def count(self, step):
        return sum(1 for line in self.log_lines() if line.startswith(f"step{step} "))

    def crash_after_step1(self):
        """1단계를 끝내고 2단계 도중에 죽은 실행이 남긴 작업 공간."""
        ws = rx.Workspace(Path(self.tmp.name), TODO)
        ws.save({"done": [rx.STEPS[0]], "runs": [{"kind": "fresh", "attempt": 1}]})
        ws.log(f"step1 {rx.STEPS[0]} kind=fresh attempt=1")

    def test_fresh_runs_every_step_from_the_task(self):
        q = self.run_once(_row())
        result = q.result()
        self.assertEqual(result["resume_kind"], "fresh")
        self.assertEqual(result["steps"], list(rx.STEPS))
        self.assertEqual(result["input"], TASK)
        self.assertEqual([self.count(n) for n in (1, 2, 3)], [1, 1, 1])

    def test_result_is_shaped_for_the_work_item_form(self):
        q = self.run_once(_row())
        report = q.artifact()[FORM_ID]["document_result"]
        self.assertIn("재개 사유: fresh", report)
        self.assertIn(TASK, report)
        # 단계는 화면이 그리는 도구 호출 이벤트로 낸다. event_type 을 비우면 저장이 거절된다.
        self.assertEqual(q.event_types().count("tool_usage_finished"), len(rx.STEPS))
        self.assertNotIn(None, [t for t, s in zip(q.event_types(), q.states()) if s == TaskState.TASK_STATE_WORKING])

    def test_fresh_clears_a_previous_record(self):
        # 같은 키에 남은 기록이 있어도 신규면 이어 가지 않는다.
        self.crash_after_step1()
        self.run_once(_row())
        self.assertEqual(self.count(1), 2)

    def test_reclaim_does_not_redo_finished_steps(self):
        self.crash_after_step1()
        q = self.run_once(_row(claim_count=2))
        result = q.result()
        self.assertEqual(result["resume_kind"], "reclaim")
        self.assertEqual(result["attempt"], 2)
        self.assertEqual(self.count(1), 1, f"끝난 1단계를 다시 했다: {self.log_lines()}")
        self.assertEqual([self.count(2), self.count(3)], [1, 1])
        self.assertEqual(result["steps"], list(rx.STEPS))
        # 같은 지시가 아니라 표준 이어서 지시로 시작한다.
        self.assertEqual(result["input"], continuation_prompt(TASK))

    def test_human_answer_continues_with_the_raw_answer(self):
        answer = "승인자는 홍길동 팀장입니다. 단, 출장비는 빼 주세요"
        asked = self.run_once(_row(), SAMPLE_ASK_BEFORE_STEP="2")
        self.assertEqual(asked.states()[-1], TaskState.TASK_STATE_INPUT_REQUIRED)
        self.assertIsNone(asked.artifact())
        question = json.loads(asked.events[-1].status.message.parts[0].text)
        self.assertEqual(question, {"question": rx.QUESTION, "type": "text"})
        self.assertEqual(self.count(1), 1)

        q = self.run_once(_row(feedback=_feedback(answer, "human_answer")), SAMPLE_ASK_BEFORE_STEP="2")
        result = q.result()
        self.assertEqual(result["resume_kind"], "human_answer")
        self.assertEqual(self.count(1), 1, f"묻기 전에 끝낸 1단계를 다시 했다: {self.log_lines()}")
        self.assertIn(answer, result["input"])  # 요약본이 아니라 원문
        self.assertEqual(result["approver"], answer)
        self.assertEqual(result["steps"], list(rx.STEPS))

    def test_revision_redoes_the_draft_with_the_rejection(self):
        self.run_once(_row())
        note = "표 형식으로 다시 써 주세요"
        q = self.run_once(_row(feedback=_feedback(note, "revision")))
        result = q.result()
        self.assertEqual(result["resume_kind"], "revision")
        self.assertIn(note, result["input"])
        # 반려는 이어 가기가 아니다 — 처음부터 다시 한다.
        self.assertEqual(self.count(1), 2)
        self.assertEqual(result["steps"], list(rx.STEPS))

    def test_reclaim_after_answer_keeps_the_answer(self):
        # 답을 받아 이어 가던 실행이 죽었다. 재점유는 feedback 보다 우선하지만 답은 기록에 있다.
        answer = "홍길동"
        self.run_once(_row(), SAMPLE_ASK_BEFORE_STEP="2")
        ws = rx.Workspace(Path(self.tmp.name), TODO)
        journal = ws.load()
        journal["answer"] = answer
        ws.save(journal)
        q = self.run_once(_row(claim_count=2, feedback=_feedback(answer, "human_answer")), SAMPLE_ASK_BEFORE_STEP="2")
        result = q.result()
        self.assertEqual(result["resume_kind"], "reclaim")
        self.assertEqual(result["approver"], answer)
        self.assertEqual(self.count(1), 1)


if __name__ == "__main__":
    unittest.main()
