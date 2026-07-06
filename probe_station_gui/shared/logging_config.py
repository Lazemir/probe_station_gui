"""Utilities for configuring application-wide logging."""

from __future__ import annotations

import atexit
import logging
import os
import queue
import re
from logging.handlers import QueueHandler, QueueListener, RotatingFileHandler
from pathlib import Path

_HANDLER_FLAG = "_probe_station_gui_managed"
_FILTER_FLAG = "_probe_station_gui_sensitive_filter"
_HOT_DEBUG_FILTER_FLAG = "_probe_station_gui_hot_debug_filter"
_LISTENER: QueueListener | None = None
_LISTENER_HANDLER: logging.Handler | None = None
_LISTENER_PATH: Path | None = None
_LOG_MAX_BYTES = 10 * 1024 * 1024
_LOG_BACKUP_COUNT = 5
_TRACE_LOG_ENV = "PROBE_STATION_GUI_TRACE_LOGS"
_HOT_DEBUG_PREFIXES = ("SERIAL TRACE", "TIMING")
_NOISY_EXTERNAL_LOGGERS = (
    "pyvisa",
    "qcodes",
)
_TOKEN_BEARING_EXTERNAL_LOGGERS = (
    "telegram",
    "httpx",
    "httpcore",
)
_BOT_API_TOKEN_RE = re.compile(r"bot(\d{5,}:[A-Za-z0-9_-]+)")


def configure_logging(log_path: Path, level_name: str) -> None:
    """Configure the root logger to write to the specified file."""

    log_path = Path(log_path)
    log_path.parent.mkdir(parents=True, exist_ok=True)
    numeric_level = _normalise_level(level_name)
    root_logger = logging.getLogger()
    root_logger.setLevel(numeric_level)
    _cap_noisy_external_loggers(numeric_level)

    handler = _find_managed_handler(root_logger)
    if handler is None:
        handler = _create_queue_handler(log_path)
        setattr(handler, _HANDLER_FLAG, True)
        root_logger.addHandler(handler)
    else:
        handler = _ensure_handler_destination(handler, log_path, root_logger)

    handler.setLevel(numeric_level)
    _ensure_hot_debug_filter(handler)
    _ensure_sensitive_filter(handler)
    handler.setFormatter(
        logging.Formatter("%(asctime)s [%(levelname)s] %(name)s: %(message)s")
    )
    if _LISTENER_HANDLER is not None:
        _LISTENER_HANDLER.setLevel(numeric_level)
        _ensure_hot_debug_filter(_LISTENER_HANDLER)
        _ensure_sensitive_filter(_LISTENER_HANDLER)
        _LISTENER_HANDLER.setFormatter(logging.Formatter("%(message)s"))

    for existing in root_logger.handlers:
        if existing is handler:
            continue
        if getattr(existing, _HANDLER_FLAG, False):
            existing.setLevel(numeric_level)

    root_logger.debug("Logging configured: path=%s level=%s", log_path, level_name)


def _cap_noisy_external_loggers(root_level: int) -> None:
    """Keep third-party transport traces from dominating debug logs."""

    external_level = max(root_level, logging.INFO)
    for logger_name in _NOISY_EXTERNAL_LOGGERS:
        logging.getLogger(logger_name).setLevel(external_level)
    token_bearing_level = max(root_level, logging.WARNING)
    for logger_name in _TOKEN_BEARING_EXTERNAL_LOGGERS:
        logging.getLogger(logger_name).setLevel(token_bearing_level)


class _SensitiveLogFilter(logging.Filter):
    """Redact secrets that may appear in third-party debug messages."""

    def filter(self, record: logging.LogRecord) -> bool:
        record.msg = self._redact_value(record.msg)
        if isinstance(record.args, dict):
            record.args = {
                key: self._redact_value(value) for key, value in record.args.items()
            }
        elif isinstance(record.args, tuple):
            record.args = tuple(self._redact_value(value) for value in record.args)
        return True

    @staticmethod
    def _redact_value(value: object) -> object:
        if isinstance(value, str):
            return _BOT_API_TOKEN_RE.sub("bot<redacted-token>", value)
        return value


class _HotDebugFilter(logging.Filter):
    """Keep high-rate trace instrumentation out of normal DEBUG logs."""

    def filter(self, record: logging.LogRecord) -> bool:
        if record.levelno != logging.DEBUG:
            return True
        if _trace_logging_enabled():
            return True
        message = record.msg
        if not isinstance(message, str):
            return True
        return not message.startswith(_HOT_DEBUG_PREFIXES)


