import os
import traceback
import logging
from typing import Any, Dict

# OpenAI 호환 엔드포인트 사용 (환경변수 기반)
# OPENAI_API_KEY, OPENAI_BASE_URL(required if not default)
try:
    from openai import OpenAI
except Exception:  # 라이브러리 미설치/호환 환경 대비
    OpenAI = None  # type: ignore

logger = logging.getLogger(__name__)


async def summarize_error_to_user(exc: Exception, meta: Dict[str, Any]) -> str:
    """
    예외 정보를 바탕으로 사용자 친화적인 5줄 요약을 생성.
    - 모델: gpt-4.1-nano (요청사항 반영)
    - 실패 시 Fallback: 간단한 수동 요약문
    """
    # 오류 컨텍스트 정리
    logger.info("🔍 오류 컨텍스트 분석 중...")
    err_text = f"{type(exc).__name__}: {str(exc)}"
    tb = traceback.format_exc(limit=3)
    meta_lines = [
        f"task_id={meta.get('task_id')}",
        f"proc_inst_id={meta.get('proc_inst_id')}",
        f"agent_orch={meta.get('agent_orch')}",
        f"tool={meta.get('tool')}",
    ]
    meta_text = ", ".join([x for x in meta_lines if x])
    logger.info("📋 오류 컨텍스트 분석 완료 - %s", meta_text)

    system = (
        "당신은 엔터프라이즈 SDK의 오류 비서입니다. "
        "사용자(비개발자도 이해 가능)를 위해, 아래 조건을 정확히 지켜 5줄로 한국어 설명을 만드세요.\n"
        "형식: 각 줄은 1문장씩, 총 5줄.\n"
        "포함 요소: ①무슨 문제인지(원인 추정) ②어떤 영향이 있는지 ③즉시 할 일(대처) "
        "④재발 방지 팁 ⑤필요시 지원 요청 경로.\n"
        "과장 금지, 간결하고 친절하게."
    )
    user = (
        f"[오류요약대상]\n"
        f"- 컨텍스트: {meta_text}\n"
        f"- 에러: {err_text}\n"
        f"- 스택(상위 3프레임):\n{tb}\n"
        f"위 정보를 바탕으로 5줄 설명을 출력하세요."
    )

    try:
        if OpenAI is None:
            logger.warning("⚠️ OpenAI SDK 사용 불가 - Fallback 모드로 전환")
            raise RuntimeError("OpenAI SDK not available")

        logger.info("🤖 OpenAI 클라이언트 초기화 중...")
        client = OpenAI(
            base_url=os.getenv("OPENAI_BASE_URL", "https://api.openai.com/v1"),
            api_key=os.getenv("OPENAI_API_KEY", ""),
        )
        
        model_name = os.getenv("ERROR_SUMMARY_MODEL", "gpt-4.1-nano")
        logger.info("📡 LLM 요청 전송 중... (모델: %s)", model_name)
        
        # responses API (신규 SDK)
        resp = client.responses.create(
            model=model_name,
            input=[{"role": "system", "content": system},
                   {"role": "user", "content": user}],
        )
        
        logger.info("🔍 LLM 응답 분석 중...")
        # 텍스트 추출(호환성 고려)
        text = None
        try:
            text = resp.output_text  # type: ignore[attr-defined]
        except Exception:
            # 다른 필드 구조 호환
            if hasattr(resp, "choices") and resp.choices:
                text = getattr(resp.choices[0].message, "content", None)  # type: ignore
        if not text:
            raise RuntimeError("No text in LLM response")
        
        logger.info("✅ LLM 오류 요약 생성 완료")
        return text.strip()

    except Exception as e:
        logger.warning("⚠️ LLM 오류 요약 생성 실패: %s - Fallback 모드로 전환", str(e))
        # Fallback: 간단 5줄
        logger.info("📝 Fallback 오류 요약 생성 중...")
        
        fallback_text = (
            "1) 처리 중 알 수 없는 오류가 발생했어요(환경/입력 값 문제일 수 있어요).\n"
            "2) 작업 결과가 저장되지 않았거나 일부만 반영됐을 수 있어요.\n"
            "3) 입력 값과 네트워크 상태를 확인하고, 다시 시도해 주세요.\n"
            "4) 같은 문제가 반복되면 로그와 설정(키/URL/권한)을 점검해 주세요.\n"
            "5) 계속되면 관리자나 운영팀에 문의해 원인 분석을 요청해 주세요."
        )
        logger.info("✅ Fallback 오류 요약 생성 완료")
        return fallback_text
