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

    def configure_measurement(
        self,
        *,
        measurement_function: str,
        range_mode: str,
        impedance_range: int,
        dcr_range: int,
        frequency_hz: float,
        level_mode: str,
        voltage_level_v: float,
        current_level_a: float,
        source_resistance_ohm: int,
        aperture_rate: str,
        aperture_averages: int,
        trigger_source: str,
        trigger_delay_s: float,
        bias_enabled: bool,
        bias_level_v: float,
        monitor1: str,
        monitor2: str,
        alc_enabled: bool,
    ) -> None:
        try:
            measurement_function = str(measurement_function).strip() or "DCR"
            range_mode = str(range_mode).strip().upper() or "HOLD"
            dcr_mode = measurement_function.upper() == "DCR"
            configuration_steps = [
                (f"FUNC {measurement_function}", "FUNC?", measurement_function),
                (f"TRIG:SOUR {trigger_source}", "TRIG:SOUR?", trigger_source),
                (f"FUNC:RANG:AUTO {range_mode}", "FUNC:RANG:AUTO?", range_mode),
                (f"APER {aperture_rate}", "APER:RATE?", aperture_rate),
                (f"APER {int(aperture_averages)}", "APER:AVG?", str(int(aperture_averages))),
                (f"TRIG:DLY {float(trigger_delay_s)}", "TRIG:DLY?", str(float(trigger_delay_s))),
            ]
            if range_mode == "HOLD":
                if dcr_mode:
                    configuration_steps.append(
                        (
                            f"FUNC:DCR:RANG {int(dcr_range)}",
                            "FUNC:DCR:RANG?",
                            str(int(dcr_range)),
                        )
                    )
                else:
                    configuration_steps.append(
                        (
                            f"FUNC:IMP:RANG {int(impedance_range)}",
                            "FUNC:IMP:RANG?",
                            str(int(impedance_range)),
                        )
                    )
            if dcr_mode:
                configuration_steps.append(
                    ("BIAS OFF", "BIAS?", "OFF")
                )
            else:
                configuration_steps.extend(
                    (
                        (f"FREQ {float(frequency_hz)}", "FREQ?", str(float(frequency_hz))),
                        (
                            f"LEV:SRES {int(source_resistance_ohm)}",
                            "LEV:SRES?",
                            str(int(source_resistance_ohm)),
                        ),
                        (f"FUNC:MON1 {monitor1}", "FUNC:MON1?", monitor1),
                        (f"FUNC:MON2 {monitor2}", "FUNC:MON2?", monitor2),
                        (
                            f"LEV:ALC {'ON' if alc_enabled else 'OFF'}",
                            "LEV:ALC?",
                            "ON" if alc_enabled else "OFF",
                        ),
                    )
                )
                if level_mode.upper() == "CURRENT":
                    configuration_steps.append(
                        (
                            f"LEV:CURR {float(current_level_a)}",
                            "LEV:CURR?",
                            str(float(current_level_a)),
                        )
                    )
                else:
                    configuration_steps.append(
                        (
                            f"LEV:VOLT {float(voltage_level_v)}",
                            "LEV:VOLT?",
                            str(float(voltage_level_v)),
                        )
                    )
                if bias_enabled:
                    configuration_steps.append(
                        (
                            f"BIAS {float(bias_level_v)}",
                            "BIAS?",
                            str(float(bias_level_v)),
                        )
                    )
                else:
                    configuration_steps.append(
                        ("BIAS OFF", "BIAS?", "OFF")
                    )
            for command, query, expected in configuration_steps:
                self._write_and_verify(command, query, expected)
            logger.info(
                "Configured LCR: function=%s range_mode=%s impedance_range=%s dcr_range=%s frequency=%s level_mode=%s voltage=%s current=%s aperture=%s avg=%s trigger=%s",
                measurement_function,
                range_mode,
                int(impedance_range),
                int(dcr_range),
                float(frequency_hz),
                level_mode,
                float(voltage_level_v),
                float(current_level_a),
                aperture_rate,
                int(aperture_averages),
                trigger_source,
            )
            time.sleep(self.POST_CONFIG_SETTLE_S)
        except Exception as exc:  # pragma: no cover - backend specific failures
            raise LCRMeterError(f"Unable to configure LCR measurement: {exc}") from exc

    def configure_for_resistance(
        self, dcr_range: int, auto_range_enabled: bool
    ) -> None:
        try:
            configuration_steps = [
                ("FUNC DCR", "FUNC?", "DCR"),
                ("TRIG:SOUR INT", "TRIG:SOUR?", "INT"),
                ("BIAS OFF", "BIAS?", "OFF"),
            ]
            if auto_range_enabled:
                configuration_steps.append(
                    ("FUNC:RANG:AUTO AUTO", "FUNC:RANG:AUTO?", "AUTO")
                )
            else:
                configuration_steps.extend(
                    (
                        ("FUNC:RANG:AUTO HOLD", "FUNC:RANG:AUTO?", "HOLD"),
                        (
                            f"FUNC:DCR:RANG {int(dcr_range)}",
                            "FUNC:DCR:RANG?",
                            str(int(dcr_range)),
                        ),
                    )
                )
            configuration_steps.append(("APER FAST", "APER?", "FAST"))
            for command, query, expected in configuration_steps:
                self._write_and_verify(command, query, expected)
            logger.info(
                "Configured LCR for DCR measurement: auto_range=%s range=%s aperture=FAST trigger=INT",
                auto_range_enabled,
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
            if self._configuration_response_matches(
                normalized_response, normalized_expected
            ):
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

    @staticmethod
    def _configuration_response_matches(response: str, expected: str) -> bool:
        if response == expected:
            return True
        if response in {"ON", "1"} and expected in {"ON", "1"}:
            return True
        if response in {"OFF", "0"} and expected in {"OFF", "0"}:
            return True
        try:
            return math.isclose(
                _LCRSession._parse_numeric_response(response),
                float(expected),
                rel_tol=1e-6,
                abs_tol=1e-9,
            )
        except ValueError:
            return False

    @staticmethod
    def _parse_numeric_response(response: str) -> float:
        value = response.strip().upper()
        for suffix in ("OHM", "MS", "S", "V", "A"):
            if value.endswith(suffix):
                value = value[: -len(suffix)]
                break
        return float(value.strip())

    def read_primary_value(self, *, trigger: bool = False) -> float:
        try:
            if trigger:
                self._instrument.force_trigger()
            primary_value = self._instrument.fetch_main().primary
            if primary_value is None:
                raise ValueError("FETCH:MAIN? did not return a primary value")
            primary_value = float(primary_value)
        except Exception as exc:  # pragma: no cover - backend specific failures
            raise LCRMeterError(f"LCR fetch failed: {exc}") from exc
        if (
            not math.isfinite(primary_value)
            or abs(primary_value) >= self.OVERLOAD_RESISTANCE_OHM
        ):
            return math.inf
        return primary_value

    def read_resistance_ohm(self) -> float:
        return self.read_primary_value()

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
        self._connected_resource_name = ""
        self._measurement_function = "DCR"
        self._range_mode = "HOLD"
        self._auto_range_enabled = False
        self._impedance_range = 3
        self._dcr_range = 3
        self._frequency_hz = 1000.0
        self._level_mode = "VOLTAGE"
        self._voltage_level_v = 0.01
        self._current_level_a = 0.0001
        self._source_resistance_ohm = 30
        self._aperture_rate = "FAST"
        self._aperture_averages = 1
        self._trigger_source = "INT"
        self._trigger_delay_s = 0.0
        self._bias_enabled = False
        self._bias_level_v = 0.0
        self._monitor1 = "OFF"
        self._monitor2 = "OFF"
        self._alc_enabled = False
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
        measurement_function: str,
        range_mode: str,
        auto_range_enabled: bool,
        impedance_range: int,
        dcr_range: int,
        frequency_hz: float,
        level_mode: str,
        voltage_level_v: float,
        current_level_a: float,
        source_resistance_ohm: int,
        aperture_rate: str,
        aperture_averages: int,
        trigger_source: str,
        trigger_delay_s: float,
        bias_enabled: bool,
        bias_level_v: float,
        monitor1: str,
        monitor2: str,
        alc_enabled: bool,
        short_threshold_ohm: float,
        poll_interval_ms: int,
    ) -> None:
        """Store the runtime configuration used by future connections."""

        self._resource_name = resource_name.strip()
        self._measurement_function = str(measurement_function).strip() or "DCR"
        self._range_mode = str(range_mode).strip().upper() or "HOLD"
        self._auto_range_enabled = bool(auto_range_enabled)
        self._impedance_range = max(0, min(8, int(impedance_range)))
        self._dcr_range = max(0, min(8, int(dcr_range)))
        self._frequency_hz = max(10.0, float(frequency_hz))
        self._level_mode = str(level_mode).strip().upper() or "VOLTAGE"
        self._voltage_level_v = max(0.0, float(voltage_level_v))
        self._current_level_a = max(0.0, float(current_level_a))
        self._source_resistance_ohm = int(source_resistance_ohm)
        self._aperture_rate = str(aperture_rate).strip().upper() or "FAST"
        self._aperture_averages = max(1, min(256, int(aperture_averages)))
        self._trigger_source = str(trigger_source).strip().upper() or "INT"
        self._trigger_delay_s = max(0.0, float(trigger_delay_s))
        self._bias_enabled = bool(bias_enabled)
        self._bias_level_v = max(-2.5, min(2.5, float(bias_level_v)))
        self._monitor1 = str(monitor1).strip().upper() or "OFF"
        self._monitor2 = str(monitor2).strip().upper() or "OFF"
        self._alc_enabled = bool(alc_enabled)
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

    def request_reconfigure(self) -> None:
        """Apply the current configuration to an already connected meter."""

        with self._task_lock:
            if self._session is None:
                return
            if self._active_thread and self._active_thread.is_alive():
                self.status_message.emit("LCR meter task already running.")
                return
            thread = threading.Thread(target=self._run_reconfigure, daemon=True)
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
            logger.info(
                "Connected to LCR resource %s (%s)",
                self._resource_name,
                instrument_id or "IDN unavailable",
            )
            self._configure_session(session)
            initial_value = session.read_primary_value(trigger=self._uses_bus_trigger())
            self._session = session
            self._connected_resource_name = self._resource_name
            self._stop_polling.clear()
            self.connection_changed.emit(True, session.backend_name, self._resource_name)
            self.reading_updated.emit(
                initial_value,
                self._is_short_reading(initial_value),
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

    def _run_reconfigure(self) -> None:
        try:
            session = self._session
            if session is None:
                return
            desired_resource = self._resource_name.strip()
            connected_resource = self._connected_resource_name.strip()
            if not desired_resource:
                raise LCRMeterError("LCR resource is empty. Set it in Settings.")
            if normalize_resource_name(desired_resource) != normalize_resource_name(
                connected_resource
            ):
                self.status_message.emit("LCR resource changed. Reconnecting meter.")
                self._disconnect_session()
                replacement = _LCRSession(desired_resource, self.DEFAULT_TIMEOUT_MS)
                instrument_id = replacement.identify()
                logger.info(
                    "Reconnected to LCR resource %s (%s)",
                    desired_resource,
                    instrument_id or "IDN unavailable",
                )
                session = replacement
                self._session = session
                self._connected_resource_name = desired_resource
                self.connection_changed.emit(True, session.backend_name, desired_resource)
            else:
                self._stop_polling_session()
            initial_value = self._configure_active_session(session)
            self._stop_polling.clear()
            self.reading_updated.emit(
                initial_value,
                self._is_short_reading(initial_value),
            )
            self.status_message.emit("LCR settings applied.")
            self._start_polling_thread()
        except LCRMeterError as exc:
            self._disconnect_session()
            self.connection_changed.emit(False, "", str(exc))
            self.status_message.emit(f"LCR reconfiguration failed: {exc}")
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

    def _configure_active_session(self, session: _LCRSession) -> float:
        self._configure_session(session)
        return session.read_primary_value(trigger=self._uses_bus_trigger())

    def _configure_session(self, session: _LCRSession) -> None:
        session.configure_measurement(
            measurement_function=self._measurement_function,
            range_mode=self._range_mode,
            impedance_range=self._impedance_range,
            dcr_range=self._dcr_range,
            frequency_hz=self._frequency_hz,
            level_mode=self._level_mode,
            voltage_level_v=self._voltage_level_v,
            current_level_a=self._current_level_a,
            source_resistance_ohm=self._source_resistance_ohm,
            aperture_rate=self._aperture_rate,
            aperture_averages=self._aperture_averages,
            trigger_source=self._trigger_source,
            trigger_delay_s=self._trigger_delay_s,
            bias_enabled=self._bias_enabled,
            bias_level_v=self._bias_level_v,
            monitor1=self._monitor1,
            monitor2=self._monitor2,
            alc_enabled=self._alc_enabled,
        )

    def _is_short_reading(self, primary_value: float) -> bool:
        return (
            self._measurement_function.upper() == "DCR"
            and math.isfinite(primary_value)
            and primary_value <= self._short_threshold_ohm
        )

    def _uses_bus_trigger(self) -> bool:
        return self._trigger_source.upper() == "BUS"

    def _stop_polling_session(self) -> None:
        self._stop_polling.set()
        poll_thread = self._poll_thread
        self._poll_thread = None
        if (
            poll_thread
            and poll_thread.is_alive()
            and poll_thread is not threading.current_thread()
        ):
            poll_thread.join(timeout=2.0)

    def _disconnect_session(self) -> None:
        self._stop_polling_session()
        session = self._session
        self._session = None
        self._connected_resource_name = ""
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
                primary_value = session.read_primary_value(trigger=self._uses_bus_trigger())
            except LCRMeterError as exc:
                self.status_message.emit(f"LCR read failed: {exc}")
                self.connection_changed.emit(False, "", str(exc))
                self._disconnect_session()
                return
            self.reading_updated.emit(
                primary_value, self._is_short_reading(primary_value)
            )
            time.sleep(self._poll_interval_ms / 1000.0)
