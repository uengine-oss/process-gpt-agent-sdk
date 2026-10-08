"""재개 사유를 받아 재개 방식을 고르는 최소 Executor (샘플).

SDK 는 워크아이템을 집을 때 "왜 다시 실행되는가" 를 정해 실행 문맥에 실어 준다
(``extras["resume"]`` — README §4.8). 이 샘플은 그 값 하나로 재개 방식을 고른다.
프레임워크(LangGraph·CLI 세션·Codex thread) 대신 작업 공간의 진행 기록(journal)을
"중단 지점까지의 기록" 으로 쓴다 — 새 에이전트를 붙일 때 바꿀 곳은 그 기록뿐이다.

| 사유 | 이 샘플이 하는 것 |
|---|---|
| ``fresh`` | 기록을 지우고 1단계부터 |
| ``reclaim`` | 기록을 그대로 두고 완료되지 않은 단계부터. 입력은 표준 이어서 지시 |
| ``human_answer`` | 멈춘 단계부터. 답 **원문** 을 입력에 넣는다 |
| ``revision`` | 기록을 지우고 1단계부터 다시. 반려 원문을 입력에 넣는다 |

단계는 일부러 LLM 을 부르지 않는다(결정적이어야 재개를 검증할 수 있다). 실제
에이전트라면 ``RunPlan.prompt`` 를 모델에 넣고 단계 대신 도구 호출이 일어난다.

환경 변수
- ``SAMPLE_WORKSPACE``: 작업 공간 루트(기본 ``./.sample-workspace``). 재점유하는
  워커와 공유해야 한다(파드라면 PVC).
- ``SAMPLE_STEP_SECONDS``: 단계 하나에 걸리는 시간(기본 1). 크래시 실험에서 늘린다.
- ``SAMPLE_ASK_BEFORE_STEP``: 이 단계(1부터) 전에 사람에게 묻는다. 비우면 묻지 않는다.
"""

from __future__ import annotations

import asyncio
import json
import os
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, List

from typing_extensions import override

from a2a.helpers import new_text_artifact_update_event, new_text_status_update_event
from a2a.server.agent_execution import AgentExecutor, RequestContext
from a2a.server.events import EventQueue
from a2a.types import TaskState

from processgpt_agent_sdk import (
    RESUME_HUMAN_ANSWER,
    RESUME_RECLAIM,
    RESUME_REVISION,
    ResumeInfo,
    continuation_prompt,
    resume_info_of,
)

STEPS = ("자료 수집", "초안 작성", "검토")
QUESTION = "초안에 넣을 승인자 이름을 알려 주세요."


@dataclass(frozen=True)
class RunPlan:
    """이번 실행을 어떻게 시작하는가."""

    #: 처음 실행할 단계의 위치(0 부터). 그 앞 단계는 이미 끝났다.
    start: int
    #: 진행 기록을 지우고 시작하는가.
    reset: bool
    #: 이번 실행의 입력. 실제 에이전트라면 모델에 들어가는 지시다.
    prompt: str


def plan_run(resume: ResumeInfo, journal: Dict[str, Any], task: str) -> RunPlan:
    """재개 사유와 진행 기록으로 이번 실행의 시작점과 입력을 정한다."""
    done = len(journal.get("done") or [])
    if resume.kind == RESUME_RECLAIM:
        # 크래시 뒤 다른 실행이 이어받았다. 같은 지시를 다시 넣으면 끝난 단계를
        # 반복한다 — 표준 이어서 지시로 바꾸고, 기록은 그대로 둔다.
        return RunPlan(start=done, reset=False, prompt=continuation_prompt(task))
    if resume.kind == RESUME_HUMAN_ANSWER:
        # 사람에게 묻고 멈춘 곳부터. 요약본(summarized_feedback)이 아니라 원문을 넣는다.
        return RunPlan(start=done, reset=False, prompt=f"{task}\n\n[사람의 답]\n{resume.answer_raw}")
    if resume.kind == RESUME_REVISION:
        # 결과가 반려됐다. 이어 갈 것이 아니라 처음부터 다시 쓴다.
        return RunPlan(start=0, reset=True, prompt=f"{task}\n\n[반려 사유 — 반영해 다시 작성]\n{resume.answer_raw}")
    return RunPlan(start=0, reset=True, prompt=task)


