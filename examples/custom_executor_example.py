#!/usr/bin/env python3
"""
사용자 정의 실행기 예제

이 예제는 실제 AI 모델이나 비즈니스 로직을 시뮬레이션하는 
더 현실적인 실행기를 보여줍니다.
"""

import asyncio
import os
import sys
import json
from typing import Dict, List, Any

# 프로젝트 루트를 import 경로에 추가
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from processgpt_agent_sdk.simulator import ProcessGPTAgentSimulator
from a2a.server.agent_execution import AgentExecutor, RequestContext
from a2a.server.events import EventQueue, Event
from processgpt_agent_sdk.utils.logger import write_log_message


class CustomBusinessExecutor(AgentExecutor):
    """비즈니스 로직을 시뮬레이션하는 사용자 정의 실행기"""
    
    def __init__(self):
        self.is_cancelled = False
        self.business_processes = {
            "데이터 분석": self._analyze_data,
            "보고서 작성": self._generate_report,
            "고객 서비스": self._customer_service,
            "프로젝트 관리": self._project_management,
            "기본": self._default_process
        }

    async def execute(self, context: RequestContext, event_queue: EventQueue) -> None:
        """사용자 정의 비즈니스 로직을 실행한다."""
        write_log_message("사용자 정의 실행기 시작")
        
        prompt = context.get_user_input()
        context_data = context.get_context_data()
        
        # 시작 이벤트
        start_event = Event(
            type="task_started",
            data={
                "message": f"비즈니스 프로세스 시작: {prompt}",
                "prompt": prompt,
                "executor_type": "CustomBusinessExecutor"
            }
        )
        event_queue.enqueue_event(start_event)

        # 프롬프트 기반으로 적절한 비즈니스 프로세스 선택
        process_type = self._determine_process_type(prompt)
        write_log_message(f"선택된 프로세스: {process_type}")
        
        # 프로세스 타입 이벤트
        process_event = Event(
            type="process_selected",
            data={
                "process_type": process_type,
                "message": f"'{process_type}' 프로세스가 선택되었습니다."
            }
        )
        event_queue.enqueue_event(process_event)

        # 해당 비즈니스 프로세스 실행
        try:
            process_func = self.business_processes.get(process_type, self.business_processes["기본"])
            await process_func(prompt, context_data, event_queue)
        except Exception as e:
            error_event = Event(
                type="error",
                data={
                    "error": str(e),
                    "message": f"프로세스 실행 중 오류 발생: {e}"
                }
            )
            event_queue.enqueue_event(error_event)
            return

        if not self.is_cancelled:
            # 완료 이벤트
            done_event = Event(
                type="done",
                data={
                    "message": f"'{process_type}' 프로세스 완료",
                    "success": True,
                    "process_type": process_type
                }
            )
            event_queue.enqueue_event(done_event)

        write_log_message("사용자 정의 실행기 종료")

    async def cancel(self, context: RequestContext, event_queue: EventQueue) -> None:
        """실행 취소를 수행한다."""
        write_log_message("사용자 정의 실행기 취소 요청")
        self.is_cancelled = True

    def _determine_process_type(self, prompt: str) -> str:
        """프롬프트를 분석하여 적절한 프로세스 타입을 결정한다."""
        prompt_lower = prompt.lower()
        
        if any(keyword in prompt_lower for keyword in ["분석", "데이터", "차트", "그래프", "통계"]):
            return "데이터 분석"
        elif any(keyword in prompt_lower for keyword in ["보고서", "리포트", "문서", "작성"]):
            return "보고서 작성"
        elif any(keyword in prompt_lower for keyword in ["고객", "서비스", "문의", "지원"]):
            return "고객 서비스"
        elif any(keyword in prompt_lower for keyword in ["프로젝트", "관리", "계획", "일정"]):
            return "프로젝트 관리"
        else:
            return "기본"

    async def _analyze_data(self, prompt: str, context_data: Dict[str, Any], event_queue: EventQueue):
        """데이터 분석 프로세스를 시뮬레이션한다."""
        steps = [
            ("데이터 수집", "필요한 데이터를 수집하고 있습니다..."),
            ("데이터 정제", "데이터를 정제하고 전처리하고 있습니다..."),
            ("분석 수행", "통계 분석 및 패턴 인식을 수행하고 있습니다..."),
            ("결과 생성", "분석 결과를 생성하고 있습니다..."),
            ("시각화", "차트와 그래프를 생성하고 있습니다...")
        ]
        
        for i, (step_name, step_message) in enumerate(steps, 1):
            if self.is_cancelled:
                break
                
            event = Event(
                type="progress",
                data={
                    "step": i,
                    "total_steps": len(steps),
                    "step_name": step_name,
                    "message": step_message,
                    "progress_percentage": (i / len(steps)) * 100
                }
            )
            event_queue.enqueue_event(event)
            await asyncio.sleep(1.5)

        if not self.is_cancelled:
            # 결과 출력
            result_event = Event(
                type="output",
                data={
                    "content": {
                        "analysis_type": "데이터 분석",
                        "findings": [
                            "데이터셋에서 3개의 주요 패턴을 발견했습니다.",
                            "평균 성능이 지난 달 대비 15% 향상되었습니다.",
                            "이상값 5개가 감지되어 추가 검토가 필요합니다."
                        ],
                        "recommendations": [
                            "월별 트렌드 모니터링을 강화하세요.",
                            "이상값에 대한 근본 원인 분석을 수행하세요.",
                            "성능 향상 요인을 다른 영역에도 적용해보세요."
                        ],
                        "charts": ["trend_chart.png", "distribution_chart.png"]
                    },
                    "final": True
                }
            )
            event_queue.enqueue_event(result_event)

    async def _generate_report(self, prompt: str, context_data: Dict[str, Any], event_queue: EventQueue):
        """보고서 작성 프로세스를 시뮬레이션한다."""
        steps = [
            ("요구사항 분석", "보고서 요구사항을 분석하고 있습니다..."),
            ("구조 설계", "보고서 구조와 목차를 설계하고 있습니다..."),
            ("내용 작성", "주요 내용을 작성하고 있습니다..."),
            ("검토 및 수정", "작성된 내용을 검토하고 수정하고 있습니다..."),
            ("최종 확인", "최종 검토를 수행하고 있습니다...")
        ]
        
        for i, (step_name, step_message) in enumerate(steps, 1):
            if self.is_cancelled:
                break
                
            event = Event(
                type="progress",
                data={
                    "step": i,
                    "total_steps": len(steps),
                    "step_name": step_name,
                    "message": step_message,
                    "progress_percentage": (i / len(steps)) * 100
                }
            )
            event_queue.enqueue_event(event)
            await asyncio.sleep(1.2)

        if not self.is_cancelled:
            result_event = Event(
                type="output",
                data={
                    "content": {
                        "report_type": "종합 보고서",
                        "sections": [
                            "1. 개요",
                            "2. 현황 분석",
                            "3. 주요 발견사항",
                            "4. 권장사항",
                            "5. 결론"
                        ],
                        "key_points": [
                            "주요 목표 달성률: 87%",
                            "개선 영역: 고객 만족도",
                            "우선순위: 프로세스 자동화"
                        ],
                        "next_steps": [
                            "이해관계자 검토 요청",
                            "실행 계획 수립",
                            "월간 진행상황 모니터링"
                        ]
                    },
                    "final": True
                }
            )
            event_queue.enqueue_event(result_event)

    async def _customer_service(self, prompt: str, context_data: Dict[str, Any], event_queue: EventQueue):
        """고객 서비스 프로세스를 시뮬레이션한다."""
        steps = [
            ("문의 분석", "고객 문의 내용을 분석하고 있습니다..."),
            ("솔루션 검색", "기존 솔루션 데이터베이스에서 검색하고 있습니다..."),
            ("응답 준비", "고객 맞춤 응답을 준비하고 있습니다..."),
            ("품질 검토", "응답 품질을 검토하고 있습니다...")
        ]
        
        for i, (step_name, step_message) in enumerate(steps, 1):
            if self.is_cancelled:
                break
                
            event = Event(
                type="progress",
                data={
                    "step": i,
                    "total_steps": len(steps),
                    "step_name": step_name,
                    "message": step_message,
                    "progress_percentage": (i / len(steps)) * 100
                }
            )
            event_queue.enqueue_event(event)
            await asyncio.sleep(1.0)

        if not self.is_cancelled:
            result_event = Event(
                type="output",
                data={
                    "content": {
                        "service_type": "고객 지원",
                        "inquiry_category": "일반 문의",
                        "resolution": "고객님의 문의에 대한 상세한 답변을 준비했습니다.",
                        "response_time": "평균 응답 시간: 2시간",
                        "satisfaction_score": 4.8,
                        "follow_up_required": False
                    },
                    "final": True
                }
            )
            event_queue.enqueue_event(result_event)

    async def _project_management(self, prompt: str, context_data: Dict[str, Any], event_queue: EventQueue):
        """프로젝트 관리 프로세스를 시뮬레이션한다."""
        steps = [
            ("프로젝트 분석", "프로젝트 요구사항을 분석하고 있습니다..."),
            ("일정 계획", "프로젝트 일정을 계획하고 있습니다..."),
            ("리소스 할당", "필요한 리소스를 할당하고 있습니다..."),
            ("위험 평가", "프로젝트 위험을 평가하고 있습니다..."),
            ("계획 최적화", "전체 계획을 최적화하고 있습니다...")
        ]
        
        for i, (step_name, step_message) in enumerate(steps, 1):
            if self.is_cancelled:
                break
                
            event = Event(
                type="progress",
                data={
                    "step": i,
                    "total_steps": len(steps),
                    "step_name": step_name,
                    "message": step_message,
                    "progress_percentage": (i / len(steps)) * 100
                }
            )
            event_queue.enqueue_event(event)
            await asyncio.sleep(1.3)

        if not self.is_cancelled:
            result_event = Event(
                type="output",
                data={
                    "content": {
                        "project_type": "종합 프로젝트 관리",
                        "timeline": "예상 완료: 6주",
                        "milestones": [
                            "주 1: 요구사항 정의",
                            "주 2-3: 설계 및 개발",
                            "주 4-5: 테스트 및 검증",
                            "주 6: 배포 및 완료"
                        ],
                        "resources": [
                            "개발자 2명",
                            "디자이너 1명",
                            "PM 1명"
                        ],
                        "risks": [
                            "기술적 복잡성: 중간",
                            "일정 지연 가능성: 낮음",
                            "리소스 부족: 낮음"
                        ]
                    },
                    "final": True
                }
            )
            event_queue.enqueue_event(result_event)

    async def _default_process(self, prompt: str, context_data: Dict[str, Any], event_queue: EventQueue):
        """기본 프로세스를 시뮬레이션한다."""
        steps = [
            ("요청 처리", "요청을 처리하고 있습니다..."),
            ("결과 생성", "결과를 생성하고 있습니다...")
        ]
        
        for i, (step_name, step_message) in enumerate(steps, 1):
            if self.is_cancelled:
                break
                
            event = Event(
                type="progress",
                data={
                    "step": i,
                    "total_steps": len(steps),
                    "step_name": step_name,
                    "message": step_message,
                    "progress_percentage": (i / len(steps)) * 100
                }
            )
            event_queue.enqueue_event(event)
            await asyncio.sleep(1.0)

        if not self.is_cancelled:
            result_event = Event(
                type="output",
                data={
                    "content": {
                        "process_type": "일반 처리",
                        "result": f"'{prompt}' 요청이 성공적으로 처리되었습니다.",
                        "timestamp": "처리 완료 시간 기록됨"
                    },
                    "final": True
                }
            )
            event_queue.enqueue_event(result_event)


async def main():
    """메인 함수 - 사용자 정의 실행기로 시뮬레이션 실행"""
    print("=== ProcessGPT 사용자 정의 실행기 예제 ===")
    print()
    
    # 실행기 생성
    executor = CustomBusinessExecutor()
    
    # 시뮬레이터 생성
    simulator = ProcessGPTAgentSimulator(
        executor=executor,
        agent_orch="custom_business"
    )
    
    # 여러 예제 실행
    examples = [
        "고객 데이터를 분석해서 트렌드를 파악해주세요",
        "분기별 성과 보고서를 작성해주세요",
        "고객 문의에 대한 응답을 준비해주세요",
        "신제품 개발 프로젝트를 계획해주세요"
    ]
    
    for i, prompt in enumerate(examples, 1):
        print(f"\n--- 예제 {i}: {prompt} ---")
        await simulator.run_simulation(prompt)
        print("\n" + "="*60)


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print("\n예제 실행이 중단되었습니다.")
        sys.exit(1)
