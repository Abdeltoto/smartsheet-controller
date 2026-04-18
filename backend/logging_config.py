"""Structured logging with token redaction."""
import logging
import re
import sys
from typing import Any


_TOKEN_PATTERN = re.compile(r'(?i)(bearer\s+|"?(?:token|api_?key|authorization)"?\s*[:=]\s*"?)([A-Za-z0-9\-_\.]{16,})')


class TokenRedactor(logging.Filter):
    """Redact anything that looks like a bearer token or API key from log records."""

    def filter(self, record: logging.LogRecord) -> bool:
        try:
            if isinstance(record.msg, str):
                record.msg = _TOKEN_PATTERN.sub(r"\1***REDACTED***", record.msg)
            if record.args:
                record.args = tuple(
                    _TOKEN_PATTERN.sub(r"\1***REDACTED***", a) if isinstance(a, str) else a
                    for a in record.args
                )
        except Exception:
            pass
        return True


class CompactFormatter(logging.Formatter):
    """Single-line human-readable formatter with timestamps."""

    def format(self, record: logging.LogRecord) -> str:
        ts = self.formatTime(record, "%H:%M:%S")
        level = record.levelname[0]
        name = record.name.split(".")[-1]
        extra_ctx = ""
        ctx_parts = []
        for key in ("session_id", "request_id", "tool"):
            val = getattr(record, key, None)
            if val:
                ctx_parts.append(f"{key}={val}")
        if ctx_parts:
            extra_ctx = " [" + " ".join(ctx_parts) + "]"
        return f"{ts} {level} {name}{extra_ctx}: {record.getMessage()}"


def setup_logging(level: str = "INFO") -> None:
    """Configure root logger once."""
    root = logging.getLogger()
    if getattr(root, "_smartsheet_configured", False):
        return

    root.setLevel(level.upper())

    for h in list(root.handlers):
        root.removeHandler(h)

    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(CompactFormatter())
    handler.addFilter(TokenRedactor())
    root.addHandler(handler)

    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("httpcore").setLevel(logging.WARNING)
    logging.getLogger("uvicorn.access").setLevel(logging.WARNING)

    root._smartsheet_configured = True  # type: ignore[attr-defined]


def get_logger(name: str) -> logging.Logger:
    return logging.getLogger(name)


def log_with(logger: logging.Logger, level: int, msg: str, **ctx: Any) -> None:
    """Log a message with extra context fields."""
    logger.log(level, msg, extra=ctx)
