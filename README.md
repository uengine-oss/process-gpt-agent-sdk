# 📘 ProcessGPT Agent SDK – README

## 1. 이게 뭐하는 건가요?
이 SDK는 **ProcessGPT 에이전트 서버**를 만들 때 필요한 **공통 기능**을 제공합니다.  

- DB에서 **작업(todo) 폴링** → 처리할 일감 가져오기  
- **컨텍스트 준비** (사용자 정보, 폼 정의, MCP 설정 등 자동으로 조회)  
- 다양한 **에이전트 오케스트레이션(A2A)** 과 호환  
- **이벤트(Event) 전송 규격 통일화** → 결과를 DB에 안전하게 저장  
- **채팅(SSE) 전송 계층** → 하트비트 · 재접속(attach) · 중지(stop) 를 프레임워크가 제공  
- **테넌트 인증** → 요청이 보낸 `tenant_id` 를 요청자의 JWT 로 검증  

👉 쉽게 말하면: **여러 종류의 AI 에이전트를 같은 규칙으로 실행/저장/호출할 수 있게 해주는 통합 SDK** 입니다.  

> **0.5.0 에서 달라진 점**  
> 채팅 SSE 전송 계층과 테넌트 인증이 SDK 로 올라왔습니다. 그동안 각 에이전트
> 저장소가 따로 만들어 쓰던 하트비트·재접속·중지·인증을 `mount_chat_routes()`
> 한 번으로 대체할 수 있습니다. 자세한 내용은 4.5 · 4.6 을 보세요.
> 기존 `mount_chat_sse()` 단독 호출은 동작이 그대로라 곧바로 올려도 깨지지 않습니다.

---

## 2. 아키텍처 다이어그램
```mermaid
flowchart TD
    subgraph DB[Postgres/Supabase]
        T[todolist]:::db
        E[events]:::db
    end

    subgraph DB2[Postgres/Supabase]
        CH[chats]:::db
    end

    subgraph SDK
        P[Polling\n(fetch_pending_task)] --> C[Context 준비\n(fetch_context_bundle 등)]
        C --> X[Executor\n(MinimalExecutor)]
        X -->|TaskStatusUpdateEvent| E
        X -->|TaskArtifactUpdateEvent| T
    end

    subgraph CHAT[채팅 SSE]
        G["테넌트 가드\n(JWT 검증)"] --> S["POST /chat/stream"]
        S --> X
        X -->|토큰·done| R["런 레지스트리"]
        R -->|스냅샷 + 실시간| A["POST /chat/stream/attach"]
        R --> CH
        K["POST /chat/stop"] -->|취소| X
    end

    classDef db fill=#f2f2f2,stroke=#333,stroke-width=1px;
```

- **todolist**: 각 작업(Task)의 진행 상태, 결과물 저장  
- **events**: 실행 중간에 발생한 이벤트 로그 저장  
- **chats**: 채팅 턴의 최종 응답 저장  
- SDK는 세 테이블을 자동으로 연결해 줍니다.  
- 채팅 경로는 요청이 Executor 에 닿기 전에 테넌트를 검증하고, 턴이 내보내는
  이벤트를 런 레지스트리에 남겨 재접속·중지가 가능하게 합니다.  

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
- **채팅 부가 라우트**: `/chat/stream/attach`(재접속) · `/chat/stop`(중지)

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
        # 요청 본문의 tenant_id 를 요청자의 JWT 로 검증한다(기본값). 4.6 참고.
        tenant_auth=True,
    )

    app = Starlette()
    # /chat/stream · /chat/stream/attach · /chat/stop 을 한 번에 붙이고,
    # tenant_auth 가 켜져 있으면 스트림 경로에 검증 미들웨어도 건다.
    server.mount_chat_routes(app)

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

> 스트림 하나만 필요하면 `mount_chat_sse(app, path="/chat/stream")` 를 그대로 쓸 수
> 있습니다(하트비트와 런 레지스트리는 이 경로에도 적용됩니다). 다만 재접속·중지
> 라우트와 테넌트 검증 미들웨어는 `mount_chat_routes()` 만 붙여 줍니다.

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
  -H 'authorization: Bearer <supabase-jwt>' \
  -d '{"message":"hello","conversation_id":"conv-1","tenant_id":"t1","user_uid":"u1"}'