def _trace_logging_enabled() -> bool:
    raw = os.getenv(_TRACE_LOG_ENV, "")
    return raw.strip().lower() in {"1", "true", "yes", "on"}


def _ensure_hot_debug_filter(handler: logging.Handler) -> None:
    """Attach the hot-debug suppressor once to a managed handler."""

    for existing in handler.filters:
        if getattr(existing, _HOT_DEBUG_FILTER_FLAG, False):
            return
    log_filter = _HotDebugFilter()
    setattr(log_filter, _HOT_DEBUG_FILTER_FLAG, True)
    handler.addFilter(log_filter)


def _ensure_sensitive_filter(handler: logging.Handler) -> None:
    """Attach the secret-redacting filter once to the handler."""

    for existing in handler.filters:
        if getattr(existing, _FILTER_FLAG, False):
            return
    log_filter = _SensitiveLogFilter()
    setattr(log_filter, _FILTER_FLAG, True)
    handler.addFilter(log_filter)


def _create_queue_handler(log_path: Path) -> logging.Handler:
    """Create a non-blocking frontend for the managed file logger."""

    log_queue: queue.SimpleQueue[logging.LogRecord] = queue.SimpleQueue()
    handler = QueueHandler(log_queue)
    _start_listener(log_queue, log_path)
    return handler


def _start_listener(
    log_queue: queue.SimpleQueue[logging.LogRecord],
    log_path: Path,
) -> None:
    """Start the background file writer for queued log records."""

    global _LISTENER, _LISTENER_HANDLER, _LISTENER_PATH
    _stop_listener()
    file_handler = RotatingFileHandler(
        log_path,
        maxBytes=_LOG_MAX_BYTES,
        backupCount=_LOG_BACKUP_COUNT,
        encoding="utf-8",
        delay=True,
    )
    setattr(file_handler, _HANDLER_FLAG, True)
    listener = QueueListener(log_queue, file_handler)
    listener.start()
    _LISTENER = listener
    _LISTENER_HANDLER = file_handler
    _LISTENER_PATH = log_path


def _stop_listener() -> None:
    """Stop the current background file writer, if any."""

    global _LISTENER, _LISTENER_HANDLER, _LISTENER_PATH
    listener = _LISTENER
    handler = _LISTENER_HANDLER
    _LISTENER = None
    _LISTENER_HANDLER = None
    _LISTENER_PATH = None
    if listener is not None:
        listener.stop()
    if handler is not None:
        handler.close()


def _normalise_level(level_name: str) -> int:
    """Translate a textual level into a logging module constant."""

    if not level_name:
        return logging.INFO
    upper = level_name.upper()
    if upper in logging._nameToLevel:  # type: ignore[attr-defined]
        return logging._nameToLevel[upper]  # type: ignore[attr-defined]
    return logging.INFO


def _find_managed_handler(logger: logging.Logger) -> logging.Handler | None:
    """Return an existing handler owned by the logging configurator."""

    for handler in logger.handlers:
        if getattr(handler, _HANDLER_FLAG, False):
            return handler
    return None


def _ensure_handler_destination(
    handler: logging.Handler, log_path: Path, root_logger: logging.Logger
) -> logging.Handler:
    """Replace the managed handler if it points to the wrong file."""

    if isinstance(handler, QueueHandler):
        if _LISTENER_PATH == log_path:
            return handler
        root_logger.removeHandler(handler)
        handler.close()
        new_handler = _create_queue_handler(log_path)
        setattr(new_handler, _HANDLER_FLAG, True)
        root_logger.addHandler(new_handler)
        return new_handler

    if not isinstance(handler, logging.FileHandler):
        root_logger.removeHandler(handler)
        handler.close()
        new_handler = _create_queue_handler(log_path)
        setattr(new_handler, _HANDLER_FLAG, True)
        root_logger.addHandler(new_handler)
        return new_handler

    current_path = Path(handler.baseFilename)
    if current_path != log_path:
        root_logger.removeHandler(handler)
        handler.close()
        new_handler = _create_queue_handler(log_path)
        setattr(new_handler, _HANDLER_FLAG, True)
        root_logger.addHandler(new_handler)
        return new_handler

    root_logger.removeHandler(handler)
    handler.close()
    new_handler = _create_queue_handler(log_path)
    setattr(new_handler, _HANDLER_FLAG, True)
    root_logger.addHandler(new_handler)
    return new_handler


atexit.register(_stop_listener)

