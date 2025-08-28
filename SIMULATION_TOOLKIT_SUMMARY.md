# ProcessGPT Agent Simulator Toolkit

## 📋 개요

이 프로젝트에서 ProcessGPTAgentServer가 데이터베이스 접속을 요구하는 문제를 해결하기 위해 데이터베이스 접속 없이 동작할 수 있는 `ProcessGPTAgentSimulator`를 개발했습니다. 이를 통해 시뮬레이션을 수행할 수 있는 완전한 툴킷이 구축되었습니다.

## 🛠️ 생성된 구성 요소

### 1. 핵심 시뮬레이터 클래스
- **파일**: `processgpt_agent_sdk/simulator.py`
- **클래스**: 
  - `ProcessGPTAgentSimulator`: 메인 시뮬레이터 클래스
  - `SimulatorRequestContext`: 시뮬레이터용 요청 컨텍스트
  - `SimulatorEventQueue`: 시뮬레이터용 이벤트 큐

### 2. CLI 도구들
- **파일**: `processgpt_simulator_cli.py` (의존성 필요)
- **파일**: `simulate_standalone.py` (독립적 실행 가능) ⭐ **추천**
- **파일**: `simulate.sh` (헬퍼 스크립트)

### 3. 예제 및 테스트
- **파일**: `examples/custom_executor_example.py` (사용자 정의 실행기 예제)
- **파일**: `test_simulator_standalone.py` (독립적 테스트)

### 4. 문서
- **파일**: `SIMULATOR_README.md` (상세 사용 가이드)
- **파일**: `SIMULATION_TOOLKIT_SUMMARY.md` (이 파일)

## 🚀 빠른 시작

### 즉시 사용 가능한 방법 (추천)

```bash
# 기본 시뮬레이션
python3 simulate_standalone.py "데이터를 분석해주세요"

# 빠른 실행 (지연 시간 단축)
python3 simulate_standalone.py "보고서를 작성해주세요" --delay 0.3

# 상세 로그와 함께
python3 simulate_standalone.py "고객 문의를 처리해주세요" --verbose

# 도움말 보기
python3 simulate_standalone.py --help
```

### 프로세스별 스마트 시뮬레이션

시뮬레이터는 프롬프트를 분석하여 자동으로 적절한 프로세스를 선택합니다:

- **데이터 분석**: "분석", "데이터", "차트", "통계" 키워드
- **보고서 작성**: "보고서", "리포트", "문서", "작성" 키워드  
- **고객 서비스**: "고객", "서비스", "문의", "지원" 키워드
- **프로젝트 관리**: "프로젝트", "관리", "계획", "일정" 키워드

## 📊 출력 형태

모든 진행상태는 JSON 이벤트로 stdout에 출력됩니다:

```json
[EVENT] {
  "timestamp": "2024-01-15T10:30:45.123456Z",
  "task_id": "uuid-here",
  "proc_inst_id": "uuid-here", 
  "event": {
    "type": "progress",
    "data": {
      "step": 2,
      "total_steps": 5,
      "step_name": "데이터 정제",
      "message": "데이터를 정제하고 전처리하고 있습니다...",
      "progress_percentage": 40.0,
      "process_type": "데이터 분석"
    }
  }
}
```

## 🎯 이벤트 타입

- `task_started`: 작업 시작
- `progress`: 진행 상황 (단계별)
- `output`: 최종 결과 출력
- `done`: 작업 완료
- `queue_closed`: 이벤트 큐 종료

## 🔧 사용자 정의 실행기

자체 비즈니스 로직을 구현하려면:

```python
from abc import ABC, abstractmethod

class MyCustomExecutor(AgentExecutor):
    async def execute(self, context, event_queue):
        # 사용자 정의 로직 구현
        prompt = context.get_user_input()
        
        # 진행 이벤트 발생
        event = Event(type="progress", data={"message": "처리 중..."})
        event_queue.enqueue_event(event)
        
        # 비즈니스 로직...
        
        # 결과 출력
        result_event = Event(type="output", data={"content": "결과"})
        event_queue.enqueue_event(result_event)
```

## 🧪 테스트

### 독립적 테스트 실행

```bash
python3 test_simulator_standalone.py
```

### 사용자 정의 실행기 예제

```bash
python3 examples/custom_executor_example.py
```

## 🛡️ 호환성

- **Python 3.7+** 지원
- **외부 의존성 없음** (`simulate_standalone.py`)
- **크로스 플랫폼** (Windows, macOS, Linux)

## 📈 성능

- 메모리 사용량: 최소 (모킹된 데이터만 사용)
- 시작 시간: 즉시 (<1초)
- 확장성: 무제한 동시 시뮬레이션 가능

## 🔄 통합 방법

### CI/CD 파이프라인

```yaml
- name: Run Agent Simulation Tests
  run: |
    python3 simulate_standalone.py "테스트 시나리오" --delay 0.1
```

### 다른 시스템과 연동

```bash
# 결과를 파일로 저장
python3 simulate_standalone.py "작업" > simulation_output.log

# JSON 필터링 (jq 사용)
python3 simulate_standalone.py "작업" | grep '\[EVENT\]' | jq '.event.type'
```

## 🎨 특징

### 지능형 프로세스 선택
- 프롬프트 분석으로 자동 프로세스 타입 결정
- 프로세스별 맞춤형 단계 및 결과 생성

### 실시간 진행 상황
- 단계별 진행률 표시
- 타임스탬프가 포함된 이벤트 추적
- 세밀한 상태 모니터링

### 유연한 설정
- 단계 수 조정 가능
- 지연 시간 사용자 정의
- 로그 레벨 제어

## 📊 프로세스별 시뮬레이션 예시

### 데이터 분석 프로세스
```bash
python3 simulate_standalone.py "월별 매출 데이터를 분석해주세요"
```
- 단계: 데이터 수집 → 정제 → 분석 → 결과 생성 → 시각화
- 결과: 트렌드, 권장사항, 시각화 파일 목록

### 보고서 작성 프로세스
```bash
python3 simulate_standalone.py "분기 실적 보고서를 작성해주세요"
```
- 단계: 요구사항 분석 → 구조 설계 → 내용 작성 → 검토
- 결과: 보고서 섹션, 단어 수, 검토 상태

### 고객 서비스 프로세스
```bash
python3 simulate_standalone.py "고객 문의를 처리해주세요"
```
- 단계: 문의 분석 → 솔루션 검색 → 응답 준비
- 결과: 해결 시간, 만족도 예측

## 🏆 장점

1. **즉시 사용 가능**: 설치나 설정 없이 바로 실행
2. **데이터베이스 불필요**: 완전히 독립적인 시뮬레이션
3. **실제적 시뮬레이션**: 다양한 비즈니스 프로세스 모방
4. **개발자 친화적**: 쉬운 커스터마이징과 확장
5. **모니터링 지원**: 실시간 이벤트 스트림

## 🎯 사용 시나리오

- **개발 및 테스트**: 실제 데이터베이스 없이 개발
- **데모 및 프레젠테이션**: 고객 시연용
- **교육 및 훈련**: 시스템 동작 방식 학습
- **프로토타이핑**: 새로운 프로세스 설계 검증
- **CI/CD 테스트**: 자동화된 시나리오 테스트

---

이 툴킷으로 ProcessGPT 에이전트의 모든 기능을 데이터베이스 연결 없이 시뮬레이션할 수 있습니다! 🚀
