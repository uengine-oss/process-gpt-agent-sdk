import os
import logging
import traceback
from typing import Any, Dict, Optional, List
from llm_factory import create_llm


logger = logging.getLogger(__name__)


# ─────────────────────────────
# Lazy Singleton LLM Client
# ─────────────────────────────
_client = None
_global_agent_model = None

def set_agent_model(agent: Optional[Dict[str, Any]]) -> None:
    """첫 번째 에이전트의 모델을 글로벌 변수에 설정합니다."""
    global _global_agent_model
    
    if agent:
        model = agent.get("model")
        if model:
            # "openai/gpt-4o" 형식을 파싱하여 provider와 model 분리
            if "/" in model:
                provider, model_name = model.split("/", 1)
                _global_agent_model = {"provider": provider, "model": model_name}
                logger.info("🤖 글로벌 에이전트 모델 설정: %s/%s", provider, model_name)
            else:
                # 벤더사명이 없으면 모델명만 저장
                _global_agent_model = {"provider": None, "model": model}
                logger.info("🤖 글로벌 에이전트 모델 설정: %s", model)
        else:
            _global_agent_model = None
            logger.info("🤖 에이전트에 모델 정보가 없음")
    else:
        _global_agent_model = None
        logger.info("🤖 에이전트가 없음")

def get_agent_model() -> Optional[str]:
    """글로벌 에이전트 모델 정보를 반환합니다."""
    return _global_agent_model

def get_client():
    global _client
    if _client is not None:
        return _client
    
    # 글로벌 에이전트 모델로 클라이언트 생성
    agent_model = get_agent_model()
    if agent_model:
        provider = agent_model["provider"]
        model_name = agent_model["model"]
        
        if provider:
            # 벤더사가 있으면 provider와 model 모두 전달
            _client = create_llm(provider=provider, model=model_name, temperature=0)
            logger.info("🤖 글로벌 에이전트 모델로 LLM 클라이언트 초기화: %s/%s", provider, model_name)
        else:
            # 벤더사가 없으면 model만 전달
            _client = create_llm(model=model_name, temperature=0)
            logger.info("🤖 글로벌 에이전트 모델로 LLM 클라이언트 초기화: %s", model_name)
    else:
        _client = create_llm(model="gpt-4.1-mini", temperature=0)
        logger.info("🔧 기본 모델로 LLM 클라이언트 초기화: gpt-4.1-mini")
    return _client

# ─────────────────────────────
# 공통 LLM 호출 유틸
# ─────────────────────────────
async def _llm_request(system: str, user: str) -> str:
    logger.info("📡 LLM 요청 전송")

    model = get_client()
    
    # llm_factory를 사용한 LLM 호출
    messages = [
        {"role": "system", "content": system},
        {"role": "user", "content": user},
    ]
    
    response = await model.ainvoke(messages)
    
    # 응답에서 텍스트 추출
    if hasattr(response, 'content'):
        text = response.content
    elif isinstance(response, str):
        text = response
    else:
        raise RuntimeError("No text in LLM response")

    return text.strip()

# ─────────────────────────────
# 공개 API
# ─────────────────────────────
async def summarize_error_to_user(exc: Exception, meta: Dict[str, Any]) -> str:
    """예외 정보를 바탕으로 사용자 친화적인 5줄 요약을 생성."""
    logger.info("\n\n🔍 오류 컨텍스트 분석 시작")

    err_text = f"{type(exc).__name__}: {str(exc)}"

    # 가벼운 스택 문자열 (상위 3프레임)
    try:
        tb = "".join(traceback.TracebackException.from_exception(exc, limit=3).format())
    except Exception:
        tb = traceback.format_exc(limit=3)

    meta_items: List[str] = []
    for k in ("task_id", "proc_inst_id", "agent_orch", "tool"):
        v = meta.get(k)
        if v:
            meta_items.append(f"{k}={v}")
    meta_text = ", ".join(meta_items)

    logger.info("📋 오류 컨텍스트 정리 완료 - %s", meta_text)

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
        text = await _llm_request(system, user)
        logger.info("✅ LLM 오류 요약 생성 완료")
        return text
    except Exception as e:
        logger.warning("⚠️ LLM 오류 요약 생성 실패: %s", e, exc_info=True)
        raise

