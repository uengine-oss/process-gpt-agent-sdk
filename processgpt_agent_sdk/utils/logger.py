import logging
import os
import traceback
from typing import Optional, Dict


# Configure root logger only once (idempotent)
if not logging.getLogger().handlers:
	logging.basicConfig(
		level=logging.INFO,
		format="%(asctime)s %(levelname)s %(name)s - %(message)s",
	)

LOGGER_NAME = os.getenv("LOGGER_NAME") or "processgpt"
APPLICATION_LOGGER = logging.getLogger(LOGGER_NAME)


def set_application_logger_name(name: str) -> None:
	"""애플리케이션 로거 이름을 런타임에 변경한다."""
	global APPLICATION_LOGGER
	APPLICATION_LOGGER = logging.getLogger(name or "processgpt")


def write_log_message(message: str, level: int = logging.INFO) -> None:
	"""로그 메시지를 쓴다."""
	spaced = os.getenv("LOG_SPACED", "1") != "0"
	suffix = "\n" if spaced else ""
	APPLICATION_LOGGER.log(level, f"{message}{suffix}")


def handle_application_error(title: str, error: Exception, *, raise_error: bool = True, extra: Optional[Dict] = None) -> None:
	"""예외 상황을 처리한다."""
	spaced = os.getenv("LOG_SPACED", "1") != "0"
	suffix = "\n" if spaced else ""
	context = f" | extra={extra}" if extra else ""
	APPLICATION_LOGGER.error(f"{title}: {error}{context}{suffix}")
	APPLICATION_LOGGER.error(traceback.format_exc())
	if raise_error:
		raise error
