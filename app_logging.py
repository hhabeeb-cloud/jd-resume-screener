"""Date-based, privacy-conscious logging for the resume screener."""

import logging
from contextlib import contextmanager
from contextvars import ContextVar
from datetime import date, datetime
from pathlib import Path
from typing import Callable, Iterator
from uuid import uuid4

LOG_DIRECTORY = Path(__file__).resolve().with_name("logs")
REQUEST_ID: ContextVar[str] = ContextVar("screening_request_id", default="-")


class RequestIdFilter(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        record.request_id = REQUEST_ID.get()
        return True


class DateBasedFileHandler(logging.Handler):
    """Write records to a new file whenever the local calendar date changes."""

    terminator = "\n"

    def __init__(
        self,
        directory: Path,
        clock: Callable[[], date] | None = None,
    ) -> None:
        super().__init__(level=logging.INFO)
        self.directory = directory
        self.clock = clock or (lambda: datetime.now().astimezone().date())
        self._active_date: date | None = None
        self._stream = None

    def emit(self, record: logging.LogRecord) -> None:
        try:
            current_date = self.clock()
            if current_date != self._active_date:
                if self._stream is not None:
                    self._stream.close()
                self.directory.mkdir(parents=True, exist_ok=True)
                path = self.directory / f"resume-screener-{current_date.isoformat()}.log"
                self._stream = path.open("a", encoding="utf-8")
                self._active_date = current_date

            self._stream.write(self.format(record) + self.terminator)
            self._stream.flush()
        except Exception:
            self.handleError(record)

    def close(self) -> None:
        if self._stream is not None:
            self._stream.close()
            self._stream = None
        super().close()


def configure_logging() -> logging.Logger:
    """Configure one shared app logger without duplicating handlers on reruns."""
    app_logger = logging.getLogger("resume_screener")
    app_logger.setLevel(logging.INFO)
    app_logger.propagate = False
    if not any(getattr(handler, "_resume_screener_handler", False) for handler in app_logger.handlers):
        handler = DateBasedFileHandler(LOG_DIRECTORY)
        handler.addFilter(RequestIdFilter())
        handler.setFormatter(logging.Formatter(
            "%(asctime)s %(levelname)s [request_id=%(request_id)s] %(message)s"
        ))
        handler._resume_screener_handler = True
        app_logger.addHandler(handler)
    return app_logger


logger = configure_logging()


@contextmanager
def request_log_context() -> Iterator[str]:
    """Start one batch with a delimiter and scope its correlation ID."""
    request_id = str(uuid4())
    token = REQUEST_ID.set(request_id)
    try:
        logger.info("==================== REQUEST START ====================")
        yield request_id
    finally:
        REQUEST_ID.reset(token)