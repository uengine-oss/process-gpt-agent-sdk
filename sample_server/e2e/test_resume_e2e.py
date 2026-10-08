"""재개 샘플 에이전트 e2e — 실제 점유 RPC, 실제 프로세스 kill, 설치된 SDK.

run.sh 로 돌린다. PyPI 에서 받은 SDK 를 새 venv 에 깔고, 그 venv 의 python 으로
워커(sample_server/resume_server.py)를 띄운다. 워커가 기동 때 남기는 SDK 버전·경로를
확인해 소스 경로로 돈 것은 실패로 본다.

대상 DB 는 로컬 Supabase(process-gpt-vue3)다. 이 테스트가 만드는 행은
activity_name 이 MARKER 로 시작하고, 지우는 것도 그 행들뿐이다.
"""

from __future__ import annotations

import json
import os
import re
import signal
import subprocess
import sys
import tempfile
import time
import uuid
from pathlib import Path
from urllib.parse import urlparse

import pytest

if os.getenv("RESUME_SAMPLE_E2E") != "1":
    pytest.skip("run.sh 로 돌린다(RESUME_SAMPLE_E2E=1)", allow_module_level=True)

HERE = Path(__file__).resolve().parent
REPO = HERE.parents[1]
SERVER = HERE.parent / "resume_server.py"
LOG_DIR = HERE / ".logs"

CONTAINER = os.getenv("RESUME_SAMPLE_DB_CONTAINER", "supabase_db_process-gpt-vue3")
TEMPLATE_TODO = os.getenv("RESUME_SAMPLE_TEMPLATE_TODO", "1ec57f21-0fea-452f-a310-3ff3419d3531")
AGENT_ORCH = "resume-sample"
MARKER = "[resume-sample]"
EXPECTED_SDK = os.getenv("RESUME_SAMPLE_SDK_VERSION", "0.11.0")
LEASE_SECONDS = 8
TIMEOUT = 120
#: 워커들이 공유하는 작업 공간. 실행 결과(재개 사유·입력)는 여기 journal.json 에 남는다.
WORKSPACE: Path | None = None


# ------------------------------------------------------------------ DB


def sql(query: str) -> list[list[str]]:
    out = subprocess.run(
        ["docker", "exec", "-i", CONTAINER, "psql", "-U", "postgres", "-d", "postgres",
         "-qAt", "-F", "\t", "-v", "ON_ERROR_STOP=1", "-c", query],
        capture_output=True, text=True, check=True,
    ).stdout
    return [line.split("\t") for line in out.splitlines() if line]


def lit(value: str) -> str:
    return "'" + value.replace("'", "''") + "'"


def row(todo_id: str) -> dict:
    rows = sql(
        "SELECT coalesce(draft_status::text, ''), coalesce(claim_count, 0), coalesce(draft::text, ''), "
        f"coalesce(feedback::text, '') FROM todolist WHERE id = {lit(todo_id)}"
    )
    ds, claims, draft, feedback = rows[0]
    journal = WORKSPACE / todo_id / "journal.json" if WORKSPACE else None
    try:
        result = json.loads(journal.read_text(encoding="utf-8")).get("result") if journal else None
    except (FileNotFoundError, ValueError):
        result = None
    return {
        "draft_status": ds,
        "claim_count": int(claims),
        "draft": json.loads(draft) if draft else None,
        "feedback": json.loads(feedback) if feedback else None,
        # 이번 실행의 재개 사유·입력. 화면에 보이는 draft 는 폼 모양의 보고서다.
        "result": result,
    }


def report(r: dict) -> str:
    """draft({form_id: {필드: 보고서}})의 보고서 본문."""
    values = [v for form in (r["draft"] or {}).values() if isinstance(form, dict) for v in form.values()]
    return "\n".join(str(v) for v in values)


def event_count(todo_id: str, event_type: str) -> int:
    return int(sql(f"SELECT count(*) FROM events WHERE todo_id::text = {lit(todo_id)} "
                   f"AND event_type::text = {lit(event_type)}")[0][0])


