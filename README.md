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

### Event Type (4가지)
| Event Type | Python 클래스 | 저장 테이블 | 설명 |
|------------|---------------|-------------|------|
| **task_started** | `TaskStatusUpdateEvent` | `events` | 작업 시작 상태 |
| **task_working** | `TaskStatusUpdateEvent` | `events` | 작업 진행 중 상태 |
| **task_completed** | `TaskArtifactUpdateEvent` | `todolist` | 작업 완료 및 결과물 저장 |
| **task_error** | `TaskStatusUpdateEvent` | `events` | 작업 오류 발생 |

👉 **A2A 타입 2가지**가 핵심이며, 각각 `events`와 `todolist` 테이블에 매칭됩니다. **Event Type 4가지**로 세부 상태를 구분합니다.

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

### 4.2 Executor 구현 예시 (채팅 저장 payload 커스텀)

```python
from a2a.helpers import new_text_message
from a2a.types import Role
from processgpt_agent_sdk.chat_mode import ChatRequestContext


class MyExecutor(...):
    async def execute(self, context, event_queue):
        # 채팅(SSE) 요청이면 Message-only로 응답하고,
        # Message.metadata.chat_payload를 chats.messages에 그대로 저장합니다.
        if isinstance(context, ChatRequestContext):
            text = f"[chat] {context.get_user_input()}"
            msg = new_text_message(text=text, role=Role.ROLE_AGENT)
            msg.metadata.update(
                {
                    "chat_payload": {
                        # 이 payload는 외부 서비스가 원하는 형태로 자유롭게 구성하세요.
                        "role": "assistant",
                        "content": text,
                        "conversation_id": context.req.conversation_id,
                        "tenant_id": context.req.tenant_id,
                        "user_uid": context.req.user_uid,
                    }
                }
            )
            await event_queue.enqueue_event(msg)
            return

        # 그 외(폴링)는 Task lifecycle 패턴으로 처리 (Task → status/artifact)
        ...
```

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

핵심은 사용자 `AgentExecutor.execute()`가 **요청 경로에 따라** 아래 둘 중 하나를 선택하는 것입니다.

- **프로세스(폴링) 경로**: `Task lifecycle` (Task → status/artifact)
  - 첫 이벤트는 반드시 `Task`
  - 이후 `TaskStatusUpdateEvent` / `TaskArtifactUpdateEvent`만
- **채팅(SSE) 경로**: `Message-only` (Message 1개)
  - 정확히 1개의 `Message`만
  - status/artifact/Task를 섞지 않음

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