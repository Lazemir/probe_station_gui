"""GW Instek LCR meter integration used for needle height calibration."""

from __future__ import annotations

import logging
import math
import threading
import time
from typing import Optional

from PySide6.QtCore import QObject, Signal

from probe_station_gui.gwinstek_lcr_76200 import GWInstekLCR76200, normalize_resource_name


logger = logging.getLogger(__name__)


class LCRMeterError(RuntimeError):
    """Raised when the LCR meter backend cannot complete the request."""


class _LCRSession:
    """Thin wrapper around the single supported GW Instek driver."""

    backend_name = "qcodes"
    CONFIG_COMMAND_DELAY_S = 0.2
    CONFIG_VERIFY_RETRIES = 3
    CONFIG_VERIFY_DELAY_S = 0.15
    POST_CONFIG_SETTLE_S = 0.2
    OVERLOAD_RESISTANCE_OHM = 9.9e19

    def __init__(self, address: str, timeout_ms: int) -> None:
        normalized_address = normalize_resource_name(address)
        try:
            self._instrument = GWInstekLCR76200(
                name="gwinstek_lcr76200",
                address=normalized_address,
                timeout=max(0.1, timeout_ms / 1000.0),
            )
        except ImportError as exc:
            raise LCRMeterError(
                "QCoDeS LCR driver is unavailable. Install optional dependencies "
                "with `pip install .[lcr]`."
            ) from exc
        except Exception as exc:  # pragma: no cover - backend specific failures
            raise LCRMeterError(
                f"Unable to open LCR resource {normalized_address}: {exc}"
            ) from exc

    def identify(self) -> str:
        try:
            idn = self._instrument.get_idn()
        except Exception as exc:  # pragma: no cover - backend specific failures
            raise LCRMeterError(f"LCR identify query failed: {exc}") from exc
        parts = (
            idn.get("model"),
            idn.get("serial"),
            idn.get("firmware"),
            idn.get("vendor"),
        )
        return ", ".join(part for part in parts if part)

    def configure_for_resistance(self, dcr_range: int) -> None:
        try:
            configuration_steps = (
                ("FUNC DCR", "FUNC?", "DCR"),
                ("TRIG:SOUR INT", "TRIG:SOUR?", "INT"),
                ("BIAS OFF", "BIAS?", "OFF"),
                ("FUNC:RANG:AUTO HOLD", "FUNC:RANG:AUTO?", "HOLD"),
                (f"FUNC:DCR:RANG {int(dcr_range)}", "FUNC:DCR:RANG?", str(int(dcr_range))),
                ("APER FAST", "APER?", "FAST"),
            )
            for command, query, expected in configuration_steps:
                self._write_and_verify(command, query, expected)
            logger.info(
                "Configured LCR for DCR measurement: range=%s aperture=FAST trigger=INT",
                int(dcr_range),
            )
            time.sleep(self.POST_CONFIG_SETTLE_S)
        except Exception as exc:  # pragma: no cover - backend specific failures
            raise LCRMeterError(f"Unable to configure resistance mode: {exc}") from exc

    def _write_and_verify(self, command: str, query: str, expected_token: str) -> None:
        last_response = ""
        normalized_expected = expected_token.strip().upper()
        for attempt in range(self.CONFIG_VERIFY_RETRIES):
            self._instrument.write(command)
            time.sleep(self.CONFIG_COMMAND_DELAY_S)
            response = str(self._instrument.ask(query)).strip()
            normalized_response = response.split(",", 1)[0].strip().upper()
            if normalized_response == normalized_expected:
                return
            last_response = response
            logger.warning(
                "LCR config verification mismatch after %s: expected %s from %s, got %s (attempt %s/%s)",
                command,
                expected_token,
                query,
                response,
                attempt + 1,
                self.CONFIG_VERIFY_RETRIES,
            )
            time.sleep(self.CONFIG_VERIFY_DELAY_S)
        raise LCRMeterError(
            f"LCR rejected configuration command {command!r}: "
            f"{query} returned {last_response!r}, expected {expected_token!r}"
        )

    def read_resistance_ohm(self) -> float:
        try:
            resistance_ohm = float(self._instrument.read_resistance())
        except Exception as exc:  # pragma: no cover - backend specific failures
            raise LCRMeterError(f"LCR fetch failed: {exc}") from exc
        if (
            not math.isfinite(resistance_ohm)
            or abs(resistance_ohm) >= self.OVERLOAD_RESISTANCE_OHM
        ):
            return math.inf
        return resistance_ohm

    def close(self) -> None:
        self._instrument.close()