async def summarize_feedback(feedback_data: List[dict], content_data: dict = {}) -> str:
    """피드백과 결과물을 바탕으로 통합된 피드백 요약을 생성."""
    logger.info(
        "🔍 피드백 요약 처리 시작 | 피드백: %s, 결과물: %s자",
        feedback_data, content_data)

    system_prompt = _get_feedback_system_prompt()
    user_prompt = _create_feedback_summary_prompt(feedback_data, content_data)

    try:
        text = await _llm_request(system_prompt, user_prompt)
        logger.info("✅ LLM 피드백 요약 생성 완료")
        return text
    except Exception as e:
        logger.error("❌ LLM 피드백 요약 생성 실패: %s", e, exc_info=True)
        raise

# ─────────────────────────────
# 프롬프트 유틸
# ─────────────────────────────
def _create_feedback_summary_prompt(feedback_data: List[dict], content_data: dict = {}) -> str:
    """피드백 정리 프롬프트 - 현재 결과물과 피드백을 함께 분석"""
    blocks: List[str] = ["다음은 사용자의 피드백과 결과물입니다. 이를 분석하여 통합된 피드백을 작성해주세요:"]
    if feedback_data:
        blocks.append(f"=== 피드백 내용 ===\n{feedback_data}")
    if content_data:
        blocks.append(f"=== 현재 결과물/작업 내용 ===\n{content_data}")

    blocks.append(
        """**상황 분석 및 처리 방식:**
- **현재 결과물을 보고 어떤 점이 문제인지, 개선이 필요한지 판단**
- 피드백이 있다면 그 의도와 요구사항을 정확히 파악
- 결과물 자체가 마음에 안들어서 다시 작업을 요청하는 경우일 수 있음
- 작업 방식이나 접근법이 잘못되었다고 판단하는 경우일 수 있음
- 부분적으로는 좋지만 특정 부분의 수정이나 보완이 필요한 경우일 수 있음
- 현재 결과물에 매몰되지 말고, 실제 어떤 부분이 문제인지 파악하여 개선 방안을 제시

**피드백 통합 원칙:**
- **가장 최신 피드백을 최우선으로 반영**
- 결과물과 피드백을 종합적으로 분석하여 핵심 문제점 파악
- **시간 흐름을 파악하여 피드백들 간의 연결고리와 문맥을 이해**
- 구체적이고 실행 가능한 개선사항 제시
- **자연스럽고 통합된 하나의 완전한 피드백으로 작성**
- 최대 1000자까지 허용하여 상세히 작성
- 만약 전달된 피드백 내용이 1000자 미만일 경우 요약하지 않고 하나의 문맥으로 그대로 반환

**중요한 상황별 처리:**
- 결과물 품질에 대한 불만 → **품질 개선** 요구
- 작업 방식에 대한 불만 → **접근법 변경** 요구
- 이전에 저장을 했는데 잘못 저장되었다면 → **수정**이 필요
- 이전에 조회만 했는데 저장이 필요하다면 → **저장**이 필요
- 부분적 수정이 필요하다면 → **특정 부분 개선** 요구

출력 형식: 현재 상황을 종합적으로 분석한 완전한 피드백 문장 (다음 작업자가 즉시 이해하고 실행할 수 있도록)"""
    )
    return "\n\n".join(blocks)

def _get_feedback_system_prompt() -> str:
    """피드백 요약용 시스템 프롬프트"""
    return """당신은 피드백 정리 전문가입니다.

핵심 원칙:
- 최신 피드백을 최우선으로 하여 시간 흐름을 파악
- 피드백 간 문맥과 연결고리를 파악하여 하나의 완전한 요청으로 통합
- 자연스럽고 통합된 피드백으로 작성
- 구체적인 요구사항과 개선사항을 누락 없이 포함
- 다음 작업자가 즉시 이해할 수 있도록 명확하게"""
