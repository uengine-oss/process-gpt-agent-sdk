import logging
import os
from typing import Dict, List

import litellm


logger = logging.getLogger(__name__)
_llm_proxy_config_logged = False


def _openai_api_base_from_proxy_url(proxy_url: str) -> str:
    """프록시 URL을 OpenAI 클라이언트용 api_base(/v1 포함)로 정규화."""
    base = proxy_url.rstrip("/")
    if base.endswith("/v1"):
        return base
    return f"{base}/v1"


async def completion_text(
    messages: List[Dict[str, str]], *, temperature: float = 0
) -> str:
    """
    LiteLLM 프록시(OpenAI 호환)로 요청을 보내고 텍스트를 반환합니다.

    Required env vars:
    - LLM_MODEL
    - LLM_PROXY_URL
    - LLM_PROXY_API_KEY
    """
    global _llm_proxy_config_logged
    model = os.environ.get("LLM_MODEL")
    proxy_url = os.environ.get("LLM_PROXY_URL")
    api_key = os.environ.get("LLM_PROXY_API_KEY")
    missing = [
        n
        for n, v in (
            ("LLM_MODEL", model),
            ("LLM_PROXY_URL", proxy_url),
            ("LLM_PROXY_API_KEY", api_key),
        )
        if not v
    ]
    if missing:
        raise RuntimeError(
            "LLM 호출을 위해 다음 환경변수가 필요합니다: " + ", ".join(missing)
        )
    if not _llm_proxy_config_logged:
        logger.info("🤖 LLM 프록시 (url=%s, model=%s)", proxy_url, model)
        _llm_proxy_config_logged = True

    response = await litellm.acompletion(
        model=model,
        messages=messages,
        temperature=temperature,
        api_base=_openai_api_base_from_proxy_url(proxy_url),
        api_key=api_key,
    )
    try:
        return response.choices[0].message["content"]
    except Exception:
        return str(response)
