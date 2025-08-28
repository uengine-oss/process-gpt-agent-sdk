# ProcessGPT Agent Simulator

ProcessGPT Agent Simulator는 데이터베이스 연결 없이 ProcessGPT 에이전트를 시뮬레이션할 수 있는 도구입니다. 개발, 테스트, 데모 목적으로 사용할 수 있습니다.

## 🎯 주요 기능

- **데이터베이스 불필요**: Supabase 연결 없이 시뮬레이션 실행
- **CLI 인터페이스**: 명령줄에서 간단하게 실행
- **실시간 이벤트 출력**: 진행 상태를 JSON 형태로 stdout에 출력
- **사용자 정의 실행기**: 자체 비즈니스 로직 구현 가능
- **다양한 시뮬레이션 모드**: 단계별 진행, 지연 시간 조정 등

## 🚀 빠른 시작

### 1. 기본 사용법

```bash
# 간단한 시뮬레이션 실행
python processgpt_simulator_cli.py "데이터를 분석해주세요"

# 또는 shell script 사용
./simulate.sh "보고서를 작성해주세요"
```

### 2. 고급 옵션

```bash
# 단계 수와 지연 시간 조정
python processgpt_simulator_cli.py "프로젝트를 계획해주세요" --steps 8 --delay 0.5

# 에이전트 타입과 활동 이름 지정
python processgpt_simulator_cli.py "고객 문의를 처리해주세요" \
  --agent-orch "customer_service" \
  --activity-name "ticket_processing"

# 상세 로그 출력
python processgpt_simulator_cli.py "분석 작업" --verbose
```

## 📋 CLI 옵션

| 옵션 | 설명 | 기본값 |
|------|------|--------|
| `prompt` | 에이전트가 처리할 프롬프트 메시지 | (필수) |
| `--agent-orch` | 에이전트 오케스트레이션 타입 | `simulator` |
| `--activity-name` | 활동 이름 | `simulation_task` |
| `--user-id` | 사용자 ID | 자동 생성 |
| `--tenant-id` | 테넌트 ID | 자동 생성 |
| `--tool` | 사용할 도구 | `default` |
| `--feedback` | 피드백 메시지 | (빈 문자열) |
| `--steps` | 시뮬레이션 단계 수 | `5` |
| `--delay` | 각 단계별 대기 시간(초) | `1.0` |
| `--verbose` | 상세한 로그 출력 | `false` |

## 📊 출력 형태

시뮬레이터는 각 이벤트를 JSON 형태로 stdout에 출력합니다:

```json
[EVENT] {
  "timestamp": "2024-01-15T10:30:45.123456Z",
  "task_id": "550e8400-e29b-41d4-a716-446655440000",
  "proc_inst_id": "550e8400-e29b-41d4-a716-446655440001",
  "event": {
    "type": "progress",
    "data": {
      "step": 2,
      "total_steps": 5,
      "message": "단계 2/5: 작업 처리 중...",
      "progress_percentage": 40.0
    }
  }
}
```

### 이벤트 타입

- `task_started`: 작업 시작
- `progress`: 진행 상황 업데이트
- `output`: 중간/최종 결과 출력
- `done`: 작업 완료
- `cancelled`: 작업 취소
- `error`: 오류 발생

## 🔧 사용자 정의 실행기

자체 비즈니스 로직을 구현하려면 `AgentExecutor`를 상속받아 사용자 정의 실행기를 만들 수 있습니다:

```python
from a2a.server.agent_execution import AgentExecutor, RequestContext
from a2a.server.events import EventQueue, Event
from processgpt_agent_sdk.simulator import ProcessGPTAgentSimulator

class MyCustomExecutor(AgentExecutor):
    async def execute(self, context: RequestContext, event_queue: EventQueue) -> None:
        # 사용자 정의 로직 구현
        prompt = context.get_user_input()
        
        # 이벤트 발생
        event = Event(
            type="custom_event",
            data={"message": f"처리 중: {prompt}"}
        )
        event_queue.enqueue_event(event)
        
        # 비즈니스 로직 수행...
        
    async def cancel(self, context: RequestContext, event_queue: EventQueue) -> None:
        # 취소 로직 구현
        pass

# 시뮬레이터에서 사용
executor = MyCustomExecutor()
simulator = ProcessGPTAgentSimulator(executor=executor)
await simulator.run_simulation("테스트 프롬프트")
```

## 📁 예제

### 1. 기본 시뮬레이션 예제

```bash
# examples/custom_executor_example.py 실행
python examples/custom_executor_example.py
```

