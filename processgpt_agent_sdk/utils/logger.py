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

APPLICATION_LOGGER = logging.getLogger("process-gpt-agent-framework")


def write_log_message(message: str, level: int = logging.INFO) -> None:
	spaced = os.getenv("LOG_SPACED", "1") != "0"
	suffix = "\n" if spaced else ""
	APPLICATION_LOGGER.log(level, f"{message}{suffix}")


def handle_application_error(title: str, error: Exception, *, raise_error: bool = True, extra: Optional[Dict] = None) -> None:
	spaced = os.getenv("LOG_SPACED", "1") != "0"
	suffix = "\n" if spaced else ""
	context = f" | extra={extra}" if extra else ""
	APPLICATION_LOGGER.error(f"{title}: {error}{context}{suffix}")
	APPLICATION_LOGGER.error(traceback.format_exc())
	if raise_error:
		raise error
