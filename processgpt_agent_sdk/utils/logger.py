import logging
import os
import traceback
from typing import Optional, Dict


# Configure root logger only once (idempotent)
if not logging.getLogger().handlers:
	# LOG_LEVEL 환경변수 읽기
	log_level = os.getenv("LOG_LEVEL", "INFO").upper()
	if log_level == "DEBUG":
		level = logging.DEBUG
	elif log_level == "INFO":
		level = logging.INFO
	elif log_level == "WARNING":
		level = logging.WARNING
	elif log_level == "ERROR":
		level = logging.ERROR
	else:
		level = logging.INFO
	
	logging.basicConfig(
		level=level,
		format="%(asctime)s %(levelname)s %(name)s - %(message)s",
	)

LOGGER_NAME = os.getenv("LOGGER_NAME") or "processgpt"
APPLICATION_LOGGER = logging.getLogger(LOGGER_NAME)

# Application logger도 같은 레벨로 설정
log_level = os.getenv("LOG_LEVEL", "INFO").upper()
if log_level == "DEBUG":
	APPLICATION_LOGGER.setLevel(logging.DEBUG)
elif log_level == "INFO":
	APPLICATION_LOGGER.setLevel(logging.INFO)
elif log_level == "WARNING":
	APPLICATION_LOGGER.setLevel(logging.WARNING)
elif log_level == "ERROR":
	APPLICATION_LOGGER.setLevel(logging.ERROR)

# 디버그 레벨 상수 정의
DEBUG_LEVEL_NONE = 0      # 디버그 로그 없음
DEBUG_LEVEL_BASIC = 1     # 기본 디버그 로그 (INFO 레벨)
DEBUG_LEVEL_DETAILED = 2  # 상세 디버그 로그 (DEBUG 레벨)
DEBUG_LEVEL_VERBOSE = 3   # 매우 상세한 디버그 로그 (DEBUG 레벨 + 추가 정보)

# 환경변수에서 디버그 레벨 읽기
DEBUG_LEVEL = int(os.getenv("DEBUG_LEVEL", "1"))


def set_application_logger_name(name: str) -> None:
	"""애플리케이션 로거 이름을 런타임에 변경한다."""
	global APPLICATION_LOGGER
	APPLICATION_LOGGER = logging.getLogger(name or "processgpt")


def write_log_message(message: str, level: int = logging.INFO, debug_level: int = DEBUG_LEVEL_BASIC) -> None:
	"""로그 메시지를 쓴다. 디버그 레벨에 따라 출력 여부를 결정한다."""
	# 디버그 레벨 체크
	if debug_level > DEBUG_LEVEL:
		return
	
	# DEBUG 레벨 로그의 경우 로거 레벨도 확인
	if level == logging.DEBUG and DEBUG_LEVEL < DEBUG_LEVEL_DETAILED:
		return
	
	spaced = os.getenv("LOG_SPACED", "1") != "0"
	suffix = "\n" if spaced else ""
	APPLICATION_LOGGER.log(level, f"{message}{suffix}")


def write_debug_message(message: str, debug_level: int = DEBUG_LEVEL_BASIC) -> None:
	"""디버그 전용 로그 메시지를 쓴다."""
	write_log_message(message, logging.DEBUG, debug_level)


def write_info_message(message: str, debug_level: int = DEBUG_LEVEL_BASIC) -> None:
	"""정보 로그 메시지를 쓴다."""
	write_log_message(message, logging.INFO, debug_level)


def set_debug_level(level: int) -> None:
	"""런타임에 디버그 레벨을 설정한다."""
	global DEBUG_LEVEL
	DEBUG_LEVEL = level
	write_info_message(f"디버그 레벨이 {level}로 설정되었습니다.", DEBUG_LEVEL_BASIC)


def handle_application_error(title: str, error: Exception, *, raise_error: bool = True, extra: Optional[Dict] = None) -> None:
	"""예외 상황을 처리한다."""
	spaced = os.getenv("LOG_SPACED", "1") != "0"
	suffix = "\n" if spaced else ""
	context = f" | extra={extra}" if extra else ""
	APPLICATION_LOGGER.error(f"{title}: {error}{context}{suffix}")
	APPLICATION_LOGGER.error(traceback.format_exc())
	if raise_error:
		raise error
