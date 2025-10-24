"""Utilities for configuring application-wide logging."""

from __future__ import annotations

import logging
from pathlib import Path

_HANDLER_FLAG = "_probe_station_gui_managed"


def configure_logging(log_path: Path, level_name: str) -> None:
    """Configure the root logger to write to the specified file."""

    log_path = Path(log_path)
    log_path.parent.mkdir(parents=True, exist_ok=True)
    numeric_level = _normalise_level(level_name)
    root_logger = logging.getLogger()
    root_logger.setLevel(numeric_level)

    handler = _find_managed_handler(root_logger)
    if handler is None:
        handler = logging.FileHandler(log_path, encoding="utf-8")
        setattr(handler, _HANDLER_FLAG, True)
        root_logger.addHandler(handler)
    else:
        handler = _ensure_handler_destination(handler, log_path, root_logger)

    handler.setLevel(numeric_level)
    handler.setFormatter(
        logging.Formatter("%(asctime)s [%(levelname)s] %(name)s: %(message)s")
    )

    for existing in root_logger.handlers:
        if existing is handler:
            continue
        if getattr(existing, _HANDLER_FLAG, False):
            existing.setLevel(numeric_level)

    root_logger.debug("Logging configured: path=%s level=%s", log_path, level_name)


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

    if not isinstance(handler, logging.FileHandler):
        root_logger.removeHandler(handler)
        handler.close()
        new_handler = logging.FileHandler(log_path, encoding="utf-8")
        setattr(new_handler, _HANDLER_FLAG, True)
        root_logger.addHandler(new_handler)
        return new_handler

    current_path = Path(handler.baseFilename)
    if current_path != log_path:
        root_logger.removeHandler(handler)
        handler.close()
        new_handler = logging.FileHandler(log_path, encoding="utf-8")
        setattr(new_handler, _HANDLER_FLAG, True)
        root_logger.addHandler(new_handler)
        return new_handler

    return handler

