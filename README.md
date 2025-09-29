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

### Event Type (4가지)
| Event Type | Python 클래스 | 저장 테이블 | 설명 |
|------------|---------------|-------------|------|
| **task_started** | `TaskStatusUpdateEvent` | `events` | 작업 시작 상태 |
| **task_working** | `TaskStatusUpdateEvent` | `events` | 작업 진행 중 상태 |
| **task_completed** | `TaskArtifactUpdateEvent` | `todolist` | 작업 완료 및 결과물 저장 |
| **task_error** | `TaskStatusUpdateEvent` | `events` | 작업 오류 발생 |

👉 **A2A 타입 2가지**가 핵심이며, 각각 `events`와 `todolist` 테이블에 매칭됩니다. **Event Type 4가지**로 세부 상태를 구분합니다.

---

## 4. 미니멀 예제 (기본 사용법)

### minimal_executor.py
```python
class MinimalExecutor(AgentExecutor):
    async def execute(self, context: RequestContext, event_queue: EventQueue):
        # 1) 입력 가져오기
        query = context.get_user_input()
        print("User Query:", query)

        # 2) 상태 이벤트 (events 테이블 저장)
        payload = {"demo": "hello world"}
        event_queue.enqueue_event(
            TaskStatusUpdateEvent(
                status={
                    "state": TaskState.working,
                    "message": new_agent_text_message(
                        json.dumps(payload, ensure_ascii=False),  # ⚠️ str() 쓰지말고 반드시 json.dumps!
                        context.get_context_data()["row"]["proc_inst_id"],
                        context.get_context_data()["row"]["id"],
                    ),
                },
                contextId=context.get_context_data()["row"]["proc_inst_id"],
                taskId=context.get_context_data()["row"]["id"],
                metadata={"crew_type": "action", "event_type": "task_started"},
            )
        )

        # 3) 최종 아티팩트 이벤트 (todolist 테이블 저장)
        artifact = new_text_artifact(
            name="result",
            description="Demo Result",
            text=json.dumps(payload, ensure_ascii=False),  # ⚠️ 여기서도 str() 금지!
        )
        event_queue.enqueue_event(
            TaskArtifactUpdateEvent(
                artifact=artifact,
                lastChunk=True,
                contextId=context.get_context_data()["row"]["proc_inst_id"],
                taskId=context.get_context_data()["row"]["id"],
            )
        )
```

### minimal_server.py
```python
async def main():
    load_dotenv()
    server = ProcessGPTAgentServer(
        agent_executor=MinimalExecutor(),
        agent_type="crewai-action"  # 오케스트레이터 타입
    )
    await server.run()
```

👉 실행하면 SDK가 자동으로:
1. DB에서 작업 하나 가져오기 (`fetch_pending_task`)  
2. 컨텍스트 준비 (폼/유저/MCP 조회)  
3. Executor 실행 → 이벤트/결과 DB에 저장  

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

## 6. 요약
- 이 SDK는 **ProcessGPT Agent**를 표준 규격으로 실행/저장/호출하는 공통 레이어  
- 작업 → 컨텍스트 준비 → Executor 실행 → 이벤트 저장 전체를 자동화  
- **A2A 타입 2가지**: `TaskStatusUpdateEvent`, `TaskArtifactUpdateEvent`  
- **Event Type 4가지**: `task_started`, `task_working`, `task_completed`, `task_error`  
- **DB 매핑**:  
  - `TaskStatusUpdateEvent` → `events` 테이블  
  - `TaskArtifactUpdateEvent` → `todolist` 테이블  
- ⚠️ **str() 대신 무조건 `json.dumps` 사용!**



## 7. 버전업
- ./release.sh 버전
- 오류 발생시 : python -m ensurepip --upgrade