def clone_task() -> str:
    todo_id = str(uuid.uuid4())
    sql(
        "INSERT INTO todolist "
        "SELECT (jsonb_populate_record(null::todolist, to_jsonb(t) || jsonb_build_object("
        f"  'id', {lit(todo_id)}, 'agent_orch', {lit(AGENT_ORCH)},"
        f"  'activity_name', {lit(MARKER + ' ')} || t.activity_name,"
        "  'status', 'IN_PROGRESS', 'agent_mode', 'DRAFT', 'draft', null, 'output', null,"
        "  'log', null, 'feedback', null, 'temp_feedback', null,"
        "  'start_date', now()::timestamp, 'end_date', null, 'updated_at', now(),"
        "  'draft_status', null, 'consumer', null, 'lease_until', null, 'claim_count', 0"
        "))).* "
        f"FROM todolist t WHERE t.id = {lit(TEMPLATE_TODO)}"
    )
    return todo_id


def screen_feedback(todo_id: str, content: str, kind: str) -> None:
    """화면이 하는 것과 같다 — 질문 카드 답(resumePatchForAnswer)·결과 반려(revisionFeedbackEntry).

    feedback 에 kind 를 단 항목을 덧붙이고 FB_REQUESTED 로 돌린다.
    """
    entry = json.dumps({"time": "2026-10-08T00:00:00Z", "content": content, "user_id": None, "kind": kind},
                       ensure_ascii=False)
    sql(
        "UPDATE todolist SET feedback = coalesce(feedback, '[]'::jsonb) || "
        f"jsonb_build_array({lit(entry)}::jsonb), draft_status = 'FB_REQUESTED' "
        f"WHERE id = {lit(todo_id)}"
    )


def remove(ids: list[str]) -> None:
    if not ids:
        return
    joined = ", ".join(lit(i) for i in ids)
    sql(f"DELETE FROM events WHERE todo_id::text IN ({joined})")
    sql(f"DELETE FROM task_execution_properties WHERE todo_id IN ({joined})")
    sql(f"DELETE FROM notifications WHERE url IN (SELECT '/todolist/' || x FROM unnest(ARRAY[{joined}]) x)")
    sql(f"DELETE FROM todolist WHERE id::text IN ({joined}) AND activity_name LIKE {lit(MARKER + '%')}")


def wait(todo_id: str, until, what: str, timeout: float = TIMEOUT) -> dict:
    deadline = time.monotonic() + timeout
    while True:
        r = row(todo_id)
        if until(r):
            return r
        if time.monotonic() > deadline:
            raise TimeoutError(f"{timeout}초 안에 '{what}' 에 닿지 않았다: {r}")
        time.sleep(0.25)


def completed_as(kind: str):
    return lambda r: r["draft_status"] == "COMPLETED" and (r["result"] or {}).get("resume_kind") == kind


# ------------------------------------------------------------------ 워커


def _supabase() -> dict[str, str]:
    env = {k: os.environ[k] for k in ("SUPABASE_URL", "SUPABASE_KEY") if os.environ.get(k)}
    if len(env) < 2 and (REPO / ".env").exists():
        for line in (REPO / ".env").read_text(encoding="utf-8").splitlines():
            key, _, value = line.strip().partition("=")
            if key in ("SUPABASE_URL", "SUPABASE_KEY") and key not in env and value:
                env[key] = value.strip().strip('"').strip("'")
    return env


