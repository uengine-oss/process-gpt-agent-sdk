#!/bin/bash
# ProcessGPT Agent Simulator Helper Script
# 
# 사용법:
#   ./simulate.sh "프롬프트 메시지"
#   ./simulate.sh "데이터 분석해주세요" --agent-orch "data_analyst" --steps 3

# 스크립트 디렉토리로 이동
cd "$(dirname "$0")"

# Python 경로 확인
if command -v python3 &> /dev/null; then
    PYTHON=python3
elif command -v python &> /dev/null; then
    PYTHON=python
else
    echo "Error: Python이 설치되어 있지 않습니다."
    exit 1
fi

# CLI 도구 실행
$PYTHON processgpt_simulator_cli.py "$@"
