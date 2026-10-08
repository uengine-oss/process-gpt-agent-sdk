#!/usr/bin/env bash
# 재개 샘플 e2e — PyPI 에서 받은 SDK 로 돌린다.
#
#   sample_server/e2e/run.sh            # process-gpt-agent-sdk==0.11.0
#   SDK_VERSION=0.11.1 sample_server/e2e/run.sh
#
# 필요: 로컬 Supabase(process-gpt-vue3) 가 떠 있고, SUPABASE_URL/SUPABASE_KEY 가
# 환경이나 저장소 .env 에 있다.
set -euo pipefail

HERE="$(cd "$(dirname "$0")" && pwd)"
SDK_VERSION="${SDK_VERSION:-0.11.0}"
VENV="$HERE/.venv-$SDK_VERSION"

if [[ ! -x "$VENV/bin/python" ]]; then
  python3 -m venv "$VENV"
fi
# 소스 경로(-e .)가 아니라 패키지 저장소의 배포본을 깐다.
"$VENV/bin/python" -m pip install -q --upgrade --no-cache-dir --index-url https://pypi.org/simple \
  "process-gpt-agent-sdk==$SDK_VERSION" pytest

# 저장소 밖에서 돌린다 — 저장소 루트가 sys.path 에 들어가 소스가 설치본을 가리지 않게.
cd "$HERE/.."
RESUME_SAMPLE_E2E=1 RESUME_SAMPLE_SDK_VERSION="$SDK_VERSION" \
  "$VENV/bin/pytest" -v -p no:cacheprovider --rootdir "$HERE" "$HERE/test_resume_e2e.py" "$@"
