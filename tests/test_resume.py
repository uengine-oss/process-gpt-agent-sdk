"""워크아이템이 왜 다시 실행되는지(재개 사유)를 실행기에 넘긴다.

스펙: infra/process-gpt/openspec/changes/workitem-resume-default/specs/agent-sdk_workitem-resume-signal

2026-10-06 실측(E1)에서 재점유된 행은 claim_count=2, 3 으로 왔지만 아무도 읽지
않았고, 사람 답변 재개와 반려 재작업은 행 모양이 같아 구분할 수 없었다. 그래서
세 서비스가 모두 이미 끝낸 단계를 처음부터 반복했다. 아래 시나리오는 실제 점유
RPC 가 돌려주는 행 모양 그대로다.
"""

import asyncio
import json
import unittest
from unittest.mock import patch

from processgpt_agent_sdk import (
    RESUME_FRESH,
    RESUME_HUMAN_ANSWER,
    RESUME_RECLAIM,
    RESUME_REVISION,
    ResumeInfo,
    continuation_prompt,
    resume_info_from_row,
    resume_info_of,
)
from processgpt_agent_sdk import processgpt_agent_framework as fw

TODO = "00000000-0000-0000-0000-00000000000a"


def _row(**kw):
    """점유 RPC 가 돌려준 행(RETURNING t.*)과 같은 모양."""
    row = {"id": TODO, "draft_status": "STARTED", "claim_count": 1, "feedback": None, "draft": None}
    row.update(kw)
    return row


class ResumeKindFromRowTest(unittest.TestCase):
    def test_rs_1_1_fresh_claim(self):
        info = resume_info_from_row(_row())
        self.assertEqual(info.kind, RESUME_FRESH)
        self.assertEqual(info.attempt, 1)
        self.assertEqual(info.key, TODO)
        self.assertEqual(info.answer_raw, "")
        self.assertFalse(info.is_resume)

    def test_rs_2_1_reclaim_after_lease_expired(self):
        info = resume_info_from_row(_row(claim_count=2))
        self.assertEqual(info.kind, RESUME_RECLAIM)
        self.assertEqual(info.attempt, 2)
        self.assertTrue(info.is_resume)

    def test_rs_2_2_reclaim_wins_over_human_answer_feedback(self):
        # 사람 답변으로 다시 집힌 실행이 도중에 죽어 다른 워커가 회수했다.
        info = resume_info_from_row(_row(
            claim_count=2,
            feedback=[{"time": "t", "content": "승인", "user_id": "u", "kind": "human_answer"}],
        ))
        self.assertEqual(info.kind, RESUME_RECLAIM)
        self.assertEqual(info.attempt, 2)

    def test_rs_3_1_human_answer_carries_the_raw_answer(self):
        answer = "승인합니다. 단 C사는 제외하세요"
        info = resume_info_from_row(_row(
            feedback=[{"time": "t", "content": answer, "user_id": "u1", "kind": "human_answer"}],
        ))
        self.assertEqual(info.kind, RESUME_HUMAN_ANSWER)
        self.assertEqual(info.answer_raw, answer)

    def test_rs_3_2_revision_after_draft_rejected(self):
        info = resume_info_from_row(_row(
            draft={"x": 1},
            feedback=[{"time": "t", "content": "표 형식으로 다시", "user_id": "u1", "kind": "revision"}],
        ))
        self.assertEqual(info.kind, RESUME_REVISION)
        self.assertEqual(info.answer_raw, "표 형식으로 다시")

    def test_rs_3_2_last_entry_decides(self):
        # 반려를 한 번 거친 뒤 사람에게 묻고 답한 경우 — draft 가 있어도 답변이다.
        info = resume_info_from_row(_row(
            draft={"x": 1},
            feedback=[
                {"content": "표 형식으로 다시", "kind": "revision"},
                {"content": "B안으로 진행", "kind": "human_answer"},
            ],
        ))
        self.assertEqual(info.kind, RESUME_HUMAN_ANSWER)
        self.assertEqual(info.answer_raw, "B안으로 진행")

    def test_rs_3_3_legacy_entry_without_kind_is_revision(self):
        info = resume_info_from_row(_row(feedback=[{"time": "t", "content": "예산 500만원으로 진행", "user_id": "u1"}]))
        self.assertEqual(info.kind, RESUME_REVISION)
        self.assertEqual(info.answer_raw, "예산 500만원으로 진행")

    def test_rs_3_4_json_string_feedback_reads_the_same(self):
        as_list = [{"content": "답", "kind": "human_answer"}]
        self.assertEqual(
            resume_info_from_row(_row(feedback=json.dumps(as_list, ensure_ascii=False))),
            resume_info_from_row(_row(feedback=as_list)),
        )

    def test_rs_3_5_broken_feedback_is_fresh(self):
        for broken in ("깨진 json", "{}", "[]", [], [None], 7):
            with self.subTest(feedback=broken):
                self.assertEqual(resume_info_from_row(_row(feedback=broken)).kind, RESUME_FRESH)
        # 아주 오래된 모양: 문자열만 든 배열은 내용이 있으니 반려로 읽는다.
        self.assertEqual(resume_info_from_row(_row(feedback=["다시"])).answer_raw, "다시")

    def test_missing_or_bad_claim_count_counts_as_first(self):
        for value in (None, 0, "x", -1):
            with self.subTest(claim_count=value):
                info = resume_info_from_row(_row(claim_count=value))
                self.assertEqual(info.kind, RESUME_FRESH)
                self.assertEqual(info.attempt, 1)
        self.assertEqual(resume_info_from_row(_row(claim_count="3")).kind, RESUME_RECLAIM)


