# 📘 ProcessGPT Agent SDK – README

## 1. 이게 뭐하는 건가요?
이 SDK는 **ProcessGPT 에이전트 서버**를 만들 때 필요한 **공통 기능**을 제공합니다.  

- DB에서 **작업(todo) 폴링** → 처리할 일감 가져오기  
- **컨텍스트 준비** (사용자 정보, 폼 정의, MCP 설정 등 자동으로 조회)  
- 다양한 **에이전트 오케스트레이션(A2A)** 과 호환  
- **이벤트(Event) 전송 규격 통일화** → 결과를 DB에 안전하게 저장  

👉 쉽게 말하면: **여러 종류의 AI 에이전트를 같은 규칙으로 실행/저장/호출할 수 있게 해주는 통합 SDK** 입니다.  

---

## 2. 아키텍처 다이어그램
```mermaid
flowchart TD
    subgraph DB[Postgres/Supabase]
        T[todolist]:::db
        E[events]:::db
    end

    subgraph SDK
        P[Polling\n(fetch_pending_task)] --> C[Context 준비\n(fetch_context_bundle 등)]
        C --> X[Executor\n(MinimalExecutor)]
        X -->|TaskStatusUpdateEvent| E
        X -->|TaskArtifactUpdateEvent| T
    end

    classDef db fill=#f2f2f2,stroke=#333,stroke-width=1px;
```

- **todolist**: 각 작업(Task)의 진행 상태, 결과물 저장  
- **events**: 실행 중간에 발생한 이벤트 로그 저장  
- SDK는 두 테이블을 자동으로 연결해 줍니다.  

---

## 3. A2A 타입과 이벤트 종류

### A2A 타입 (2가지)
| A2A 타입 | 설명 | 매칭 테이블 |
|----------|------|-------------|
| **TaskStatusUpdateEvent** | 작업 상태 업데이트 | `events` 테이블 |
| **TaskArtifactUpdateEvent** | 작업 결과물 업데이트 | `todolist` 테이블 |

### (v1.0) Enum 변경사항: `snake_case` → `SCREAMING_SNAKE_CASE`

`a2a-sdk` v1.0부터 A2A 스펙(ProtoJSON) 정합성을 위해 **모든 enum 값이 대문자 스네이크 케이스로 표준화**되었습니다.

- **TaskState**
  - `TaskState.submitted` → `TaskState.TASK_STATE_SUBMITTED`
  - `TaskState.working` → `TaskState.TASK_STATE_WORKING`
  - `TaskState.completed` → `TaskState.TASK_STATE_COMPLETED`
  - `TaskState.failed` → `TaskState.TASK_STATE_FAILED`
  - `TaskState.canceled` → `TaskState.TASK_STATE_CANCELED`
  - `TaskState.input_required` → `TaskState.TASK_STATE_INPUT_REQUIRED`
  - `TaskState.auth_required` → `TaskState.TASK_STATE_AUTH_REQUIRED`
  - `TaskState.rejected` → `TaskState.TASK_STATE_REJECTED`
  - (추가) `TaskState.TASK_STATE_UNSPECIFIED`

- **Role**
  - `Role.user` → `Role.ROLE_USER`
  - `Role.agent` → `Role.ROLE_AGENT`
  - (추가) `Role.ROLE_UNSPECIFIED`

### `events.event_type` enum 매핑

DB 의 `events.event_type` 컬럼은 enum 입니다. Executor 가 emit한 `TaskStatusUpdateEvent` 가 `events` 테이블에 저장될 때 어떤 enum 값으로 들어가는지는 다음과 같이 결정됩니다.

| event_type (DB enum) | 발행 주체 | A2A 이벤트 형태 | 매핑 방식 |
|---|---|---|---|
| `task_started` | Executor | `TaskStatusUpdateEvent(state=SUBMITTED)` | **자동** (state 기반) |
| `task_completed` | Executor | `TaskStatusUpdateEvent(state=COMPLETED)` | **자동** (state 기반) |
| `error` | Executor | `TaskStatusUpdateEvent(state=FAILED)` | **자동** (state 기반) |
| `human_asked` | Executor | `TaskStatusUpdateEvent(state=INPUT_REQUIRED)` | **자동** (state 기반) |
| `task_working` | Executor | `TaskStatusUpdateEvent(state=WORKING)` + `metadata["event_type"]="task_working"` | 명시 |
| `tool_usage_started` / `tool_usage_finished` | Executor | `TaskStatusUpdateEvent(state=WORKING)` + `metadata["event_type"]="tool_usage_*"` | 명시 (sub-event, 아래 참조) |
| `crew_completed` | SDK | (Executor 가 emit X) | **자동** — `TaskArtifactUpdateEvent(last_chunk=True)` 처리 시점에 SDK 가 발행 (안전망: framework 의 `task_done()`) |