```

`tenant_auth=True`(기본값) 면 `Authorization: Bearer …` 가 필요합니다. 토큰이 없으면
401, 본문의 `tenant_id` 가 그 사용자의 소속이 아니면 403 입니다. 본문에 `tenant_id` 를
넣지 않으면 검증된 값이 자동으로 채워집니다. 하위호환으로 본문 `user_jwt` 도 받습니다.

### 4.4 설치(옵션: SSE, 인증)

채팅(SSE)을 포함해 사용하려면 extras가 필요합니다.

```bash
pip install "process-gpt-agent-sdk[sse]"
```

테넌트 인증까지 쓰려면 JWT 검증용 extras를 함께 설치합니다.

```bash
pip install "process-gpt-agent-sdk[sse,auth]"
```

| extras | 들어오는 것 | 언제 필요한가 |
|---|---|---|
| `sse` | starlette, uvicorn | 채팅 라우트를 마운트할 때 |
| `auth` | pyjwt[crypto] | `tenant_auth=True` 로 둘 때 |

`auth` 는 함수 안에서 import 하므로, 인증을 끈 서버는 설치하지 않아도 기동에 영향이
없습니다(검증을 실제로 시도하는 순간 설치 안내와 함께 500 이 납니다).

참고로, 레포에는 빠르게 확인할 수 있는 샘플(`sample_server/minimal_server.py`, `sample_server/minimal_executor.py`)도 포함되어 있습니다.

### 4.5 채팅 전송 계층 (하트비트 · 재접속 · 중지)

`mount_chat_routes()` 를 쓰면 아래 세 가지가 자동으로 따라옵니다. Executor 는 바뀌지
않습니다 — 지금까지처럼 A2A 이벤트만 emit 하면 됩니다.

| 기능 | 무엇을 해결하나 |
|---|---|
| **하트비트** | 한 턴은 LLM 이 오래 생각하는 동안 수 분씩 아무 이벤트도 내보내지 않는다. 중간 프록시(Cloudflare 등)가 유휴 커넥션을 100초 안팎에서 끊으면 백엔드는 계속 도는데 화면만 "생각 중…" 에서 멈춘다. 15초마다 SSE 주석(`: keep-alive`)을 끼워 커넥션을 살려 둔다. 주석이라 프론트 파서(`data:` 만 처리)는 무시한다. |
| **재접속** | 새로고침하거나 방을 다시 열면 진행 중인 턴의 결과를 영영 못 받았다. `/chat/stream/attach` 가 지금까지 쌓인 본문을 `snapshot` 1건으로 주고 이후 토큰을 실시간으로 잇는다. |
| **중지** | 프론트의 중지 버튼은 자기 쪽 `fetch` 만 끊을 뿐이라 서버 실행은 계속 돌며 토큰·도구 호출을 그대로 소비했다. `/chat/stop` 이 실행 task 를 실제로 취소한다. |

여기에 더해, **같은 방에 새 턴이 들어오면 이전 턴을 먼저 끊습니다**. 프론트가 로딩 중
새 메시지를 보낼 때 서버에는 취소 신호를 주지 않아, 이게 없으면 같은
`conversation_id` 에 두 실행이 겹칩니다.

**재접속 요청**

```bash
curl -N -X POST http://127.0.0.1:8010/chat/stream/attach \
  -H 'content-type: application/json' \
  -H 'authorization: Bearer <supabase-jwt>' \
  -d '{"conversation_id":"conv-1","tenant_id":"t1"}'
```

활성 턴이 있으면 `text/event-stream` 으로 응답합니다.

```
event: message
data: {"type": "snapshot", "content": "지금까지 쓴 본문"}

event: message
data: {"type": "token", "content": "이어서"}
```

활성 턴이 없으면 SSE 가 아니라 **`200 {"active": false}`** 입니다. 404 가 아닌 이유는,
첫 attach 시도는 항상 "아직 스트림 없음" 이라 404 로 두면 메시지를 보낼 때마다 브라우저
네트워크 탭에 실패 요청이 쌓이기 때문입니다. 프론트는 `content-type` 이
`text/event-stream` 이 아니면 조용히 종료하면 됩니다.

**중지 요청**

```bash
curl -X POST http://127.0.0.1:8010/chat/stop \
  -H 'content-type: application/json' \
  -H 'authorization: Bearer <supabase-jwt>' \
  -d '{"conversation_id":"conv-1","tenant_id":"t1"}'