이 예제는 다음과 같은 비즈니스 프로세스를 시뮬레이션합니다:
- 데이터 분석
- 보고서 작성
- 고객 서비스
- 프로젝트 관리

### 2. 실제 사용 시나리오

```bash
# 데이터 분석 시뮬레이션
./simulate.sh "월별 매출 데이터를 분석하고 트렌드를 파악해주세요" \
  --agent-orch "data_analyst" \
  --steps 6 \
  --delay 2.0

# 고객 서비스 시뮬레이션
./simulate.sh "제품 반품 문의에 대한 응답을 준비해주세요" \
  --agent-orch "customer_service" \
  --activity-name "return_inquiry" \
  --feedback "고객은 배송 지연을 이유로 반품을 요청했습니다"

# 프로젝트 관리 시뮬레이션
./simulate.sh "신제품 출시를 위한 프로젝트 계획을 수립해주세요" \
  --agent-orch "project_manager" \
  --steps 8 \
  --delay 1.5
```

## 🔍 로그 및 디버깅

### 로그 레벨 조정

환경 변수 `LOG_SPACED`를 설정하여 로그 형태를 조정할 수 있습니다:

```bash
# 로그 간격 제거
LOG_SPACED=0 python processgpt_simulator_cli.py "테스트"

# 상세 로그 출력
python processgpt_simulator_cli.py "테스트" --verbose
```

### 이벤트 필터링

이벤트 출력을 필터링하려면 `jq`를 사용할 수 있습니다:

```bash
# 진행 상황 이벤트만 출력
./simulate.sh "테스트" | grep '\[EVENT\]' | jq '.event | select(.type == "progress")'

# 최종 결과만 출력
./simulate.sh "테스트" | grep '\[EVENT\]' | jq '.event | select(.type == "output")'
```

## 🎛️ 고급 설정

### 시뮬레이션 데이터 커스터마이징

`ProcessGPTAgentSimulator` 클래스를 상속받아 모킹 데이터를 커스터마이징할 수 있습니다:

```python
class CustomSimulator(ProcessGPTAgentSimulator):
    def _prepare_mock_service_data(self, task_record):
        # 기본 데이터 가져오기
        data = super()._prepare_mock_service_data(task_record)
        
        # 사용자 정의 에이전트 추가
        data["agent_list"].append({
            "id": "custom-agent-id",
            "name": "specialized_agent",
            "role": "Domain Expert",
            # ... 기타 필드
        })
        
        return data
```

### 환경별 설정

개발, 스테이징, 프로덕션 환경에 따라 다른 설정을 사용할 수 있습니다:

```python
import os

# 환경별 시뮬레이션 설정
if os.getenv("ENV") == "development":
    simulation_steps = 3
    step_delay = 0.5
elif os.getenv("ENV") == "staging":
    simulation_steps = 5
    step_delay = 1.0
else:  # production
    simulation_steps = 10
    step_delay = 2.0
```

## 🤝 통합 방법

### CI/CD 파이프라인에서 사용

```yaml
# .github/workflows/test.yml
- name: Run Agent Simulation Tests
  run: |
    python processgpt_simulator_cli.py "테스트 시나리오 1" --steps 3 --delay 0.1
    python processgpt_simulator_cli.py "테스트 시나리오 2" --steps 3 --delay 0.1
```

### 다른 도구와 연동

```bash
# 결과를 파일로 저장
./simulate.sh "분석 작업" > simulation_output.log

# 결과를 다른 서비스로 전송
./simulate.sh "작업" | curl -X POST -H "Content-Type: application/json" \
  -d @- https://api.example.com/simulation-results
```

## 🔧 문제 해결

### 일반적인 문제

1. **Import 오류**: Python 경로가 올바르게 설정되었는지 확인
2. **Permission 오류**: `chmod +x simulate.sh`로 실행 권한 부여
3. **의존성 오류**: `pip install -r requirements.txt`로 의존성 설치

### 디버깅 팁

- `--verbose` 플래그 사용하여 상세 로그 확인
- 시뮬레이션 단계를 줄여서 빠른 테스트 수행
- 사용자 정의 실행기에서 예외 처리 추가

## 📚 추가 자료

- [ProcessGPT Agent Framework 문서](README.md)
- [A2A SDK 문서](https://github.com/your-org/a2a-sdk)
- [예제 코드](examples/)

## 🤝 기여하기

버그 리포트, 기능 요청, 또는 코드 기여를 환영합니다. 이슈를 생성하거나 풀 리퀘스트를 제출해 주세요.

## 📄 라이선스

MIT 라이선스 하에 배포됩니다. 자세한 내용은 [LICENSE](LICENSE) 파일을 참조하세요.