> **자동 매핑 규칙**: SDK 는 다음 lifecycle state 를 자동으로 enum 값으로 매핑합니다.
> - `TASK_STATE_SUBMITTED` → `task_started`
> - `TASK_STATE_COMPLETED` → `task_completed`
> - `TASK_STATE_FAILED` → `error`
> - `TASK_STATE_INPUT_REQUIRED` → `human_asked`
>
> `TASK_STATE_WORKING` 은 의도적으로 자동 매핑 대상이 아닙니다. WORKING 은 너무 광범위하고 도메인 sub-event(`tool_usage_*` 등) 의 베이스로도 재사용되므로, sub-event 의미와 충돌하지 않도록 NULL 로 두거나 `metadata["event_type"]` 으로 명시하세요. metadata 가 없으면 `event_type` 컬럼은 NULL 로 저장됩니다(허용됨).
>
> **명시 vs 자동 우선순위**: `metadata["event_type"]` 가 있으면 자동 매핑보다 우선합니다 (explicit > implicit). 예: `state=WORKING + metadata["event_type"]="tool_usage_started"` → `tool_usage_started` 로 저장.
>
> **`task_completed` vs `TaskArtifactUpdateEvent`**: 둘은 별개입니다. `task_completed` 는 events 테이블의 lifecycle 표시이고, 실제 결과물 저장은 `TaskArtifactUpdateEvent(last_chunk=True)` 가 todolist 테이블에 수행합니다.

### A2A 타입 = 라우팅 키 (SDK 는 dumb transport)

**원칙**: Executor 는 A2A 표준 이벤트와 표준 필드만 emit. SDK 는 매직 메타데이터 없이 **A2A 이벤트 타입 자체를 라우팅 키로** 사용합니다. 필터링은 Executor 책임이고 SDK 는 받은 대로 라우팅합니다.

**라우팅 매트릭스**:

| A2A 이벤트 타입 | ChatEventQueue | ProcessEventQueue |
|---|---|---|
| `Task` (라이프사이클 마커) | silently ignore | silently ignore |
| `Message` | SSE `{"type":"token","content":...}` (반복 허용 = 토큰 스트리밍) | silently ignore |
| `TaskStatusUpdateEvent` | silently ignore | events 테이블 저장 (state/text 그대로) |
| `TaskArtifactUpdateEvent(last_chunk=True)` | SSE `done` + chats 저장 | todolist 저장 (`is_final=True`) |

**Executor 의 표준 흐름 (LLM 스트리밍 예시)**:
1. `Task` (state=SUBMITTED) — 라이프사이클 시작
2. 토큰마다:
   - `Message(text=token)` — 채팅용
   - `TaskStatusUpdateEvent(state=WORKING, text=token)` — 프로세스용 (필요 시 JSON payload)
3. `TaskStatusUpdateEvent(state=COMPLETED, text=full)` — 종료 알림 (선택)
4. `TaskArtifactUpdateEvent(last_chunk=True, text=full)` — 최종 결과

> 부하 우려가 있다면 (예: events 테이블에 토큰 row 가 너무 많이 쌓일 경우) Executor 가 직접 필터링/집계하세요. SDK 는 정책을 강제하지 않습니다.

### 도메인 sub-event 기록 (도구 호출 등)

`tool_usage_started`, `tool_usage_finished` 같은 도메인 sub-event 는 A2A `TaskState` 에 직접 매핑되지 않습니다 — `TaskState` 는 작업 전체의 lifecycle (SUBMITTED → WORKING → COMPLETED/FAILED) 추상화이고, 도구 호출은 그 안에서 일어나는 세부 사건입니다.