class ResumeInfoContractTest(unittest.TestCase):
    def test_rs_1_2_round_trips_through_json(self):
        info = resume_info_from_row(_row(claim_count=3))
        data = json.loads(json.dumps(info.to_dict()))
        self.assertEqual(set(data), {"kind", "attempt", "key", "answer_raw"})
        self.assertEqual(ResumeInfo.from_dict(data), info)

    def test_rs_4_2_reads_extras_then_row_then_fresh(self):
        reclaim = resume_info_from_row(_row(claim_count=2)).to_dict()
        self.assertEqual(resume_info_of({"resume": reclaim}).kind, RESUME_RECLAIM)
        # 구버전 SDK: extras 에 resume 이 없으면 행으로 같은 규칙 적용
        self.assertEqual(resume_info_of({"row": _row(claim_count=2)}).kind, RESUME_RECLAIM)
        self.assertEqual(resume_info_of({}).kind, RESUME_FRESH)
        self.assertEqual(resume_info_of(None).kind, RESUME_FRESH)

    def test_rs_4_2_reads_a_request_context(self):
        class Ctx:
            row = _row(feedback=[{"content": "답", "kind": "human_answer"}])

            def get_context_data(self):
                return {"row": self.row, "extras": {}}

        self.assertEqual(resume_info_of(Ctx()).kind, RESUME_HUMAN_ANSWER)

    def test_rs_5_1_continuation_prompt(self):
        text = continuation_prompt("보고서를 작성하라")
        self.assertIn("중단", text)
        self.assertIn("다시 하지", text)
        self.assertIn("이어서", text)
        self.assertIn("보고서를 작성하라", text)
        self.assertNotIn("None", continuation_prompt())


class RequestContextCarriesResumeTest(unittest.TestCase):
    """SDK 가 실제로 실행기에 넘기는 문맥에 resume 이 실린다."""

    def _prepared(self, row):
        async def _none(*_a, **_k):
            return None

        async def _form(*_a, **_k):
            return ("form", [], "")

        async def _users(*_a, **_k):
            return ([], [])

        async def _list(*_a, **_k):
            return []

        async def _summary(*_a, **_k):
            return "요약본"

        ctx = fw.ProcessGPTRequestContext(row)
        with patch.object(fw, "fetch_email_users_by_proc_inst_id", _none), \
             patch.object(fw, "fetch_tenant_mcp", _none), \
             patch.object(fw, "fetch_form_def", _form), \
             patch.object(fw, "fetch_users_grouped", _users), \
             patch.object(fw, "fetch_proc_inst_sources", _list), \
             patch.object(fw, "summarize_feedback", _summary):
            asyncio.run(ctx.prepare_context())
        return ctx

    def test_rs_1_and_4_1_extras_and_metadata_have_resume_alongside_old_fields(self):
        row = _row(feedback=[{"content": "C사 제외", "kind": "human_answer"}])
        ctx = self._prepared(row)
        extras = ctx.get_context_data()["extras"]
        self.assertEqual(extras["resume"], {
            "kind": "human_answer", "attempt": 1, "key": TODO, "answer_raw": "C사 제외",
        })
        # 기존 필드는 그대로
        self.assertEqual(extras["summarized_feedback"], "요약본")
        self.assertIs(ctx.get_context_data()["row"], row)
        self.assertEqual(ctx.metadata["resume"]["kind"], "human_answer")
        self.assertEqual(ctx.resume.kind, RESUME_HUMAN_ANSWER)

    def test_resume_is_known_before_prepare_context(self):
        ctx = fw.ProcessGPTRequestContext(_row(claim_count=2))
        self.assertEqual(ctx.resume.kind, RESUME_RECLAIM)


if __name__ == "__main__":
    unittest.main()
