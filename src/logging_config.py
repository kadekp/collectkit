"""
Structured JSON logging configuration for Grafana Loki compatibility.

Usage:
    from .logging_config import setup_logging, get_logger
    setup_logging()
    logger = get_logger(__name__)
    logger.info("Event occurred", extra={"event": "my_event", "user_id": 123})
"""

import json
import logging
import os
import sys
from datetime import datetime, timezone


# Fields that contain PII and should be masked
PII_FIELDS = ["phone_number", "customer_number"]


def mask_phone(phone: str) -> str:
    """
    Mask phone number for logging (keep first 4 and last 3 digits).
    Example: 6282333895355 -> 6282***355
    """
    if not phone or len(phone) < 8:
        return phone
    return f"{phone[:4]}***{phone[-3:]}"


def mask_pii(key: str, value) -> str:
    """Mask PII fields for logging."""
    if key in PII_FIELDS and value:
        return mask_phone(str(value))
    return value


class JSONFormatter(logging.Formatter):
    """JSON formatter for Grafana Loki compatibility."""

    # Extra fields to include in JSON output if present
    EXTRA_FIELDS = [
        "event",
        "phone_number",
        "task_id",
        "task_type",
        "duration_ms",
        "count",
        "error",
        "mimin_id",
        "status",
        "customer_number",
        "borrower_status",
        "days_late",
        "billing_amount",
        "ptp_count",
        "message_count",
        "checked",
        "paid",
        "errors",
        "sent",
        "failed",
        "guardrail",
        "reason",
        # LLM/Agent logging fields
        "model",
        "input_tokens",
        "output_tokens",
        "total_tokens",
        "has_tool_calls",
        "tool_name",
        "tool_args",
        "success",
    ]

    def format(self, record: logging.LogRecord) -> str:
        """Format log record as JSON."""
        log_record = {
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "level": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
            "module": record.module,
            "function": record.funcName,
            "line": record.lineno,
        }

        # Add extra fields if present (with PII masking)
        for key in self.EXTRA_FIELDS:
            value = getattr(record, key, None)
            if value is not None:
                log_record[key] = mask_pii(key, value)

        # Add exception info if present
        if record.exc_info:
            log_record["exception"] = self.formatException(record.exc_info)

        return json.dumps(log_record, default=str)


class HumanReadableFormatter(logging.Formatter):
    """Human-readable formatter for local development."""

    def format(self, record: logging.LogRecord) -> str:
        """Format log record as human-readable string with extras."""
        # Base format
        base = f"{self.formatTime(record)} [{record.levelname}] {record.getMessage()}"

        # Add extra fields if present (with PII masking)
        extras = []
        for key in JSONFormatter.EXTRA_FIELDS:
            value = getattr(record, key, None)
            if value is not None:
                extras.append(f"{key}={mask_pii(key, value)}")

        if extras:
            base += f" | {', '.join(extras)}"

        # Add exception if present
        if record.exc_info:
            base += f"\n{self.formatException(record.exc_info)}"

        return base


def setup_logging(json_format: bool = None) -> None:
    """
    Configure logging for the application.

    Args:
        json_format: Force JSON format. If None, auto-detect based on LOG_FORMAT env var.
                    Set LOG_FORMAT=json for JSON, LOG_FORMAT=text for human-readable.
                    Default is JSON in production (Railway), text locally.
    """
    if json_format is None:
        log_format = os.getenv("LOG_FORMAT", "").lower()
        if log_format == "json":
            json_format = True
        elif log_format == "text":
            json_format = False
        else:
            # Auto-detect: use JSON if running on Railway (has RAILWAY_* env vars)
            json_format = bool(os.getenv("RAILWAY_ENVIRONMENT"))

    handler = logging.StreamHandler(sys.stdout)

    if json_format:
        handler.setFormatter(JSONFormatter())
    else:
        handler.setFormatter(
            HumanReadableFormatter(
                fmt="%(asctime)s [%(levelname)s] %(message)s",
                datefmt="%Y-%m-%d %H:%M:%S",
            )
        )

    # Configure root logger with configurable log level
    root = logging.getLogger()
    log_level = os.getenv("LOG_LEVEL", "INFO").upper()
    root.setLevel(getattr(logging, log_level, logging.INFO))
    root.handlers = [handler]

    # Suppress noisy loggers
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("httpcore").setLevel(logging.WARNING)
    logging.getLogger("urllib3").setLevel(logging.WARNING)


def get_logger(name: str) -> logging.Logger:
    """Get a logger instance with the given name."""
    return logging.getLogger(name)