class Workspace:
    """워크아이템 하나의 작업 공간. 재개 키(todo id)마다 폴더 하나다.

    - ``journal.json``: 끝난 단계와 받은 답 — 재개의 근거
    - ``steps.log``: 단계를 실제로 수행할 때마다 한 줄. 부수효과(외부 호출, 대장
      기록)를 대신한다. 같은 단계가 두 줄이면 그 일을 두 번 한 것이다.
    """

    def __init__(self, root: Path, key: str):
        self.dir = root / (key or "no-key")
        self.dir.mkdir(parents=True, exist_ok=True)
        self.journal_path = self.dir / "journal.json"
        self.log_path = self.dir / "steps.log"

    def load(self) -> Dict[str, Any]:
        try:
            return json.loads(self.journal_path.read_text(encoding="utf-8"))
        except (FileNotFoundError, ValueError):
            return {}

    def save(self, journal: Dict[str, Any]) -> None:
        tmp = self.journal_path.with_suffix(".tmp")
        tmp.write_text(json.dumps(journal, ensure_ascii=False), encoding="utf-8")
        tmp.replace(self.journal_path)  # 단계 도중 죽어도 기록이 반쯤 쓰이지 않는다

    def log(self, line: str) -> None:
        with self.log_path.open("a", encoding="utf-8") as f:
            f.write(line + "\n")


class ResumableExecutor(AgentExecutor):
    def __init__(self, workspace_root: str | None = None):
        self.root = Path(workspace_root or os.getenv("SAMPLE_WORKSPACE") or ".sample-workspace")
        self.step_seconds = float(os.getenv("SAMPLE_STEP_SECONDS") or "1")
        ask = (os.getenv("SAMPLE_ASK_BEFORE_STEP") or "").strip()
        self.ask_before = int(ask) if ask else None

    @override
    async def execute(self, context: RequestContext, event_queue: EventQueue) -> None:
        resume = resume_info_of(context)
        task_id = str(context.task_id or resume.key or "task")
        context_id = str(context.context_id or "ctx")
        task = context.get_user_input() or ""

        ws = Workspace(self.root, resume.key or task_id)
        journal = ws.load()
        plan = plan_run(resume, journal, task)
        if plan.reset:
            journal = {}
        if resume.kind == RESUME_HUMAN_ANSWER:
            journal["answer"] = resume.answer_raw
        journal.setdefault("runs", []).append(
            {"kind": resume.kind, "attempt": resume.attempt, "start": plan.start + 1, "prompt": plan.prompt}
        )
        ws.save(journal)

        async def status(state: TaskState, text: str) -> None:
            event = new_text_status_update_event(task_id=task_id, context_id=context_id, state=state, text=text)
            if state == TaskState.TASK_STATE_WORKING:
                # WORKING 은 자동 매핑되지 않는다. events.event_type 은 NOT NULL 이라
                # 비워 두면 같은 묶음의 이벤트(human_asked 등)까지 저장이 늦어진다.
                event.metadata.update({"event_type": "task_working"})
            await event_queue.enqueue_event(event)

        await status(TaskState.TASK_STATE_SUBMITTED, f"재개 사유: {resume.kind} (점유 {resume.attempt}회차)")

        done: List[str] = list(journal.get("done") or [])
        for index in range(plan.start, len(STEPS)):
            number = index + 1
            if self.ask_before == number and not journal.get("answer"):
                # 묻고 끝낸다. SDK 가 작업을 HUMAN_ASKED 로 두고, 화면에서 답하면
                # human_answer 로 다시 집힌다.
                await status(TaskState.TASK_STATE_INPUT_REQUIRED, QUESTION)
                return
            await asyncio.sleep(self.step_seconds)
            ws.log(f"step{number} {STEPS[index]} kind={resume.kind} attempt={resume.attempt}")
            done.append(STEPS[index])
            journal["done"] = done
            ws.save(journal)
            await status(TaskState.TASK_STATE_WORKING, f"step{number} {STEPS[index]} 완료")

        result = {
            "resume_kind": resume.kind,
            "attempt": resume.attempt,
            "steps": done,
            "input": plan.prompt,
            "approver": journal.get("answer", ""),
        }
        await status(TaskState.TASK_STATE_COMPLETED, "완료")
        await event_queue.enqueue_event(
            new_text_artifact_update_event(
                task_id=task_id,
                context_id=context_id,
                name="result",
                text=json.dumps(result, ensure_ascii=False),
                append=False,
                last_chunk=True,
                artifact_id=str(uuid.uuid4()),
            )
        )

    @override
    async def cancel(self, context: RequestContext, event_queue: EventQueue) -> None:
        return