이런 sub-event 는 `state=TASK_STATE_WORKING` 그대로 두고, **`metadata["event_type"]`** 로 enum 값을 명시하세요. SDK 가 그 값을 `events.event_type` 컬럼에 그대로 기록합니다. `data` 컬럼에는 `text` 로 실은 JSON payload (도구 이름, 인자, 결과 등) 가 저장됩니다.

```python
import json
from a2a.helpers import new_text_status_update_event
from a2a.types import TaskState

# 도구 호출 시작
evt_start = new_text_status_update_event(
    task_id=task_id, context_id=context_id,
    state=TaskState.TASK_STATE_WORKING,
    text=json.dumps(
        {"tool": "web_search", "args": {"query": "process-gpt"}},
        ensure_ascii=False,
    ),
)
evt_start.metadata.update({"event_type": "tool_usage_started"})
await event_queue.enqueue_event(evt_start)

# ... 도구 실제 호출 ...

# 도구 호출 종료
evt_end = new_text_status_update_event(
    task_id=task_id, context_id=context_id,
    state=TaskState.TASK_STATE_WORKING,
    text=json.dumps(
        {"tool": "web_search", "result_summary": "...", "elapsed_ms": 312},
        ensure_ascii=False,
    ),
)
evt_end.metadata.update({"event_type": "tool_usage_finished"})
await event_queue.enqueue_event(evt_end)
```

> **TaskState 는 lifecycle, metadata 는 도메인 분류**: A2A 표준 envelope 안에 머무르면서 도메인 이벤트도 enum 으로 정확히 기록할 수 있는 방식입니다. `metadata` 자체는 A2A `TaskStatusUpdateEvent` 의 표준 free-form 필드라 "A2A 표준만 사용" 원칙과 충돌하지 않습니다.
> 
> **빈도 주의**: tool_usage 는 trace 성격이라 LLM 한 번에 도구 5번 호출하면 events row 10개가 쌓입니다. 너무 빈번하면 Executor 측에서 sampling/aggregation 을 적용하거나, 결과만 한 번에 묶어 emit 하세요. `tool_usage_*` 는 **도구 호출 단위**에서만 emit하고, 토큰 스트리밍 루프 안에는 절대 넣지 마세요.

---

## 4. 사용 예시

이 SDK는 “하나의 완제품 서비스”가 아니라, **내 서비스에 붙여서 사용하는 프레임워크/라이브러리**입니다.

아래 예시는 한 프로세스에서 다음을 동시에 제공합니다.

- **프로세스(폴링)**: `await server.run()`로 DB에서 todo를 가져와 처리
- **채팅(SSE)**: `/chat/stream` 엔드포인트로 요청을 받아 `Message-only`로 응답 + `chats`에 저장

### 4.1 서버 구성 예시 (폴링 + SSE 함께)

```python
import asyncio

import uvicorn
from starlette.applications import Starlette

from processgpt_agent_sdk import ProcessGPTAgentServer
from my_service.my_executor import MyExecutor


async def main():
    server = ProcessGPTAgentServer(
        agent_executor=MyExecutor(),
        agent_type="langchain-react",
    )

    app = Starlette()
    server.mount_chat_sse(app, path="/chat/stream")

    uvicorn_server = uvicorn.Server(
        uvicorn.Config(app, host="127.0.0.1", port=8010, log_level="info")
    )

    await asyncio.gather(
        server.run(),
        uvicorn_server.serve(),
    )


if __name__ == "__main__":
    asyncio.run(main())
```

### 4.2 Executor 구현 예시 (A2A 표준만 사용)

> **제 1원칙**: Executor 는 A2A 표준 이벤트만 emit. SDK 매직 메타데이터(`metadata.update({"type":"token",...})` 같은) 일절 사용 금지. **A2A 이벤트 타입 자체가 라우팅 키.**

이벤트 흐름:
1. `Task(state=SUBMITTED)` — 라이프사이클 시작
2. 토큰마다:
   - `Message(text=token)` — 채팅(SSE) 토큰 청크. ChatEventQueue 가 처리.
   - `TaskStatusUpdateEvent(state=WORKING, text=token)` — 프로세스 진행. ProcessEventQueue 가 events 테이블에 저장.