```

| 응답 | 의미 |
|---|---|
| `{"stopped": true}` | 진행 중이던 턴을 취소했다 |
| `{"stopped": false, "reason": "no_active_turn"}` | 취소할 실행이 없다(이미 끝났거나 HITL 대기 중) |
| `403 {"stopped": false, "reason": "forbidden"}` | 요청자의 테넌트와 방 소유 테넌트가 다르다 |

재접속·중지 모두 **방 소유 테넌트(`chat_rooms.tenant_id`)와 요청자의 테넌트가 같을 때만**
동작합니다. 방을 조회하지 못하면 거부합니다(fail-closed).

**하트비트 주기**는 `SSE_HEARTBEAT_SECONDS` 환경변수로 바꿉니다(기본 15초).

### 4.6 테넌트 인증

채팅 요청의 `tenant_id` 는 Executor 를 지나 스킬 디렉터리 로드·샌드박스 마운트·작업공간
경로까지 그대로 흘러갑니다. 검증하지 않으면 값만 바꿔 호출해 남의 테넌트 자원에 닿을 수
있습니다. 그래서 `tenant_auth` 의 기본값은 **켜짐**입니다.

```python
server = ProcessGPTAgentServer(
    agent_executor=MyExecutor(),
    agent_type="crewai-action",
    tenant_auth=True,   # 기본값
)
server.mount_chat_routes(app)
```

검증 경로는 토큰 헤더의 `alg` 에 따라 갈립니다.

1. **비대칭**(ES256/RS256 …) — Supabase JWKS(`/auth/v1/.well-known/jwks.json`) 로컬 검증.
   최신 Supabase 프로젝트(JWT signing keys)가 여기 해당합니다.
2. **HS256 + `SUPABASE_JWT_SECRET`** — 레거시 대칭키 프로젝트·커스텀 SSO 토큰 로컬 검증.
3. 둘 다 불가하면 **GoTrue `/auth/v1/user`** 에 위임.

소속 테넌트는 JWT 클레임(`tenant_id` / `app_metadata.tenant_id` / `tenant_ids`)을 먼저
보고, 없으면 `users` 테이블에서 조회합니다(멀티 테넌트 소속 대응). 검증 결과는 토큰
단위로 60초 캐시하고, 토큰 만료가 더 이르면 그 시점까지만 캐시합니다.

| 환경변수 | 쓰임 |
|---|---|
| `SUPABASE_URL` | JWKS 주소를 만든다(비대칭 검증) |
| `SUPABASE_JWT_SECRET` | HS256 대칭키 검증(있을 때만) |

**판정 규칙**

| 상황 | 결과 |
|---|---|
| 토큰 없음 | 401 |
| 요청한 `tenant_id` 가 소속이 아님 | 403 |
| 요청에 `tenant_id` 없고 소속이 하나 | 그 값으로 채워 통과 |
| 요청에 `tenant_id` 없고 소속이 여럿 | 400 (`tenant_id is required`) |
| 소속 테넌트가 하나도 없음 | 403 |

직접 만든 라우트에도 같은 규칙을 걸 수 있습니다.

```python
from processgpt_agent_sdk import tenant_guard, request_tenant_id

@tenant_guard
async def my_handler(request):
    # 요청이 보낸 값이 아니라 검증된 값만 쓴다
    tenant_id = request_tenant_id(request)
    ...
```

테넌트 스코프가 없는 엔드포인트는 `auth_guard` 로 인증만 확인합니다.

> **끄는 경우**: 앞단에 별도 인증 게이트웨이가 있거나 로컬 개발일 때만
> `tenant_auth=False` 로 둡니다. 이때는 기동 로그에 경고가 남고, 요청 본문의
> `tenant_id` 가 그대로 Executor 에 전달됩니다.

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
- **전송 계층**: 하트비트·재접속·중지·테넌트 검증은 프레임워크가 처리합니다. Executor 는 이들을 알 필요가 없습니다 (4.5 · 4.6)

### 6.1 프로세스(폴링)만 실행

```python
from processgpt_agent_sdk import ProcessGPTAgentServer

server = ProcessGPTAgentServer(agent_executor=MyExecutor(), agent_type="crewai-action")
await server.run()
```

### 6.2 채팅(SSE) 엔드포인트 추가

SSE를 쓰려면 extras 설치가 필요합니다.

```bash
pip install "process-gpt-agent-sdk[sse,auth]"
```

```python
from starlette.applications import Starlette

from processgpt_agent_sdk import ProcessGPTAgentServer