class LCRMeterController(QObject):
    """Manage connection and polling for the external LCR meter."""

    connection_changed: Signal = Signal(bool, str, str)
    reading_updated: Signal = Signal(float, bool)
    status_message: Signal = Signal(str)

    DEFAULT_TIMEOUT_MS = 2000

    def __init__(self) -> None:
        super().__init__()
        self._resource_name = ""
        self._dcr_range = 3
        self._short_threshold_ohm = 10.0
        self._poll_interval_ms = 250
        self._session: Optional[_LCRSession] = None
        self._task_lock = threading.Lock()
        self._active_thread: Optional[threading.Thread] = None
        self._poll_thread: Optional[threading.Thread] = None
        self._stop_polling = threading.Event()

    def apply_configuration(
        self,
        *,
        resource_name: str,
        dcr_range: int,
        short_threshold_ohm: float,
        poll_interval_ms: int,
    ) -> None:
        """Store the runtime configuration used by future connections."""

        self._resource_name = resource_name.strip()
        self._dcr_range = max(0, min(8, int(dcr_range)))
        self._short_threshold_ohm = max(0.0, float(short_threshold_ohm))
        self._poll_interval_ms = max(50, int(poll_interval_ms))

    def is_connected(self) -> bool:
        """Return True when the LCR backend is connected."""

        return self._session is not None

    def request_connect(self) -> None:
        """Open the configured LCR resource in a background thread."""

        with self._task_lock:
            if self._active_thread and self._active_thread.is_alive():
                self.status_message.emit("LCR meter task already running.")
                return
            thread = threading.Thread(target=self._run_connect, daemon=True)
            self._active_thread = thread
            thread.start()

    def request_disconnect(self) -> None:
        """Close the current LCR resource and stop polling."""

        with self._task_lock:
            if self._active_thread and self._active_thread.is_alive():
                self.status_message.emit("LCR meter task already running.")
                return
            thread = threading.Thread(target=self._run_disconnect, daemon=True)
            self._active_thread = thread
            thread.start()

    def shutdown(self) -> None:
        """Stop background work before application exit."""

        self._stop_polling.set()
        poll_thread = self._poll_thread
        if poll_thread and poll_thread.is_alive():
            poll_thread.join(timeout=2.0)
        session = self._session
        self._session = None
        if session is not None:
            try:
                session.close()
            except Exception:  # pragma: no cover - best effort shutdown
                logger.exception("Failed to close LCR session during shutdown")

    def _run_connect(self) -> None:
        try:
            if not self._resource_name:
                raise LCRMeterError("LCR resource is empty. Set it in Settings.")
            self._disconnect_session()
            session = _LCRSession(self._resource_name, self.DEFAULT_TIMEOUT_MS)
            instrument_id = session.identify()
            session.configure_for_resistance(self._dcr_range)
            initial_resistance_ohm = session.read_resistance_ohm()
            self._session = session
            self._stop_polling.clear()
            self.connection_changed.emit(
                True, session.backend_name, instrument_id or self._resource_name
            )
            self.reading_updated.emit(
                initial_resistance_ohm,
                math.isfinite(initial_resistance_ohm)
                and initial_resistance_ohm <= self._short_threshold_ohm,
            )
            self.status_message.emit(f"LCR connected via {session.backend_name}.")
            self._start_polling_thread()
        except LCRMeterError as exc:
            self._disconnect_session()
            self.connection_changed.emit(False, "", str(exc))
            self.status_message.emit(f"LCR connection failed: {exc}")
        finally:
            with self._task_lock:
                self._active_thread = None

    def _run_disconnect(self) -> None:
        try:
            self._disconnect_session()
            self.connection_changed.emit(False, "", "Disconnected")
            self.status_message.emit("LCR disconnected.")
        finally:
            with self._task_lock:
                self._active_thread = None

    def _disconnect_session(self) -> None:
        self._stop_polling.set()
        poll_thread = self._poll_thread
        self._poll_thread = None
        if (
            poll_thread
            and poll_thread.is_alive()
            and poll_thread is not threading.current_thread()
        ):
            poll_thread.join(timeout=2.0)
        session = self._session
        self._session = None
        if session is not None:
            try:
                session.close()
            except Exception:  # pragma: no cover - best effort cleanup
                logger.exception("Failed to close LCR session cleanly")

    def _start_polling_thread(self) -> None:
        if self._poll_thread and self._poll_thread.is_alive():
            return
        thread = threading.Thread(target=self._poll_readings, daemon=True)
        self._poll_thread = thread
        thread.start()

    def _poll_readings(self) -> None:
        while not self._stop_polling.is_set():
            session = self._session
            if session is None:
                return
            try:
                resistance_ohm = session.read_resistance_ohm()
            except LCRMeterError as exc:
                self.status_message.emit(f"LCR read failed: {exc}")
                self.connection_changed.emit(False, "", str(exc))
                self._disconnect_session()
                return
            is_short = (
                math.isfinite(resistance_ohm)
                and resistance_ohm <= self._short_threshold_ohm
            )
            self.reading_updated.emit(resistance_ohm, is_short)
            time.sleep(self._poll_interval_ms / 1000.0)
