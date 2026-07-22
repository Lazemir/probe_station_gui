"""Internal FluidNC serial dialogue helpers for stage control."""

from __future__ import annotations

import logging
import time
from collections.abc import Iterator
from dataclasses import dataclass
from typing import Callable

import serial

from probe_station_gui.stage.errors import SERIAL_IO_EXCEPTIONS, StageControllerError
from probe_station_gui.stage.types import _Status


logger = logging.getLogger(__name__)

_SERIAL_IO_EXCEPTIONS = SERIAL_IO_EXCEPTIONS
_SERIAL_WAITING_EXCEPTIONS = SERIAL_IO_EXCEPTIONS + (ValueError,)


@dataclass(frozen=True)
class FluidNCSessionCallbacks:
    """Controller-owned callbacks used to interpret serial dialogue."""

    check_cancelled: Callable[[], None]
    raise_if_controller_reboot_line: Callable[[str, str], None]
    handle_limit_line: Callable[[str], None]
    handle_homing_message_line: Callable[[str], bool]
    handle_coordinate_state_line: Callable[[str], None]
    parse_status_line: Callable[[str], _Status | None]
    extract_status_homed_axes: Callable[[str], set[str] | None]
    update_homing_status: Callable[[set[str]], None]
    handle_pending_serial_data_side_effects: Callable[[bytes, str], None]


