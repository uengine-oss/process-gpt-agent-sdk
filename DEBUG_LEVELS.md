# ProcessGPT 디버그 레벨 가이드

ProcessGPT 서버는 환경변수 `DEBUG_LEVEL`을 통해 디버그 로그의 출력 레벨을 제어할 수 있습니다.

## 디버그 레벨 설정

### 환경변수로 설정
```bash
# Windows PowerShell
$env:DEBUG_LEVEL="2"

# Linux/Mac
export DEBUG_LEVEL=2
```

### 런타임에 설정
```python
from processgpt_agent_sdk.utils.logger import set_debug_level, DEBUG_LEVEL_DETAILED
set_debug_level(DEBUG_LEVEL_DETAILED)
```

## 디버그 레벨 설명

### 0 - DEBUG_LEVEL_NONE
- 디버그 로그 없음
- 기본 INFO/ERROR 로그만 출력
- 프로덕션 환경에 적합

### 1 - DEBUG_LEVEL_BASIC (기본값)
- 핵심 디버그 정보만 출력
- 서버 초기화, 작업 시작/완료, 예외 발생 등
- 운영 환경에서 문제 추적에 유용

### 2 - DEBUG_LEVEL_DETAILED
- 상세한 디버그 정보 출력
- 데이터 준비 과정, 실행기 초기화, 태스크 생성 등
- 개발 및 테스트 환경에 적합

### 3 - DEBUG_LEVEL_VERBOSE
- 매우 상세한 디버그 정보 출력
- 폴링 루프, 취소 상태 확인, 이벤트 처리 등
- 디버깅 및 성능 분석에 유용

## 디버그 포인트별 레벨

### DEBUG_LEVEL_BASIC (레벨 1)
- DEBUG-001: 서버 초기화 완료
- DEBUG-004: 작업 레코드 수신
- DEBUG-007: 실행 및 취소 감시 시작
- DEBUG-008: 작업 완료 처리
- DEBUG-009: 작업 처리 중 예외 발생
- DEBUG-016: 취소 처리 시작
- DEBUG-019: 이벤트 큐 삽입 실패
- DEBUG-021: 이벤트 저장 전체 실패

### DEBUG_LEVEL_DETAILED (레벨 2)
- DEBUG-005: 서비스 데이터 준비 시작
- DEBUG-006: 준비된 데이터 요약
- DEBUG-010: 실행기 및 이벤트 큐 초기화 완료
- DEBUG-011: 비동기 태스크 생성 완료

### DEBUG_LEVEL_VERBOSE (레벨 3)
- DEBUG-002: 폴링 시작
- DEBUG-003: 대기 중인 작업 없음
- DEBUG-012: 태스크 대기 시작
- DEBUG-013: 태스크 완료 감지
- DEBUG-014: 대기 중인 태스크 취소
- DEBUG-015: 취소 상태 확인
- DEBUG-017: 이벤트 큐 삽입 시작
- DEBUG-018: 이벤트 큐 삽입 성공
- DEBUG-020: 백그라운드 이벤트 처리 태스크 생성

## 사용 예시

### 개발 환경 (상세한 로그)
```bash
export DEBUG_LEVEL=3
python your_script.py
```

### 운영 환경 (기본 로그)
```bash
export DEBUG_LEVEL=1
python your_script.py
```

### 로그 없음 (최소 로그)
```bash
export DEBUG_LEVEL=0
python your_script.py
```

## 성능 고려사항

- DEBUG_LEVEL_VERBOSE는 매우 많은 로그를 생성하므로 성능에 영향을 줄 수 있습니다
- 프로덕션 환경에서는 DEBUG_LEVEL_BASIC 이하를 권장합니다
- 로그 출력량이 많을 때는 로그 파일 크기 관리에 주의하세요