3. `TaskStatusUpdateEvent(state=COMPLETED, text=full)` — 종료 알림 (선택)
4. `TaskArtifactUpdateEvent(last_chunk=True, text=full)` — 최종 결과. 둘 다 처리 (chats / todolist).

> 도구 호출 같은 도메인 sub-event 는 위 코드 흐름과 별개로, "도메인 sub-event 기록" 섹션의 패턴 (`metadata["event_type"]`) 을 참고해서 도구 호출 단위에서 emit 하세요.

```python
import os

from a2a.helpers import (
    new_task,
    new_text_artifact_update_event,
    new_text_message,
    new_text_status_update_event,
)
from a2a.types import Role, TaskState
import litellm


class MyExecutor(...):
    async def execute(self, context, event_queue):
        model = os.environ.get("LLM_MODEL")
        proxy_url = (os.environ.get("LLM_PROXY_URL") or "").rstrip("/")
        api_key = os.environ.get("LLM_PROXY_API_KEY")
        api_base = proxy_url if proxy_url.endswith("/v1") else f"{proxy_url}/v1"

        task_id = str(context.task_id)
        context_id = str(context.context_id)

        # 1) 라이프사이클 시작
        await event_queue.enqueue_event(
            new_task(task_id=task_id, context_id=context_id, state=TaskState.TASK_STATE_SUBMITTED)
        )

        # 2) LLM 스트리밍
        stream = await litellm.acompletion(
            model=model,
            messages=[
                {"role": "system", "content": "You are a helpful assistant. Reply in Korean."},
                {"role": "user", "content": context.get_user_input()},
            ],
            temperature=0, stream=True,
            api_base=api_base, api_key=api_key,
        )

        full = ""
        async for chunk in stream:
            try:
                token = chunk.choices[0].delta.content
            except Exception:
                token = None
            if not token:
                continue
            full += token

            # 채팅용 — Message 1개 = SSE token 1개
            await event_queue.enqueue_event(
                new_text_message(text=token, role=Role.ROLE_AGENT)
            )

            # 프로세스용 — events 테이블에 진행 row 1개씩.
            # 부하가 우려되면 여기서 직접 필터링/집계 (예: JSON payload 단위로만 emit).
            await event_queue.enqueue_event(
                new_text_status_update_event(
                    task_id=task_id, context_id=context_id,
                    state=TaskState.TASK_STATE_WORKING,
                    text=token,
                )
            )

        # 3) 종료 알림 (선택)
        await event_queue.enqueue_event(
            new_text_status_update_event(
                task_id=task_id, context_id=context_id,
                state=TaskState.TASK_STATE_COMPLETED,
                text=full,
            )
        )

        # 4) 최종 결과 — chats / todolist 양쪽이 동일 이벤트로 저장
        await event_queue.enqueue_event(
            new_text_artifact_update_event(
                task_id=task_id, context_id=context_id,
                name="assistant_response",
                text=full,
                last_chunk=True,
            )
        )
```

> **저장 형식**: chats 테이블에 저장되는 payload 구조는 프레임워크가 책임집니다. Executor는 raw A2A 이벤트만 emit하면 됩니다. 저장 스키마를 바꾸고 싶다면 `mount_chat_sse(persist=...)`로 커스텀 persist 함수를 주입하세요.

> **하위호환**: 기존에 채팅 경로에서 `Message`로 최종 응답을 emit하던 Executor도 그대로 동작합니다. `ChatEventQueue`는 `TaskArtifactUpdateEvent`(권장) 또는 `Message` 둘 다 최종 응답으로 받아들입니다.

### 4.3 채팅(SSE) 요청 예시

요청 바디 예시:

```bash
curl -N -X POST http://127.0.0.1:8010/chat/stream \
  -H 'content-type: application/json' \
  -d '{"message":"hello","conversation_id":"conv-1","tenant_id":"","user_uid":"u1"}'
```

### 4.4 설치(옵션: SSE)

채팅(SSE)을 포함해 사용하려면 extras가 필요합니다.

```bash
pip install "process-gpt-agent-sdk[sse]"
```

참고로, 레포에는 빠르게 확인할 수 있는 샘플(`sample_server/minimal_server.py`, `sample_server/minimal_executor.py`)도 포함되어 있습니다.