server = ProcessGPTAgentServer(agent_executor=MyExecutor(), agent_type="crewai-action")
app = Starlette()
server.mount_chat_routes(app)
# POST /chat/stream · /chat/stream/attach · /chat/stop
```

경로를 바꾸거나 일부만 붙일 수도 있습니다. 예전 프론트가 쓰던 별칭 경로가 있으면
`stream_paths` 에 함께 넘깁니다.

```python
server.mount_chat_routes(
    app,
    stream_paths=("/chat/stream", "/{agent_id}/chat/stream"),
    attach_path="/chat/stream/attach",   # None 이면 붙이지 않는다
    stop_path="/chat/stop",              # None 이면 붙이지 않는다
)
```

FastAPI 앱에도 그대로 넘길 수 있습니다. 라우트는 항상 Starlette 라우트로 등록되는데,
FastAPI 의 `add_api_route()` 에 넘기면 핸들러 시그니처(타입 주석 없는 `request`)를 필수
쿼리 파라미터로 해석해 모든 요청이 422 로 떨어지기 때문입니다.

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
    server.mount_chat_routes(app)

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

### 6.4 다중 파드 배포 (레지스트리 교체)

재접속·중지 레지스트리의 기본 구현은 **프로세스 로컬 dict** 입니다(단일 uvicorn 워커
전제). 파드를 여러 개 띄우면 "A 파드가 돌리는 턴에 B 파드가 받은 attach 요청" 이
맞지 않으므로, 공유 백엔드 구현으로 갈아끼웁니다. SDK 는 계약만 소유하고 공유 상태
백엔드는 애플리케이션이 고릅니다.

```python
from processgpt_agent_sdk import (
    ChatRunRegistry,
    set_run_registry,
    set_inflight_registry,
)

class RedisRunRegistry(ChatRunRegistry):
    async def start_run(self, conversation_id): ...
    async def record(self, conversation_id, payload): ...
    async def mark_done(self, conversation_id): ...
    async def subscribe(self, conversation_id): ...
    async def unsubscribe(self, conversation_id, q): ...

set_run_registry(RedisRunRegistry())
```

`asyncio.Task.cancel()` 은 그 task 를 만든 프로세스 안에서만 가능하므로, 중지의 경우
공유 백엔드는 "이 방을 어느 파드가 들고 있는지" 소유권 기록과 취소 신호 전달에만 쓰고
취소 자체는 항상 소유 파드에서 일어나야 합니다.

**supersede 와 cancel 은 다릅니다.** `InflightRegistry` 는 두 메서드를 따로 둡니다.

| 메서드 | 언제 불리나 | 기본 동작 |
|---|---|---|
| `supersede(cid)` | 같은 방에 새 턴이 시작될 때 | `cancel()` 을 부른다 |
| `cancel(cid)` | `/chat/stop` 으로 명시적 중지할 때 | 실행 task 를 취소한다 |

새 요청이 이전 응답을 **대체**하는지 **이어가는지**는 서버마다 다릅니다. 대표적으로
HITL(사람 확인) 응답은 이전 턴이 남긴 interrupt 를 재개하는 것이라, 그 턴을 취소하면
체크포인트가 사라져 재개가 불가능해집니다. 어느 쪽인지는 요청 본문을 해석해야 알 수
있고 그건 Executor 의 몫이므로, 그런 서버는 `supersede()` 를 no-op 으로 재정의하고
Executor 안에서 직접 판단합니다.

```python
from processgpt_agent_sdk import InflightRegistry, set_inflight_registry

class ExecutorDecides(InflightRegistry):
    async def supersede(self, conversation_id):
        return False   # 대체/이어가기 판단은 Executor 가 한다

set_inflight_registry(ExecutorDecides())
```

## 7. 버전업
- ./release.sh 버전
- 오류 발생시 : python -m ensurepip --upgrade

## 8. integrations 모듈 안내
- 스토리지 업로드 유틸은 `processgpt_agent_sdk.integrations.storage` 로 분리되었습니다.
- 기존 `processgpt_agent_sdk.utils.upload_file_to_bucket`, `upload_files_to_bucket` 는 하위호환용으로 유지되지만 deprecated 입니다.
- 신규 코드는 아래 경로를 사용하세요:
  - `from processgpt_agent_sdk.integrations.storage import upload_file_to_bucket, upload_files_to_bucket`

### 8.1 채팅 전송 계층 모듈 (0.5.0 추가)

| 모듈 | 들어 있는 것 |
|---|---|
| `processgpt_agent_sdk.tenant_auth` | `authorize_tenant`, `tenant_guard`, `auth_guard`, `request_tenant_id`, `ChatTenantGuardMiddleware`, `TenantAuthError` |
| `processgpt_agent_sdk.chat_registry` | `ChatRunRegistry`, `InflightRegistry`, `set_run_registry`, `set_inflight_registry` |
| `processgpt_agent_sdk.chat_sse` | `with_heartbeat`, `apply_heartbeat`, `format_sse_message`, `make_attach_handler`, `make_stop_handler` |

셋 다 최상위(`from processgpt_agent_sdk import ...`)로도 노출됩니다. 대부분의 경우
`mount_chat_routes()` 하나면 충분하고, 이 모듈들은 라우트를 직접 조립하거나 레지스트리를
교체할 때만 씁니다.