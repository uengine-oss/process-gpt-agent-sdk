import os
import logging
import traceback
import uuid
from typing import Any, Dict, Optional, List, Union, BinaryIO
from pathlib import Path
import litellm


logger = logging.getLogger(__name__)


# ─────────────────────────────
# LLM Client (litellm 기반)
# ─────────────────────────────
_client = None
_global_agent_model = None


class LiteLLMClient:
    def __init__(self, model: str, temperature: float = 0, provider: Optional[str] = None) -> None:
        self.model = model
        self.temperature = temperature
        self.provider = provider

    async def ainvoke(self, messages: List[Dict[str, str]]) -> str:
        model_name = f"{self.provider}/{self.model}" if self.provider else self.model
        response = await litellm.acompletion(
            model=model_name,
            messages=messages,
            temperature=self.temperature,
        )
        # OpenAI 스타일 응답에서 content 추출
        try:
            return response.choices[0].message["content"]
        except Exception:
            # 예상치 못한 포맷인 경우 문자열로 캐스팅
            return str(response)


def create_llm(provider: Optional[str] = None, model: str = "gpt-4.1-mini", temperature: float = 0) -> LiteLLMClient:
    return LiteLLMClient(model=model, temperature=temperature, provider=provider)

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

# ─────────────────────────────
# 파일 관리 유틸
# ─────────────────────────────
async def upload_file_to_bucket(
    file: BinaryIO,
    file_name: str,
    proc_inst_id: Optional[str] = None
) -> Dict[str, Any]:
    """
    파일을 Supabase Storage 버킷에 업로드합니다.
    
    Args:
        file: 업로드할 파일 객체 (BinaryIO) (필수)
        file_name: 파일명 (필수, Storage에 저장될 경로로 사용됨)
        proc_inst_id: 프로세스 인스턴스 ID (옵션)
        
    Note:
        Content-Type은 파일 확장자를 기반으로 자동 감지됩니다.
        파일은 "files" 버킷의 "uploads" 디렉토리에 file_name으로 저장됩니다.
    
    Returns:
        업로드 결과 딕셔너리:
        - success: 성공 여부 (bool)
        - storage_path: Storage에 저장된 파일 경로
        - public_url: 공개 URL (있는 경우)
        - error: 에러 메시지 (실패 시)
    
    Example:
        ```python
        from processgpt_agent_sdk.utils import upload_file_to_bucket
        
        # 파일 업로드
        with open("./document.pdf", "rb") as f:
            result = await upload_file_to_bucket(
                file=f,
                file_name="document.pdf",
                proc_inst_id="proc_inst_id_123"
            )
        
        if result["success"]:
            print(f"업로드 완료: {result['storage_path']}")
        ```
    """
    from .database import get_db_client
    import asyncio
    import mimetypes
    
    def _upload_file() -> Dict[str, Any]:
        """파일 업로드 (동기 함수)"""
        try:
            client = get_db_client()
            
            # 파일 크기 확인을 위해 파일 데이터 읽기 (나중에 사용)
            current_pos = file.tell()
            file.seek(0, 2)  # 파일 끝으로 이동
            file_size = file.tell()
            file.seek(current_pos)  # 원래 위치로 복원
            
            # 파일명에 UUID 추가하여 중복 방지
            file_path = Path(file_name)
            file_stem = file_path.stem
            file_suffix = file_path.suffix
            unique_id = str(uuid.uuid4())[:8]  # UUID의 앞 8자리만 사용
            actual_file_name = f"{file_stem}_{unique_id}{file_suffix}"
            
            # Storage 경로는 uploads 디렉토리에 파일명으로 저장
            final_storage_path = f"uploads/{actual_file_name}"
            
            # Content-Type 자동 감지
            detected_content_type, _ = mimetypes.guess_type(actual_file_name)
            if not detected_content_type:
                detected_content_type = "application/octet-stream"
            
            # Storage에 업로드
            bucket_name = "files"
            logger.info("📤 업로드 중: %s (버킷: %s)", final_storage_path, bucket_name)
            
            storage_api = client.storage.from_(bucket_name)
            
            # 파일 포인터를 처음으로 이동
            file.seek(0)
            
            file_options = {"content-type": detected_content_type}
            
            response = storage_api.upload(
                path=final_storage_path,
                file=file,
                file_options=file_options
            )
            
            # 공개 URL 가져오기 (시도)
            public_url = None
            try:
                public_url = storage_api.get_public_url(final_storage_path)
            except Exception:
                pass  # 공개 URL이 없어도 계속 진행
            
            logger.info("✅ 업로드 완료: %s", final_storage_path)
            
            # 파일 크기는 이미 업로드 전에 계산했으므로 그대로 사용
            result = {
                "success": True,
                "storage_path": final_storage_path,
                "file_name": actual_file_name,
                "content_type": detected_content_type,
                "size": file_size
            }
            
            if public_url:
                result["public_url"] = public_url
            
            return result
            
        except Exception as e:
            error_msg = f"파일 업로드 실패: {str(e)}"
            logger.error("❌ %s", error_msg, exc_info=e)
            return {
                "success": False,
                "error": error_msg
            }
    
    return await asyncio.to_thread(_upload_file)

async def upload_files_to_bucket(
    files: List[Dict[str, Any]],
    proc_inst_id: Optional[str] = None
) -> List[Dict[str, Any]]:
    """
    여러 파일을 Supabase Storage 버킷에 업로드합니다.
    
    Args:
        files: 업로드할 파일 정보 리스트. 각 항목은 다음 필드를 포함:
            - file: 파일 객체 (BinaryIO) (필수)
            - file_name: 파일명 (필수)
            - proc_inst_id: 프로세스 인스턴스 ID (옵션, 전체 기본값보다 우선)
        proc_inst_id: 프로세스 인스턴스 ID (옵션, 모든 파일에 적용, 개별 설정보다 우선순위 낮음)
        
    Note:
        모든 파일은 "files" 버킷에 업로드됩니다.
    
    Returns:
        업로드 결과 리스트 (각 항목은 upload_file_to_bucket의 반환 형식과 동일)
    
    Example:
        ```python
        from processgpt_agent_sdk.utils import upload_files_to_bucket
        
        files = [
            {"file": open("./doc1.pdf", "rb"), "file_name": "doc1.pdf"},
            {"file": open("./doc2.pdf", "rb"), "file_name": "doc2.pdf"},
        ]
        
        results = await upload_files_to_bucket(
            files=files,
            proc_inst_id="proc_inst_id_123"
        )
        
        for result in results:
            if result["success"]:
                print(f"✅ {result['storage_path']}")
            else:
                print(f"❌ {result['error']}")
        ```
    """
    import asyncio
    
    if not files:
        logger.info("📤 업로드할 파일이 없습니다")
        return []
    
    logger.info("📤 파일 업로드 시작: %d개 파일", len(files))
    
    # 각 파일에 대해 업로드 실행 (병렬 처리)
    upload_tasks = [
        upload_file_to_bucket(
            file=file_info.get("file"),
            file_name=file_info.get("file_name"),
            proc_inst_id=file_info.get("proc_inst_id", proc_inst_id)
        )
        for file_info in files
    ]
    
    results = await asyncio.gather(*upload_tasks)
    
    success_count = sum(1 for r in results if r.get("success"))
    logger.info("📤 업로드 완료: 성공 %d/%d", success_count, len(files))
    
    return results
