"""Structured JSON logging with rotation, plus a tiny latency-timing helper.

Every log line is one JSON object per line (easy to grep/ship). Rejection
reasons, fills, and stage latencies all go through this so the daily
report and any external log shipper see the same shape.
"""
from __future__ import annotations

import contextlib
import json
import logging
import logging.handlers
import os
import time
from typing import Any, Iterator


class JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, Any] = {
            "ts": round(record.created, 3),
            "level": record.levelname,
            "logger": record.name,
            "msg": record.getMessage(),
        }
        extra = getattr(record, "fields", None)
        if isinstance(extra, dict):
            payload.update(extra)
        if record.exc_info:
            payload["exc_info"] = self.formatException(record.exc_info)
        return json.dumps(payload, default=str)


def setup_logging(log_dir: str = "logs", level: int = logging.INFO) -> logging.Logger:
    os.makedirs(log_dir, exist_ok=True)
    logger = logging.getLogger("memebot")
    logger.setLevel(level)
    logger.handlers.clear()

    file_handler = logging.handlers.RotatingFileHandler(
        os.path.join(log_dir, "memebot.jsonl"), maxBytes=10 * 1024 * 1024, backupCount=10
    )
    file_handler.setFormatter(JsonFormatter())
    logger.addHandler(file_handler)

    console_handler = logging.StreamHandler()
    console_handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s"))
    logger.addHandler(console_handler)

    logger.propagate = False
    return logger


def log_event(logger: logging.Logger, msg: str, level: int = logging.INFO, **fields: Any) -> None:
    logger.log(level, msg, extra={"fields": fields})


@contextlib.contextmanager
def stage_timer(logger: logging.Logger, stage: str, **fields: Any) -> Iterator[None]:
    """Context manager that logs how long a pipeline stage took.

    Used around every RPC call, Jupiter call, and safety check so latency
    regressions show up in the JSON logs without instrumenting each call site.
    """
    start = time.monotonic()
    error: str | None = None
    try:
        yield
    except Exception as exc:  # noqa: BLE001 - we log then re-raise
        error = repr(exc)
        raise
    finally:
        elapsed_ms = (time.monotonic() - start) * 1000.0
        log_event(
            logger,
            f"stage:{stage}",
            stage=stage,
            elapsed_ms=round(elapsed_ms, 2),
            error=error,
            **fields,
        )
