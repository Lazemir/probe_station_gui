"""Crash diagnostics helpers for native GUI failures."""

from __future__ import annotations

import faulthandler
import os
import platform
import sys
import threading
import traceback
from datetime import datetime
from pathlib import Path
from types import TracebackType
from typing import TextIO

_diagnostics_file: TextIO | None = None
_previous_threading_excepthook = threading.excepthook
_previous_sys_excepthook = sys.excepthook
_header_written = False


def configure_crash_diagnostics() -> Path:
    """Enable best-effort diagnostics for crashes below Python's exception layer."""

    path = _diagnostics_log_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    global _diagnostics_file, _header_written
    _diagnostics_file = path.open("a", encoding="utf-8", buffering=1)
    _header_written = False
    faulthandler.enable(file=_diagnostics_file, all_threads=True)
    threading.excepthook = _threading_excepthook
    sys.excepthook = _sys_excepthook
    return path


def _diagnostics_log_path() -> Path:
    if platform.system() == "Windows":
        base = os.environ.get("LOCALAPPDATA")
        if base:
            return Path(base) / "ProbeStationGUI" / "Logs" / "crash-diagnostics.log"
        return (
            Path.home()
            / "AppData"
            / "Local"
            / "ProbeStationGUI"
            / "Logs"
            / "crash-diagnostics.log"
        )
    if platform.system() == "Darwin":
        return Path.home() / "Library" / "Logs" / "ProbeStationGUI" / "crash-diagnostics.log"
    xdg_state = os.environ.get("XDG_STATE_HOME")
    if xdg_state:
        return Path(xdg_state) / "probe-station-gui" / "crash-diagnostics.log"
    return Path.home() / ".local" / "state" / "probe-station-gui" / "crash-diagnostics.log"


def _write_header(handle: TextIO) -> None:
    handle.write("\n")
    handle.write("=" * 80 + "\n")
    handle.write(f"Started: {datetime.now().isoformat(timespec='seconds')}\n")
    handle.write(f"PID: {os.getpid()}\n")
    handle.write(f"Executable: {sys.executable}\n")
    handle.write(f"Python: {sys.version.replace(chr(10), ' ')}\n")
    handle.write(f"Platform: {platform.platform()}\n")
    handle.write(f"CWD: {Path.cwd()}\n")
    handle.write(f"argv: {sys.argv!r}\n")
    handle.flush()


def _write_header_once(handle: TextIO) -> None:
    global _header_written
    if _header_written:
        return
    _write_header(handle)
    _header_written = True


def _write_exception(
    label: str,
    exc_type: type[BaseException],
    exc_value: BaseException,
    exc_traceback: TracebackType | None,
) -> None:
    handle = _diagnostics_file
    if handle is None:
        return
    _write_header_once(handle)
    handle.write("\n")
    handle.write(f"{datetime.now().isoformat(timespec='milliseconds')} {label}\n")
    traceback.print_exception(exc_type, exc_value, exc_traceback, file=handle)
    handle.flush()


def _threading_excepthook(args: threading.ExceptHookArgs) -> None:
    _write_exception(
        f"Unhandled thread exception in {args.thread.name if args.thread else '<unknown>'}",
        args.exc_type,
        args.exc_value,
        args.exc_traceback,
    )
    _previous_threading_excepthook(args)


def _sys_excepthook(
    exc_type: type[BaseException],
    exc_value: BaseException,
    exc_traceback: TracebackType | None,
) -> None:
    _write_exception("Unhandled main thread exception", exc_type, exc_value, exc_traceback)
    _previous_sys_excepthook(exc_type, exc_value, exc_traceback)
