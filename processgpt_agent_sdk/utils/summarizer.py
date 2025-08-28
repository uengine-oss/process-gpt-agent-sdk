from __future__ import annotations


import os
import json
import asyncio
from typing import Any, Tuple

import openai
from .logger import handle_application_error, write_log_message

# =============================================================================
# 요약기(Summarizer)
# 설명: 출력/피드백/현재 내용으로부터 OpenAI를 사용해 간단 요약을 생성한다.
# =============================================================================

async def summarize_async(outputs: Any, feedbacks: Any, contents: Any = None) -> Tuple[str, str]:
	"""(output_summary, feedback_summary)를 비동기로 생성해 반환한다.
	키 없음/오류 시 빈 문자열 폴백, 취소는 상위로 전파."""
	outputs_str = _convert_to_string(outputs).strip()
	feedbacks_str = _convert_to_string(feedbacks).strip()
	contents_str = _convert_to_string(contents).strip()

	output_summary = ""
	feedback_summary = ""

	if outputs_str and outputs_str not in ("[]", "{}", "[{}]"):
		write_log_message("요약 호출(이전결과물)")
		output_prompt = _create_output_summary_prompt(outputs_str)
		output_summary = await _call_openai_api_async(output_prompt, task_name="output")

	if feedbacks_str and feedbacks_str not in ("[]", "{}"):
		write_log_message("요약 호출(피드백)")
		feedback_prompt = _create_feedback_summary_prompt(feedbacks_str, contents_str)
		feedback_summary = await _call_openai_api_async(feedback_prompt, task_name="feedback")

	return output_summary or "", feedback_summary or ""


# =============================================================================
# 헬퍼: 문자열 변환
# =============================================================================

def _convert_to_string(data: Any) -> str:
	"""임의 데이터를 안전하게 문자열로 변환한다."""
	if data is None:
		return ""
	if isinstance(data, str):
		return data
	try:
		return json.dumps(data, ensure_ascii=False)
	except Exception:
		return str(data)


# =============================================================================
# 헬퍼: 프롬프트 생성
# =============================================================================

def _create_output_summary_prompt(outputs_str: str) -> str:
	"""결과물 요약용 사용자 프롬프트를 생성한다."""
	return (
		"다음 작업 결과를 정리해주세요:\n\n"
		f"{outputs_str}\n\n"
		"처리 방식:\n"
		"- 짧은 내용은 요약하지 말고 그대로 유지 (정보 손실 방지)\n"
		"- 긴 내용만 적절히 요약하여 핵심 정보 전달\n"
		"- 수치, 목차, 인물명, 물건명, 날짜, 시간 등 객관적 정보는 반드시 포함\n"
		"- 왜곡이나 의미 변경 금지, 원본 의미 보존\n"
		"- 중복만 정리하고 핵심 내용은 모두 보존\n"
		"- 하나의 통합된 문맥으로 작성"
	)


def _create_feedback_summary_prompt(feedbacks_str: str, contents_str: str = "") -> str:
	"""피드백/현재 결과물을 통합 요약하는 사용자 프롬프트를 생성한다."""
	feedback_section = (
		f"=== 피드백 내용 ===\n{feedbacks_str}" if feedbacks_str and feedbacks_str.strip() else ""
	)
	content_section = (
		f"=== 현재 결과물/작업 내용 ===\n{contents_str}" if contents_str and contents_str.strip() else ""
	)
	return (
		"다음은 사용자의 피드백과 결과물입니다. 이를 분석하여 통합된 피드백을 작성해주세요:\n\n"
		f"{feedback_section}\n\n{content_section}\n\n"
		"상황 분석 및 처리 방식:\n"
		"- 현재 결과물을 보고 문제/개선 필요점 판단\n"
		"- 최신 피드백을 최우선으로 반영\n"
		"- 실행 가능한 개선사항 제시\n"
		"- 하나의 통합된 문장으로 작성 (최대 2500자)"
	)


# =============================================================================
# 헬퍼: 시스템 프롬프트 선택
# =============================================================================

def _get_system_prompt(task_name: str) -> str:
	"""작업 종류에 맞는 시스템 프롬프트를 반환한다."""
	if task_name == "feedback":
		return (
			"당신은 피드백 정리 전문가입니다. 최신 피드백을 우선 반영하고,"
			" 문맥을 연결하여 하나의 완전한 요청으로 통합해 주세요."
		)
	return (
		"당신은 결과물 요약 전문가입니다. 긴 내용만 요약하고,"
		" 수치/고유명/날짜 등 객관 정보를 보존해 주세요."
	)


# =============================================================================
# 외부 호출: OpenAI API
# =============================================================================

async def _call_openai_api_async(prompt: str, task_name: str) -> str:
	"""OpenAI 비동기 API를 호출해 요약 텍스트를 생성한다."""
	
	if not (os.getenv("OPENAI_API_KEY") or os.getenv("OPENAI_API_KEY_BETA")):
		write_log_message("요약 비활성화: OPENAI_API_KEY 미설정")
		return ""

	client = openai.AsyncOpenAI()
	system_prompt = _get_system_prompt(task_name)
	model = os.getenv("OPENAI_SUMMARY_MODEL", "gpt-4o-mini")

	for attempt in range(1, 4):
		try:
			resp = await client.chat.completions.create(
				model=model,
				messages=[
					{"role": "system", "content": system_prompt},
					{"role": "user", "content": prompt},
				],
				temperature=0.1,
				timeout=30.0,
			)
			return (resp.choices[0].message.content or "").strip()
		except asyncio.CancelledError:
			raise
		except Exception as e:
			if attempt < 3:
				handle_application_error("요약 호출 오류(재시도)", e, raise_error=False, extra={"attempt": attempt})
				await asyncio.sleep(0.8 * (2 ** (attempt - 1)))
				continue
			handle_application_error("요약 호출 최종 실패", e, raise_error=False)
			return ""
