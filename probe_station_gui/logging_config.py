"""Utilities for configuring application-wide logging."""

from __future__ import annotations

import atexit
import logging
import queue
from logging.handlers import QueueHandler, QueueListener
from pathlib import Path

_HANDLER_FLAG = "_probe_station_gui_managed"
_LISTENER: QueueListener | None = None
_LISTENER_HANDLER: logging.Handler | None = None
_LISTENER_PATH: Path | None = None
_NOISY_EXTERNAL_LOGGERS = (
    "pyvisa",
    "qcodes",
)


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
    handler.setFormatter(
        logging.Formatter("%(asctime)s [%(levelname)s] %(name)s: %(message)s")
    )
    if _LISTENER_HANDLER is not None:
        _LISTENER_HANDLER.setLevel(numeric_level)
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
    file_handler = logging.FileHandler(log_path, encoding="utf-8")
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

