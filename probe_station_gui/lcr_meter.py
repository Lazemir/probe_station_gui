"""GW Instek LCR meter integration used for needle height calibration."""

from __future__ import annotations

import logging
import threading
import time
from dataclasses import dataclass
from typing import Optional

from PySide6.QtCore import QObject, Signal

from probe_station_gui.gwinstek_lcr_76200 import GWInstekLCR76200, normalize_resource_name


logger = logging.getLogger(__name__)


class LCRMeterError(RuntimeError):
    """Raised when the LCR meter backend cannot complete the request."""


@dataclass(frozen=True)
class ResistanceReading:
    """Single parsed resistance reading from the meter."""

    resistance_ohm: float
    is_short: bool


class _LCRBackend:
    """Small interface implemented by the concrete LCR backends."""

    backend_name = "unknown"

    def identify(self) -> str:
        raise NotImplementedError

    def configure_for_resistance(self) -> None:
        raise NotImplementedError

    def read_resistance_ohm(self) -> float:
        raise NotImplementedError

    def close(self) -> None:
        raise NotImplementedError


class _QcodesBackend(_LCRBackend):
    """Backend using the dedicated QCoDeS driver for the GW Instek meter."""

    backend_name = "qcodes"
    POST_CONFIG_SETTLE_S = 0.2

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

    def configure_for_resistance(self) -> None:
        try:
            self._instrument.function("DCR")
            self._instrument.range_mode("AUTO")
            self._instrument.trigger_source("INT")
            self._instrument.disable_bias()
            # The meter needs a brief settle time before the first FETCH after
            # a burst of configuration commands, otherwise it can time out.
            time.sleep(self.POST_CONFIG_SETTLE_S)
        except Exception as exc:  # pragma: no cover - backend specific failures
            raise LCRMeterError(f"Unable to configure resistance mode: {exc}") from exc

    def read_resistance_ohm(self) -> float:
        try:
            return float(self._instrument.read_resistance())
        except Exception as exc:  # pragma: no cover - backend specific failures
            raise LCRMeterError(f"LCR fetch failed: {exc}") from exc

    def close(self) -> None:
        self._instrument.close()


def _create_backend(address: str, timeout_ms: int) -> _LCRBackend:
    """Create the QCoDeS-backed backend for the LCR meter."""

    return _QcodesBackend(address, timeout_ms)


class LCRMeterController(QObject):
    """Manage connection and polling for the external LCR meter."""

    connection_changed: Signal = Signal(bool, str, str)
    reading_updated: Signal = Signal(float, bool)
    status_message: Signal = Signal(str)

    DEFAULT_TIMEOUT_MS = 2000

    def __init__(self) -> None:
        super().__init__()
        self._resource_name = ""
        self._short_threshold_ohm = 10.0
        self._poll_interval_ms = 250
        self._backend: Optional[_LCRBackend] = None
        self._backend_description = ""
        self._task_lock = threading.Lock()
        self._active_thread: Optional[threading.Thread] = None
        self._poll_thread: Optional[threading.Thread] = None
        self._stop_polling = threading.Event()

    def apply_configuration(
        self,
        *,
        resource_name: str,
        short_threshold_ohm: float,
        poll_interval_ms: int,
    ) -> None:
        """Store the runtime configuration used by future connections."""

        self._resource_name = resource_name.strip()
        self._short_threshold_ohm = max(0.0, float(short_threshold_ohm))
        self._poll_interval_ms = max(50, int(poll_interval_ms))

    def is_connected(self) -> bool:
        """Return True when the LCR backend is connected."""

        return self._backend is not None

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
        backend = self._backend
        self._backend = None
        if backend is not None:
            try:
                backend.close()
            except Exception:  # pragma: no cover - best effort shutdown
                logger.exception("Failed to close LCR backend during shutdown")

    def _run_connect(self) -> None:
        try:
            if not self._resource_name:
                raise LCRMeterError("LCR resource is empty. Set it in Settings.")
            self._disconnect_backend()
            backend = _create_backend(self._resource_name, self.DEFAULT_TIMEOUT_MS)
            instrument_id = backend.identify()
            backend.configure_for_resistance()
            initial_resistance_ohm = backend.read_resistance_ohm()
            self._backend = backend
            self._backend_description = f"{backend.backend_name}: {instrument_id}"
            self._stop_polling.clear()
            self.connection_changed.emit(
                True, backend.backend_name, instrument_id or self._resource_name
            )
            self.reading_updated.emit(
                initial_resistance_ohm,
                initial_resistance_ohm <= self._short_threshold_ohm,
            )
            self.status_message.emit(f"LCR connected via {backend.backend_name}.")
            self._start_polling_thread()
        except LCRMeterError as exc:
            self._disconnect_backend()
            self.connection_changed.emit(False, "", str(exc))
            self.status_message.emit(f"LCR connection failed: {exc}")
        finally:
            with self._task_lock:
                self._active_thread = None

    def _run_disconnect(self) -> None:
        try:
            self._disconnect_backend()
            self.connection_changed.emit(False, "", "Disconnected")
            self.status_message.emit("LCR disconnected.")
        finally:
            with self._task_lock:
                self._active_thread = None

    def _disconnect_backend(self) -> None:
        self._stop_polling.set()
        poll_thread = self._poll_thread
        self._poll_thread = None
        if (
            poll_thread
            and poll_thread.is_alive()
            and poll_thread is not threading.current_thread()
        ):
            poll_thread.join(timeout=2.0)
        backend = self._backend
        self._backend = None
        self._backend_description = ""
        if backend is not None:
            try:
                backend.close()
            except Exception:  # pragma: no cover - best effort cleanup
                logger.exception("Failed to close LCR backend cleanly")

    def _start_polling_thread(self) -> None:
        if self._poll_thread and self._poll_thread.is_alive():
            return
        thread = threading.Thread(target=self._poll_readings, daemon=True)
        self._poll_thread = thread
        thread.start()

    def _poll_readings(self) -> None:
        while not self._stop_polling.is_set():
            backend = self._backend
            if backend is None:
                return
            try:
                resistance_ohm = backend.read_resistance_ohm()
            except LCRMeterError as exc:
                self.status_message.emit(f"LCR read failed: {exc}")
                self.connection_changed.emit(False, "", str(exc))
                self._disconnect_backend()
                return
            is_short = resistance_ohm <= self._short_threshold_ohm
            self.reading_updated.emit(resistance_ohm, is_short)
            time.sleep(self._poll_interval_ms / 1000.0)