---

## 5. ⚠️ JSON 직렬화 주의 (str() 절대 금지)

반드시 `json.dumps()`로 직렬화해야 합니다.  

- ❌ 이렇게 하면 안됨:
  ```python
  text = str({"key": "value"})  # Python dict string → JSON 아님
  ```
  DB에 `"'{key: value}'"` 꼴로 문자열 저장됨 → 파싱 실패

- ✅ 이렇게 해야 함:
  ```python
  text = json.dumps({"key": "value"}, ensure_ascii=False)
  ```
  DB에 `{"key": "value"}` JSON 저장됨 → 파싱 성공

👉 **SDK는 내부에서 `json.loads`로 재파싱**하기 때문에, 표준 JSON 문자열이 아니면 무조건 문자열로만 남습니다.  

---

## 6. 사용법 (내 코드에 붙이기)

핵심은 **Executor 안에서 모드를 분기하지 않는 것**입니다. 동일한 A2A 이벤트 시퀀스를 emit하면, 프레임워크의 EventQueue 구현체가 프로세스/채팅에 맞게 라우팅합니다.

- 공통 이벤트 흐름: `Task` → `TaskStatusUpdateEvent[..]` → `TaskArtifactUpdateEvent(last_chunk=True)`
- **프로세스(폴링) 경로**: `ProcessEventQueue`가 status는 events 테이블, artifact는 todolist 테이블에 저장
- **채팅(SSE) 경로**: `ChatEventQueue`가 status는 SSE message 청크로, 최종 artifact는 SSE done + chats 테이블 저장으로 변환

### 6.1 프로세스(폴링)만 실행

```python
from processgpt_agent_sdk import ProcessGPTAgentServer

server = ProcessGPTAgentServer(agent_executor=MyExecutor(), agent_type="crewai-action")
await server.run()
```

### 6.2 채팅(SSE) 엔드포인트 추가

SSE를 쓰려면 extras 설치가 필요합니다.

```bash
pip install "process-gpt-agent-sdk[sse]"
```

```python
from starlette.applications import Starlette

from processgpt_agent_sdk import ProcessGPTAgentServer

server = ProcessGPTAgentServer(agent_executor=MyExecutor(), agent_type="crewai-action")
app = Starlette()
server.mount_chat_sse(app, path="/chat/stream")  # POST /chat/stream
```

요청 바디 예시:

```json
{
  "message": "안녕",
  "tenant_id": "t1",
  "user_uid": "u1",
  "user_email": "user@example.com",
  "user_name": "홍길동",
  "user_jwt": "",
  "conversation_id": "conv-1",
  "file": null,
  "files": [],
  "file_count": 0,
  "stream": true,
  "metadata": {}
}
```

### 6.3 폴링 + SSE를 한 프로세스에서 함께 실행 (권장 예시)

```python
import asyncio

import uvicorn
from starlette.applications import Starlette

from processgpt_agent_sdk import ProcessGPTAgentServer


async def main():
    server = ProcessGPTAgentServer(agent_executor=MyExecutor(), agent_type="crewai-action")

    app = Starlette()
    server.mount_chat_sse(app, path="/chat/stream")

    uvicorn_server = uvicorn.Server(
        uvicorn.Config(app, host="127.0.0.1", port=8010, log_level="info")
    )

    await asyncio.gather(
        server.run(),
        uvicorn_server.serve(),
    )


if __name__ == "__main__":
    asyncio.run(main())
```

운영 환경에서는 **폴링 프로세스**와 **HTTP API 프로세스**를 분리 운영하는 경우도 많습니다.

## 7. 버전업
- ./release.sh 버전
- 오류 발생시 : python -m ensurepip --upgrade

## 8. integrations 모듈 안내
- 스토리지 업로드 유틸은 `processgpt_agent_sdk.integrations.storage` 로 분리되었습니다.
- 기존 `processgpt_agent_sdk.utils.upload_file_to_bucket`, `upload_files_to_bucket` 는 하위호환용으로 유지되지만 deprecated 입니다.
- 신규 코드는 아래 경로를 사용하세요:
  - `from processgpt_agent_sdk.integrations.storage import upload_file_to_bucket, upload_files_to_bucket`