@dataclass(slots=True)
class FluidNCSession:
    """Bound serial session exposing small FluidNC dialogue primitives."""

    serial_connection: serial.Serial
    callbacks: FluidNCSessionCallbacks

    def write_command(
        self,
        command: str,
        *,
        check_cancelled: bool = True,
    ) -> None:
        if check_cancelled:
            self.callbacks.check_cancelled()
        self.discard_pending_input(reason=f"before command {command.strip()}")
        data = (command.strip() + "\n").encode("ascii")
        try:
            logger.debug("SERIAL TRACE stage_write command=%s", command.strip())
            self.serial_connection.write(data)
            self.serial_connection.flush()
            logger.debug(
                "SERIAL TRACE stage_write_flushed command=%s",
                command.strip(),
            )
        except _SERIAL_IO_EXCEPTIONS as exc:  # pragma: no cover - hardware interaction
            raise StageControllerError(f"Serial write failed: {exc}") from exc

    def wait_for_ok(
        self,
        timeout: float = 5.0,
        *,
        check_cancelled: bool = True,
    ) -> None:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if check_cancelled:
                self.callbacks.check_cancelled()
            line = self._readline(source="wait_for_ok")
            if not line:
                continue
            self.callbacks.raise_if_controller_reboot_line(line, "wait_for_ok")
            self.callbacks.handle_limit_line(line)
            if self.callbacks.handle_homing_message_line(line):
                continue
            lower = line.lower()
            if lower == "ok":
                return
            if lower.startswith("alarm"):
                raise StageControllerError(f"Controller alarm: {line}")
            if line.startswith("[MSG:ERR:"):
                raise StageControllerError(f"Controller reported: {line}")
            if lower.startswith("error"):
                raise StageControllerError(f"Controller reported: {line}")
        raise StageControllerError("Timeout waiting for controller acknowledgement.")

    def discard_pending_input(
        self,
        *,
        reason: str = "status query",
    ) -> bytes:
        try:
            waiting = int(getattr(self.serial_connection, "in_waiting", 0) or 0)
        except _SERIAL_WAITING_EXCEPTIONS:
            waiting = 0
        if waiting <= 0:
            return b""
        try:
            if hasattr(self.serial_connection, "read"):
                data = self.serial_connection.read(waiting)
                logger.debug(
                    "SERIAL TRACE discard_pending_input reason=%s bytes=%r",
                    reason,
                    data[:200],
                )
                self.callbacks.handle_pending_serial_data_side_effects(data, reason)
                return data
            self.serial_connection.reset_input_buffer()
            logger.debug(
                "SERIAL TRACE discard_pending_input reason=%s bytes=%d",
                reason,
                waiting,
            )
        except _SERIAL_IO_EXCEPTIONS:
            logger.debug("Serial input buffer reset failed after stale input.")
        return b""

    def read_status_frame(
        self,
        *,
        timeout: float,
        check_cancelled: bool = True,
        parse_status_line: Callable[[str], _Status | None] | None = None,
    ) -> _Status | None:
        try:
            self.discard_pending_input(reason="before status query")
            logger.debug("SERIAL TRACE stage_query_status write=?")
            self.serial_connection.write(b"?\n")
            self.serial_connection.flush()
            logger.debug("SERIAL TRACE stage_query_status flushed=?")
        except _SERIAL_IO_EXCEPTIONS as exc:  # pragma: no cover - hardware interaction
            raise StageControllerError(f"Serial query failed: {exc}") from exc
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if check_cancelled:
                self.callbacks.check_cancelled()
            line = self._readline(source="query_status")
            if not line:
                continue
            self.callbacks.raise_if_controller_reboot_line(line, "status query")
            self.callbacks.handle_limit_line(line)
            if line.lower().startswith("alarm"):
                raise StageControllerError(f"Controller alarm: {line}")
            if self.callbacks.handle_homing_message_line(line):
                continue
            self.callbacks.handle_coordinate_state_line(line)
            parser = parse_status_line or self.callbacks.parse_status_line
            status = parser(line)
            if status is None:
                continue
            homed_axes = self.callbacks.extract_status_homed_axes(line)
            if homed_axes is not None:
                status.homed_axes = set(homed_axes)
                self.callbacks.update_homing_status(status.homed_axes)
            return status
        return None

    def read_pending_output(
        self,
        *,
        max_bytes: int | None = None,
        reason: str = "terminal pending read",
    ) -> bytes:
        try:
            waiting = int(getattr(self.serial_connection, "in_waiting", 0) or 0)
        except _SERIAL_WAITING_EXCEPTIONS as exc:
            raise StageControllerError(f"Serial read failed: {exc}") from exc
        if waiting <= 0:
            return b""
        if max_bytes is not None:
            waiting = min(waiting, max(1, int(max_bytes)))
        logger.debug("SERIAL TRACE terminal_in_waiting bytes=%s", waiting)
        try:
            data = self.serial_connection.read(waiting)
        except _SERIAL_IO_EXCEPTIONS as exc:  # pragma: no cover - hardware interaction
            raise StageControllerError(f"Serial read failed: {exc}") from exc
        if data:
            logger.debug("SERIAL TRACE terminal_read bytes=%r", data[:200])
            self.callbacks.handle_pending_serial_data_side_effects(data, reason)
        return data

    def reset_input_buffer(
        self,
        *,
        reason: str = "serial dialogue reset",
    ) -> None:
        try:
            self.serial_connection.reset_input_buffer()
            logger.debug("SERIAL TRACE reset_input_buffer reason=%s", reason)
        except _SERIAL_IO_EXCEPTIONS:
            logger.debug("Serial input buffer reset failed.")

    def iter_command_response_lines(
        self,
        command: str,
        *,
        timeout: float,
        source: str,
        check_cancelled: bool = True,
        reset_input_before_command: bool = False,
        reset_input_reason: str | None = None,
        handle_limit_lines: bool = True,
        handle_homing_messages: bool = False,
        handle_coordinate_state: bool = False,
    ) -> Iterator[str]:
        if reset_input_before_command:
            self.reset_input_buffer(
                reason=reset_input_reason or f"before command {command.strip()}"
            )
        self.write_command(command, check_cancelled=check_cancelled)
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if check_cancelled:
                self.callbacks.check_cancelled()
            line = self._readline(source=source)
            if not line:
                continue
            self.callbacks.raise_if_controller_reboot_line(line, source)
            if handle_limit_lines:
                self.callbacks.handle_limit_line(line)
            if handle_homing_messages and self.callbacks.handle_homing_message_line(
                line
            ):
                continue
            lower = line.lower()
            if lower.startswith("alarm"):
                raise StageControllerError(f"Controller alarm: {line}")
            if line.startswith("[MSG:ERR:") or lower.startswith("error"):
                raise StageControllerError(f"Controller reported: {line}")
            if handle_coordinate_state:
                self.callbacks.handle_coordinate_state_line(line)
            yield line

    def _readline(self, *, source: str) -> str:
        try:
            raw = self.serial_connection.readline()
        except _SERIAL_IO_EXCEPTIONS as exc:  # pragma: no cover - hardware interaction
            raise StageControllerError(f"Serial read failed: {exc}") from exc
        line = raw.decode("ascii", errors="ignore").strip()
        if line:
            logger.debug("SERIAL TRACE stage_readline %s line=%r", source, line)
        return line