class Worker:
    """워커 프로세스 하나. kill() 은 프로세스 그룹에 SIGKILL — 파드가 죽는 것과 같다."""

    def __init__(self, name: str, workspace: Path, **env: str):
        self.name = name
        self.log_path = LOG_DIR / f"{name}.log"
        self.env = {
            **os.environ, **_supabase(),
            "CONSUMER_ID": f"resume-sample-{name}",
            "TASK_LEASE_SECONDS": str(LEASE_SECONDS),
            "TASK_LEASE_HEARTBEAT_SECONDS": str(LEASE_SECONDS // 4),
            "SAMPLE_WORKSPACE": str(workspace),
            "PYTHONUNBUFFERED": "1",
            # 피드백 요약 LLM 은 부르지 않는다(요약이 실패하면 SDK 는 원문으로 대체한다).
            "LLM_PROXY_URL": "", "LLM_PROXY_API_KEY": "",
            **env,
        }
        self.proc: subprocess.Popen | None = None
        self.sdk_version = ""
        self.sdk_path = ""

    def start(self) -> "Worker":
        LOG_DIR.mkdir(exist_ok=True)
        log = open(self.log_path, "wb")
        # cwd 를 저장소 밖에 둔다 — 저장소 루트가 sys.path 에 들어가면 소스가 설치본을 가린다.
        self.proc = subprocess.Popen(
            [sys.executable, str(SERVER)],
            cwd=self.env["SAMPLE_WORKSPACE"], env=self.env,
            stdout=log, stderr=subprocess.STDOUT, start_new_session=True,
        )
        deadline = time.monotonic() + 60
        while time.monotonic() < deadline:
            m = re.search(r"resume-sample ready sdk=(\S+) path=(\S+)", self.log_path.read_text(errors="replace"))
            if m:
                self.sdk_version, self.sdk_path = m.group(1), m.group(2)
                return self
            if self.proc.poll() is not None:
                raise RuntimeError(f"{self.name} 가 기동 중 종료됐다: {self.log_path}")
            time.sleep(0.2)
        raise TimeoutError(f"{self.name} 가 기동하지 않았다: {self.log_path}")

    def kill(self) -> None:
        if self.proc and self.proc.poll() is None:
            os.killpg(self.proc.pid, signal.SIGKILL)
            self.proc.wait(timeout=10)

    def stop(self) -> None:
        if self.proc and self.proc.poll() is None:
            os.killpg(self.proc.pid, signal.SIGTERM)
            try:
                self.proc.wait(timeout=15)
            except subprocess.TimeoutExpired:
                pass
        self.kill()

    def log_text(self) -> str:
        return self.log_path.read_text(errors="replace")


class Env:
    def __init__(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="resume-sample-")
        self.workspace = Path(self.tmp.name)
        self.workers: list[Worker] = []
        self.created: list[str] = []

    def worker(self, name: str, **env: str) -> Worker:
        w = Worker(name, self.workspace, **env).start()
        self.workers.append(w)
        return w

    def stop_workers(self) -> None:
        for w in self.workers:
            w.stop()

    def task(self) -> str:
        todo_id = clone_task()
        self.created.append(todo_id)
        return todo_id

    def steps(self, todo_id: str) -> list[str]:
        path = self.workspace / todo_id / "steps.log"
        return path.read_text(encoding="utf-8").splitlines() if path.exists() else []

    def count(self, todo_id: str, step: int) -> int:
        return sum(1 for line in self.steps(todo_id) if line.startswith(f"step{step} "))


@pytest.fixture(scope="module")
def env():
    url = _supabase().get("SUPABASE_URL", "")
    if urlparse(url).hostname not in ("127.0.0.1", "localhost"):
        pytest.fail(f"로컬 Supabase 만 대상으로 한다: SUPABASE_URL={url!r}")
    if not _supabase().get("SUPABASE_KEY"):
        pytest.fail("SUPABASE_KEY 가 없다")
    if not sql(f"SELECT 1 FROM todolist WHERE id = {lit(TEMPLATE_TODO)}"):
        pytest.fail(f"템플릿 작업 {TEMPLATE_TODO} 가 없다(RESUME_SAMPLE_TEMPLATE_TODO)")
    # 테스트 워커가 집을 수 있는 남의 행(점유 RPC 조건과 같다)이 있으면 시작하지 않는다.
    pending = sql(
        f"SELECT count(*) FROM todolist WHERE agent_orch = {lit(AGENT_ORCH)} AND status = 'IN_PROGRESS' "
        f"AND coalesce(activity_name, '') NOT LIKE {lit(MARKER + '%')} AND ("
        "  (agent_mode IN ('DRAFT','COMPLETE') AND draft IS NULL AND draft_status IS NULL)"
        "  OR draft_status = 'FB_REQUESTED'"
        "  OR (draft_status = 'STARTED' AND lease_until IS NOT NULL AND lease_until < now()))"
    )[0][0]
    if pending != "0":
        pytest.fail(f"agent_orch={AGENT_ORCH} 로 집힐 개발 데이터가 {pending}건 있다")
    e = Env()
    global WORKSPACE
    WORKSPACE = e.workspace
    try:
        yield e
    finally:
        e.stop_workers()
        remove(e.created)
        e.tmp.cleanup()


@pytest.fixture(autouse=True)
def fresh_workers(env):
    """시나리오마다 워커를 새로 띄운다. 앞 시나리오의 워커가 남으면 다음 작업을 가로챈다."""
    started = len(env.workers)
    yield
    env.stop_workers()
    for w in env.workers[started:]:
        assert "not persisted" not in w.log_text(), f"이벤트 저장이 거절됐다: {w.log_path}"


# ------------------------------------------------------------------ 시나리오


def test_0_워커는_설치된_SDK_로_돈다(env):
    w = env.worker("probe")
    assert w.sdk_version == EXPECTED_SDK, f"SDK {w.sdk_version} 로 돌았다(기대 {EXPECTED_SDK})"
    assert "site-packages" in w.sdk_path and not w.sdk_path.startswith(str(REPO / "processgpt_agent_sdk")), (
        f"설치본이 아니라 소스로 돌았다: {w.sdk_path}"
    )


def test_1_신규(env):
    env.worker("fresh", SAMPLE_STEP_SECONDS="0.5")
    todo = env.task()
    r = wait(todo, completed_as("fresh"), "신규 완료")
    assert r["claim_count"] == 1
    assert r["result"]["steps"] == ["자료 수집", "초안 작성", "검토"]
    assert [env.count(todo, n) for n in (1, 2, 3)] == [1, 1, 1]
    # 화면 폼에 들어가는 모양({form_id: {필드: 보고서}})으로 저장된다.
    assert "재개 사유: fresh" in report(r), r["draft"]
    assert event_count(todo, "tool_usage_finished") == 3


def test_2_크래시_재점유(env):
    a = env.worker("crash-A", SAMPLE_STEP_SECONDS="4")
    todo = env.task()
    # 1단계를 끝내고 2단계를 하는 도중에 죽인다.
    wait(todo, lambda r: env.count(todo, 1) == 1, "1단계 완료", timeout=60)
    before = row(todo)
    assert before["draft_status"] == "STARTED" and env.count(todo, 2) == 0, f"죽일 틈이 없었다: {before}"
    a.kill()
    kept = env.steps(todo)

    b = env.worker("crash-B", SAMPLE_STEP_SECONDS="0.5")
    r = wait(todo, completed_as("reclaim"), "재점유 완료")
    assert r["claim_count"] == 2
    assert r["result"]["attempt"] == 2
    assert env.steps(todo)[: len(kept)] == kept, "죽기 전 기록이 사라졌다(작업 공간이 새로 만들어졌다)"
    assert env.count(todo, 1) == 1, f"끝난 1단계를 다시 했다: {env.steps(todo)}"
    assert [env.count(todo, 2), env.count(todo, 3)] == [1, 1]
    assert r["result"]["input"].startswith("직전 실행이 중단되었습니다"), r["result"]["input"]
    assert "재개 사유: reclaim (점유 2회차)" in b.log_text()


def test_3_사람_답변(env):
    env.worker("human", SAMPLE_STEP_SECONDS="0.5", SAMPLE_ASK_BEFORE_STEP="2")
    todo = env.task()
    asked = wait(todo, lambda r: r["draft_status"] == "HUMAN_ASKED", "사람에게 묻고 멈춤")
    assert asked["draft"] is None, "질문이 결과로 저장됐다"
    # 이벤트는 묶어서 비동기로 저장된다 — 행 상태보다 늦을 수 있다.
    deadline = time.monotonic() + 30
    while event_count(todo, "human_asked") == 0 and time.monotonic() < deadline:
        time.sleep(0.5)
    assert event_count(todo, "human_asked") >= 1, "질문 이벤트(human_asked)가 남지 않았다"

    answer = "승인자는 홍길동 팀장입니다.\n단, 출장비 항목은 빼 주세요 (원문 그대로)"
    screen_feedback(todo, answer, "human_answer")
    r = wait(todo, completed_as("human_answer"), "답으로 이어서 완료")
    assert answer in r["result"]["input"], f"답 원문이 다음 실행 입력에 없다: {r['result']['input']!r}"
    assert answer in report(r), "화면에 보이는 결과에 답 원문이 없다"
    assert r["result"]["approver"] == answer
    assert env.count(todo, 1) == 1, f"묻기 전에 끝낸 1단계를 다시 했다: {env.steps(todo)}"
    assert r["result"]["steps"] == ["자료 수집", "초안 작성", "검토"]


def test_4_초안_반려(env):
    env.worker("revision", SAMPLE_STEP_SECONDS="0.5")
    todo = env.task()
    wait(todo, completed_as("fresh"), "초안 완료")

    note = "표 형식으로 다시 써 주세요"
    screen_feedback(todo, note, "revision")
    r = wait(todo, completed_as("revision"), "반려 반영 완료")
    assert note in r["result"]["input"]
    # 반려는 이어 가기가 아니다 — 처음부터 다시 쓴다.
    assert env.count(todo, 1) == 2
    assert r["result"]["steps"] == ["자료 수집", "초안 작성", "검토"]
