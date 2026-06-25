"""Asynchronous serial write queue for stage control."""

from __future__ import annotations

import logging
import time
from queue import Empty

import serial

from probe_station_gui.stage.errors import SERIAL_IO_EXCEPTIONS, StageControllerError
from probe_station_gui.stage.types import _QueuedSerialWrite


logger = logging.getLogger(__name__)

_SERIAL_IO_EXCEPTIONS = SERIAL_IO_EXCEPTIONS


class StageControllerSerialWriteQueueMixin:
    """Internal queued and realtime serial write methods for StageController."""

    def _next_queued_write_sequence(self) -> int:
        sequence = self._queued_write_sequence
        self._queued_write_sequence += 1
        return sequence

    def _clear_pending_async_writes(self) -> None:
        self._async_write_clear_epoch += 1
        while True:
            try:
                self._async_write_queue.get_nowait()
                self._async_write_queue.task_done()
            except Empty:
                break

    def _run_async_write_worker(self) -> None:
        while not self._async_write_shutdown.is_set():
            job = self._async_write_queue.get()
            try:
                if job.kind == "shutdown":
                    return
                if job.kind == "jog_command" and not self._await_current_jog_command(job):
                    continue
                serial_connection = self._serial
                if serial_connection is None or not serial_connection.is_open:
                    continue
                with self._serial_session_lock:
                    if not self._queued_jog_command_is_current(job):
                        continue
                    self._write_async_job(serial_connection, job)
            except StageControllerError as exc:
                self.status_message.emit(str(exc))
            finally:
                self._async_write_queue.task_done()

    def _await_current_jog_command(self, job: _QueuedSerialWrite) -> bool:
        if job.kind != "jog_command":
            return True

        deadline = time.monotonic() + self.SERIAL_JOG_COMMAND_SETTLE_S
        while time.monotonic() < deadline:
            if self._async_write_shutdown.is_set():
                return False
            if not self._queued_jog_command_is_current(job):
                return False
            remaining = deadline - time.monotonic()
            time.sleep(min(0.005, remaining))

        return self._queued_jog_command_is_current(job)

    def _queued_jog_command_is_current(self, job: _QueuedSerialWrite) -> bool:
        if job.clear_epoch != self._async_write_clear_epoch:
            logger.debug(
                "TIMING %s_dropped_after_queue_clear command=%s epoch=%s current_epoch=%s",
                job.kind,
                job.description,
                job.clear_epoch,
                self._async_write_clear_epoch,
            )
            return False
        if job.kind != "jog_command":
            return True
        if job.generation == self._queued_jog_generation:
            return True
        logger.debug(
            "TIMING %s_dropped_superseded command=%s generation=%s current_generation=%s",
            job.kind,
            job.description,
            job.generation,
            self._queued_jog_generation,
        )
        return False

    def _write_async_job(
        self, serial_connection: serial.Serial, job: _QueuedSerialWrite
    ) -> None:
        try:
            if job.kind == "jog_command":
                logger.debug(
                    "TIMING jog_serial_write_begin command=%s", job.description
                )
            elif job.kind == "jog_stop":
                logger.debug("TIMING jog_stop_write_begin command=0x85")
            elif job.kind == "soft_reset":
                logger.debug("SERIAL TRACE terminal_write %s", job.description)
            elif job.kind == "feed_override":
                logger.debug("SERIAL TRACE realtime_write %s", job.description)
            elif job.kind == "terminal":
                logger.debug("SERIAL TRACE terminal_write payload=%r", job.description)
            serial_connection.write(job.payload)
            serial_connection.flush()
            if job.kind == "jog_command":
                self._last_jog_write_timestamp = time.monotonic()
                logger.debug("TIMING jog_serial_write_flushed command=%s", job.description)
            elif job.kind == "jog_stop":
                logger.debug("TIMING jog_stop_write_flushed command=0x85")
        except _SERIAL_IO_EXCEPTIONS as exc:  # pragma: no cover - hardware interaction
            raise StageControllerError(f"Serial write failed: {exc}") from exc

    def _reset_feed_override(self) -> None:
        self._write_realtime_payload(
            self.FEED_OVERRIDE_RESET,
            "feed override reset 100%",
        )
        with self._feed_override_lock:
            self._active_feed_override_percent = 100

    def _write_realtime_payload(
        self, payload: bytes, description: str
    ) -> None:
        serial_connection = self._current_serial()
        if not hasattr(serial_connection, "write"):
            logger.debug(
                "SERIAL TRACE realtime_write skipped for test serial: %s",
                description,
            )
            return
        try:
            logger.debug("SERIAL TRACE realtime_write %s", description)
            serial_connection.write(payload)
            serial_connection.flush()
        except _SERIAL_IO_EXCEPTIONS as exc:  # pragma: no cover - hardware interaction
            raise StageControllerError(f"Serial realtime write failed: {exc}") from exc
