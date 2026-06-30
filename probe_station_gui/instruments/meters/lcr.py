"""Resistance-meter integrations used by calibration and route measurements."""

from __future__ import annotations

import atexit
import logging
import math
import threading
import time
import weakref
from contextlib import contextmanager
from typing import Callable, Iterator, Optional, TypeVar

from PySide6.QtCore import QObject, Signal

from probe_station_measure import OHMMETER_RANGE_MANUAL
from probe_station_gui.instruments.meters.worker import (
    MeterWorkerRuntime as _MeterWorkerRuntime,
    meter_worker_poll_timeout,
)
from probe_station_gui.instruments.meters.gwinstek_session import (
    GWInstekLCRSession as _GWInstekLCRSession,
)
from probe_station_gui.instruments.meters.lcr_helpers import (
    gpib_interface_resources_for as _gpib_interface_resources_for,
    normalize_resource_name,
    session_visa_resource_roles as _session_visa_resource_roles,
    voltage_sweep_point_to_dict as _voltage_sweep_point_to_dict,
)
from probe_station_gui.instruments.meters.lcr_route_session import (
    RouteSessionError as _RouteSessionError,
    configure_route_session as _configure_route_session,
    prepare_route_measurement_batch_on_session as _prepare_route_measurement_batch_on_session,
    read_route_measurement_batch_from_session as _read_route_measurement_batch_from_session,
    read_route_measurement_from_session as _read_route_measurement_from_session,
)
from probe_station_gui.instruments.meters.lcr_visa import (
    VisaOperationError as _VisaOperationError,
    session_visa_operation as _run_session_visa_operation,
)
from probe_station_gui.route.meter_config import (
    ROUTE_METER_GWINSTEK,
    ROUTE_METER_KEITHLEY,
    ROUTE_METER_LABELS,
    ROUTE_METER_TYPES,
    RouteMeterConfiguration,
)


logger = logging.getLogger(__name__)

_LCR_METER_CONTROLLERS: "weakref.WeakSet[LCRMeterController]" = weakref.WeakSet()
_SessionOperationResult = TypeVar("_SessionOperationResult")


def _shutdown_lcr_meter_controllers() -> None:
    for controller in list(_LCR_METER_CONTROLLERS):
        try:
            controller.shutdown()
        except Exception:
            logger.exception("Failed to shut down measurement instrument controller")


atexit.register(_shutdown_lcr_meter_controllers)


DEFAULT_METER_TIMEOUT_MS = 10000
KEITHLEY_LIVE_MEASUREMENT_VOLTAGE_V = 0.03
KEITHLEY_LIVE_SOURCE_VOLTAGE_RANGE_V = 0.21
KEITHLEY_LIVE_VOLTMETER_RANGE_V = 1.0
KEITHLEY_LIVE_CURRENT_RANGE_A = 10e-6
KEITHLEY_LIVE_COMPLIANCE_CURRENT_A = 9.5e-6
KEITHLEY_LIVE_NPLC = 1.0


def _reset_gpib_interfaces_for_resources(*resources: str | None) -> None:
    interfaces = _gpib_interface_resources_for(tuple(resources))
    if not interfaces:
        return
    try:
        import pyvisa
    except ImportError:
        logger.debug("PyVISA is unavailable; skipping GPIB interface reset.")
        return
    try:
        resource_manager = pyvisa.ResourceManager()
    except Exception as exc:  # pragma: no cover - backend specific failures
        logger.warning("Unable to create VISA resource manager for GPIB reset: %s", exc)
        return
    reset_any = False
    for interface_name in interfaces:
        try:
            interface = resource_manager.open_resource(interface_name)
        except Exception as exc:  # pragma: no cover - backend specific failures
            logger.warning("Unable to open %s for GPIB reset: %s", interface_name, exc)
            continue
        try:
            send_ifc = getattr(interface, "send_ifc", None)
            if callable(send_ifc):
                send_ifc()
                reset_any = True
                logger.info("Sent GPIB IFC on %s before Keithley connect.", interface_name)
        except Exception as exc:  # pragma: no cover - backend specific failures
            logger.warning("GPIB interface reset failed on %s: %s", interface_name, exc)
        finally:
            try:
                interface.close()
            except Exception:
                pass
    if reset_any:
        time.sleep(0.25)


def _session_visa_operation(
    session: object | None,
    role: str,
    operation: str,
    *,
    command: str | None,
    timeout_ms: int | None,
    read_termination: str | None,
    write_termination: str | None,
) -> object:
    try:
        return _run_session_visa_operation(
            session,
            role,
            operation,
            command=command,
            timeout_ms=timeout_ms,
            read_termination=read_termination,
            write_termination=write_termination,
        )
    except _VisaOperationError as exc:
        raise LCRMeterError(str(exc)) from exc


class LCRMeterError(RuntimeError):
    """Raised when the LCR meter backend cannot complete the request."""


_LCRSession = _GWInstekLCRSession
_LCRSession.error_type = LCRMeterError


def _open_keithley_session(
    source_resource: str,
    voltmeter_resource: str | None,
    timeout_ms: int,
) -> object:
    try:
        from probe_station_measure import Keithley2400With2182A
    except ImportError as exc:
        raise LCRMeterError(
            "Keithley route measurements require optional dependency "
            "'probe-station-measure'. Install with `pip install .[lcr]`."
        ) from exc
    source = normalize_resource_name(source_resource)
    voltmeter = normalize_resource_name(voltmeter_resource or "")
    try:
        _reset_gpib_interfaces_for_resources(source, voltmeter)
        return Keithley2400With2182A(
            source,
            voltmeter or None,
            timeout_ms=timeout_ms,
        )
    except Exception as exc:  # pragma: no cover - backend specific failures
        raise LCRMeterError(
            "Unable to open Keithley resources "
            f"{source_resource!r}, {voltmeter_resource!r}: {exc}"
        ) from exc


class RouteMeter:
    """Open, configure, and read one per-run route measurement backend."""

    def __init__(
        self,
        configuration: RouteMeterConfiguration,
        timeout_ms: int = DEFAULT_METER_TIMEOUT_MS,
    ) -> None:
        self._configuration = configuration
        self._timeout_ms = int(timeout_ms)
        self._session: object | None = None

    @property
    def backend_name(self) -> str:
        session = self._session
        if session is not None:
            return str(getattr(session, "backend_name", session.__class__.__name__))
        return ROUTE_METER_LABELS.get(
            self._configuration.meter_type, self._configuration.meter_type
        )

    def open(self) -> None:
        if self._session is not None:
            return
        if self._configuration.meter_type == ROUTE_METER_GWINSTEK:
            settings = self._configuration.gwinstek
            session = _LCRSession(settings.resource_name, self._timeout_ms)
            try:
                session.identify()
                self._configure_open_session(session, self._configuration)
            except Exception:
                session.close()
                raise
            self._session = session
            return
        if self._configuration.meter_type == ROUTE_METER_KEITHLEY:
            settings = self._configuration.keithley
            session = _open_keithley_session(
                settings.source_resource,
                settings.voltmeter_resource,
                self._timeout_ms,
            )
            try:
                identify = getattr(session, "identify", None)
                if callable(identify):
                    identify()
                self._configure_open_session(session, self._configuration)
            except Exception:
                closer = getattr(session, "close", None)
                if callable(closer):
                    closer()
                raise
            self._session = session
            return
        raise LCRMeterError(
            f"Unsupported route measurement instrument: {self._configuration.meter_type}"
        )

    def apply_route_meter_configuration(
        self,
        configuration: RouteMeterConfiguration,
    ) -> None:
        """Apply updated route measurement settings to the per-run backend."""

        if self._session is None:
            self._configuration = configuration
            return
        if configuration.meter_type != self._configuration.meter_type:
            configured_label = ROUTE_METER_LABELS.get(
                self._configuration.meter_type,
                self._configuration.meter_type,
            )
            requested_label = ROUTE_METER_LABELS.get(
                configuration.meter_type,
                configuration.meter_type,
            )
            raise LCRMeterError(
                f"Open route instrument is {configured_label}; route requested {requested_label}."
            )
        self._configure_open_session(self._session, configuration)
        self._configuration = configuration

    @staticmethod
    def _configure_open_session(
        session: object,
        configuration: RouteMeterConfiguration,
    ) -> None:
        try:
            _configure_route_session(
                session,
                configuration,
                gwinstek_session_type=_LCRSession,
                gwinstek_error="Open route instrument is not a GW Instek LCR.",
                keithley_error="Open route instrument is not a Keithley pair.",
            )
        except _RouteSessionError as exc:
            raise LCRMeterError(str(exc)) from exc

    def read_primary_value_now(self, *, restart_polling: bool = False) -> float:
        if self._session is None:
            self.open()
        if self._session is None:
            raise LCRMeterError("Route measurement instrument is not open.")
        reader = getattr(self._session, "read_primary_value", None)
        if not callable(reader):
            raise LCRMeterError("Route measurement instrument cannot read values.")
        return float(reader(trigger=True))

    def read_route_measurement_now(self, *, restart_polling: bool = False) -> dict[str, object]:
        _ = restart_polling
        if self._session is None:
            self.open()
        if self._session is None:
            raise LCRMeterError("Route measurement instrument is not open.")
        try:
            return _read_route_measurement_from_session(
                self._session,
                cannot_read_message="Route measurement instrument cannot read values.",
            )
        except _RouteSessionError as exc:
            raise LCRMeterError(str(exc)) from exc

    def read_route_measurement_batch_now(
        self,
        count: int,
        *,
        restart_polling: bool = False,
        after_measurement: object | None = None,
    ) -> list[dict[str, object]]:
        _ = restart_polling
        if self._session is None:
            self.open()
        if self._session is None:
            raise LCRMeterError("Route measurement instrument is not open.")
        count = max(1, int(count))
        try:
            return _read_route_measurement_batch_from_session(
                self._session,
                count,
                after_measurement=after_measurement,
                cannot_read_message="Route measurement instrument cannot read values.",
            )
        except _RouteSessionError as exc:
            raise LCRMeterError(str(exc)) from exc

    def prepare_route_measurement_batch_now(
        self,
        count: int,
        *,
        source_list_count: int | None = None,
    ) -> None:
        if self._session is None:
            self.open()
        if self._session is None:
            raise LCRMeterError("Route measurement instrument is not open.")
        _prepare_route_measurement_batch_on_session(
            self._session,
            max(1, int(count)),
            source_list_count=source_list_count,
        )

    @contextmanager
    def output(self, enabled: bool = True) -> Iterator["RouteMeter"]:
        if self._session is None:
            self.open()
        session = self._session
        output = getattr(session, "output", None)
        if not callable(output):
            yield self
            return
        with output(bool(enabled)):
            yield self

    def abort_current_measurement(self) -> None:
        session = self._session
        abort = getattr(session, "abort_measurement", None)
        if callable(abort):
            abort()

    def visa_resource_roles(self) -> dict[str, dict[str, object]]:
        if self._session is None:
            self.open()
        return _session_visa_resource_roles(
            self._session,
            meter_type=self._configuration.meter_type,
        )

    def visa_operation(
        self,
        role: str,
        operation: str,
        *,
        command: str | None = None,
        timeout_ms: int | None = None,
        read_termination: str | None = None,
        write_termination: str | None = None,
    ) -> object:
        if self._session is None:
            self.open()
        return _session_visa_operation(
            self._session,
            role,
            operation,
            command=command,
            timeout_ms=timeout_ms,
            read_termination=read_termination,
            write_termination=write_termination,
        )

    def close(self) -> None:
        session = self._session
        self._session = None
        if session is not None:
            closer = getattr(session, "close", None)
            if callable(closer):
                closer()


class LCRMeterController(QObject):
    """Manage connection and polling for the external measurement instrument."""

    connection_changed: Signal = Signal(bool, str, str)
    reading_started: Signal = Signal(int)
    reading_summary_updated: Signal = Signal(float, bool, int)
    reading_updated: Signal = Signal(float, bool)
    status_message: Signal = Signal(str)

    DEFAULT_TIMEOUT_MS = DEFAULT_METER_TIMEOUT_MS
    TASK_WAIT_TIMEOUT_S = 45.0

    def __init__(self) -> None:
        super().__init__()
        self._meter_type = ROUTE_METER_GWINSTEK
        self._resource_name = ""
        self._connected_resource_name = ""
        self._keithley_source_resource = ""
        self._keithley_voltmeter_resource = ""
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
        self._session: Optional[object] = None
        self._stop_polling = threading.Event()
        self._live_polling_enabled = True
        self._worker_runtime = _MeterWorkerRuntime(
            name="LCRMeterWorker",
            poll_timeout=self._meter_worker_poll_timeout,
            poll_once=self._run_meter_poll_once,
            logger=logger,
        )
        self._pending_route_meter_configuration: RouteMeterConfiguration | None = None
        self._shutdown_started = False
        self._live_output_context: object | None = None
        _LCR_METER_CONTROLLERS.add(self)

    def apply_configuration(
        self,
        *,
        meter_type: str = ROUTE_METER_GWINSTEK,
        resource_name: str,
        keithley_source_resource: str = "",
        keithley_voltmeter_resource: str = "",
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

        meter_type = str(meter_type).strip() or ROUTE_METER_GWINSTEK
        if meter_type not in ROUTE_METER_TYPES:
            meter_type = ROUTE_METER_GWINSTEK
        self._meter_type = meter_type
        self._resource_name = resource_name.strip()
        self._keithley_source_resource = keithley_source_resource.strip()
        self._keithley_voltmeter_resource = keithley_voltmeter_resource.strip()
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
        """Return True when the measurement backend is connected."""

        return self._session is not None

    def live_polling_enabled(self) -> bool:
        """Return True when standby resistance polling is enabled."""

        return self._live_polling_enabled

    def set_live_polling_enabled(self, enabled: bool) -> None:
        """Enable or disable standby resistance polling."""

        self._live_polling_enabled = bool(enabled)
        if not self._live_polling_enabled:
            self._pause_live_polling()
            return
        self._resume_live_polling()

    def wait_until_idle(self, timeout_s: float | None = None) -> bool:
        """Wait until the current background meter task finishes."""

        return self._worker_runtime.wait_until_idle(
            timeout_s=timeout_s,
            default_timeout_s=self.TASK_WAIT_TIMEOUT_S,
        )

    def _wake_meter_worker(self) -> None:
        self._worker_runtime.wake()

    def _run_on_meter_worker(self, func: Callable[[], object]) -> object:
        return self._worker_runtime.run(func)

    def _submit_meter_worker_call(self, func: Callable[[], object]) -> bool:
        return self._worker_runtime.submit(func)

    def _meter_worker_poll_timeout(self) -> float | None:
        return meter_worker_poll_timeout(
            live_polling_enabled=self._live_polling_enabled,
            stop_polling=self._stop_polling.is_set(),
            has_session=self._session is not None,
            poll_interval_ms=self._poll_interval_ms,
        )

    def meter_type(self) -> str:
        """Return the currently configured meter type."""

        return self._meter_type

    def connection_label(self) -> str:
        """Return a human readable connection target for the configured meter."""

        if self._meter_type == ROUTE_METER_KEITHLEY:
            source = self._keithley_source_resource or "source not configured"
            if self._keithley_voltmeter_resource:
                return (
                    f"Keithley 2400 {source}; "
                    f"2182A {self._keithley_voltmeter_resource}"
                )
            return f"Keithley 2400 {source}"
        return self._resource_name

    def apply_route_meter_configuration(
        self,
        configuration: RouteMeterConfiguration,
    ) -> None:
        """Apply per-run measurement settings to the connected meter."""

        if configuration.meter_type != self._meter_type:
            configured_label = ROUTE_METER_LABELS.get(
                self._meter_type, self._meter_type
            )
            requested_label = ROUTE_METER_LABELS.get(
                configuration.meter_type, configuration.meter_type
            )
            raise LCRMeterError(
                f"Connected instrument is {configured_label}; route requested {requested_label}."
            )
        self._pending_route_meter_configuration = configuration
        self._run_on_meter_worker(
            lambda: self._apply_route_meter_configuration_on_worker(configuration)
        )

    def _apply_route_meter_configuration_on_worker(
        self,
        configuration: RouteMeterConfiguration,
    ) -> None:
        session = self._session
        if session is None:
            raise LCRMeterError("Measurement instrument is not connected.")
        self._stop_polling_session()
        try:
            if session is not self._session:
                raise LCRMeterError("Measurement instrument is not connected.")
            self._apply_route_meter_configuration_to_session(
                session,
                configuration,
            )
        except Exception as exc:
            failure: BaseException = exc
            if (
                configuration.meter_type == ROUTE_METER_KEITHLEY
                and bool(self._connection_key())
            ):
                try:
                    self.status_message.emit(
                        "Instrument setup failed; reconnecting Keithley."
                    )
                    self._disconnect_session()
                    self._connect_configured_session(resume_polling=False)
                    replacement = self._session
                    if replacement is None:
                        raise LCRMeterError("Measurement instrument is not connected.")
                    self._apply_route_meter_configuration_to_session(
                        replacement,
                        configuration,
                    )
                    return
                except Exception as retry_exc:
                    failure = retry_exc
            error = self._lcr_error(failure)
            self._disconnect_session()
            self.connection_changed.emit(False, "", str(error))
            self.status_message.emit(f"Instrument setup failed: {error}")
            raise error from failure

    def _apply_route_meter_configuration_to_session(
        self,
        session: object,
        configuration: RouteMeterConfiguration,
    ) -> None:
        try:
            _configure_route_session(
                session,
                configuration,
                gwinstek_session_type=_LCRSession,
                gwinstek_error="Connected instrument is not a GW Instek LCR.",
                keithley_error="Connected instrument is not a Keithley pair.",
            )
        except _RouteSessionError as exc:
            raise LCRMeterError(str(exc)) from exc

    def apply_route_meter_runtime_configuration(
        self,
        configuration: RouteMeterConfiguration,
    ) -> None:
        """Store route-meter connection settings for a future connection."""

        self._pending_route_meter_configuration = configuration
        if configuration.meter_type == ROUTE_METER_GWINSTEK:
            settings = configuration.gwinstek
            self._meter_type = ROUTE_METER_GWINSTEK
            self._resource_name = str(settings.resource_name).strip()
            self._keithley_source_resource = ""
            self._keithley_voltmeter_resource = ""
            self._measurement_function = (
                str(settings.measurement_function).strip() or "DCR"
            )
            self._range_mode = str(settings.range_mode).strip().upper() or "HOLD"
            self._auto_range_enabled = self._range_mode == "AUTO"
            self._impedance_range = max(0, min(8, int(settings.impedance_range)))
            self._dcr_range = max(0, min(8, int(settings.dcr_range)))
            self._frequency_hz = max(10.0, float(settings.frequency_hz))
            self._level_mode = str(settings.level_mode).strip().upper() or "VOLTAGE"
            self._voltage_level_v = max(0.0, float(settings.voltage_level_v))
            self._current_level_a = max(0.0, float(settings.current_level_a))
            self._source_resistance_ohm = int(settings.source_resistance_ohm)
            self._aperture_rate = (
                str(settings.aperture_rate).strip().upper() or "FAST"
            )
            self._aperture_averages = max(
                1, min(256, int(settings.aperture_averages))
            )
            self._trigger_source = "BUS"
            self._trigger_delay_s = max(0.0, float(settings.trigger_delay_s))
            self._bias_enabled = bool(settings.bias_enabled)
            self._bias_level_v = max(-2.5, min(2.5, float(settings.bias_level_v)))
            self._monitor1 = str(settings.monitor1).strip().upper() or "OFF"
            self._monitor2 = str(settings.monitor2).strip().upper() or "OFF"
            self._alc_enabled = bool(settings.alc_enabled)
            return
        if configuration.meter_type == ROUTE_METER_KEITHLEY:
            settings = configuration.keithley
            self._meter_type = ROUTE_METER_KEITHLEY
            self._resource_name = ""
            self._keithley_source_resource = str(settings.source_resource).strip()
            self._keithley_voltmeter_resource = str(
                settings.voltmeter_resource
            ).strip()
            self._measurement_function = "DCR"
            self._range_mode = str(settings.range_mode).strip().upper() or "AUTO"
            self._auto_range_enabled = self._range_mode == "AUTO"
            return

    def open(self) -> None:
        """Open the configured instrument for route-runner compatibility."""

        self.connect_now()
        configuration = self._pending_route_meter_configuration
        if configuration is not None:
            self.apply_route_meter_configuration(configuration)

    def is_short_reading(self, primary_value: float) -> bool:
        """Return True when a primary reading satisfies the configured short threshold."""

        return self._is_short_reading(float(primary_value))

    def short_threshold_ohm(self) -> float:
        """Return the configured short-circuit resistance threshold."""

        return self._short_threshold_ohm

    def read_primary_value_now(self, *, restart_polling: bool = False) -> float:
        """Synchronously run one BUS-triggered measurement and return its value."""

        measurement = self.read_route_measurement_now(restart_polling=restart_polling)
        return float(measurement["differential_resistance_ohm"])

    def read_route_measurement_now(
        self,
        *,
        restart_polling: bool = False,
    ) -> dict[str, object]:
        """Synchronously run one route measurement and return raw readings."""

        return dict(
            self._run_on_meter_worker(
                lambda: self._read_route_measurement_now_on_worker(restart_polling)
            )
        )

    def _read_route_measurement_now_on_worker(
        self,
        restart_polling: bool,
    ) -> dict[str, object]:
        session = self._connected_session_on_worker()

        def read_measurement() -> dict[str, object]:
            measurement = _read_route_measurement_from_session(
                session,
                cannot_read_message="Measurement instrument cannot read route values.",
            )
            return dict(measurement)

        measurement = self._run_connected_session_operation(
            session,
            read_measurement,
            failure_status="Instrument read failed",
            restart_polling=restart_polling,
            before_operation=lambda: self.reading_started.emit(1),
        )
        primary_value = float(measurement["differential_resistance_ohm"])
        self._emit_reading_summary(primary_value, 1)
        return measurement

    def read_voltage_sweep_now(
        self,
        voltages_v: list[float] | tuple[float, ...],
        *,
        restart_polling: bool = False,
    ) -> dict[str, object]:
        """Synchronously run a raw source-voltage sweep and return V/I points."""

        return dict(
            self._run_on_meter_worker(
                lambda: self._read_voltage_sweep_now_on_worker(
                    voltages_v,
                    restart_polling,
                )
            )
        )

    def _read_voltage_sweep_now_on_worker(
        self,
        voltages_v: list[float] | tuple[float, ...],
        restart_polling: bool,
    ) -> dict[str, object]:
        session = self._connected_session_on_worker()
        voltage_list_reader = getattr(session, "measure_voltage_list", None)
        if not callable(voltage_list_reader):
            raise LCRMeterError(
                "Raw voltage sweeps require a Keithley 2400 + 2182A instrument."
            )

        def read_sweep() -> dict[str, object]:
            points = [
                _voltage_sweep_point_to_dict(point)
                for point in voltage_list_reader(voltages_v)
            ]
            return {
                "source_voltages_v": [float(value) for value in voltages_v],
                "points": points,
            }

        measurement = self._run_connected_session_operation(
            session,
            read_sweep,
            failure_status="Instrument read failed",
            restart_polling=restart_polling,
            before_operation=lambda: self.reading_started.emit(max(1, len(voltages_v))),
        )
        points = list(measurement["points"])
        primary_value = self._mean_resistance_from_measurements(points)
        self._emit_reading_summary(primary_value, len(points))
        return measurement

    def read_route_measurement_batch_now(
        self,
        count: int,
        *,
        restart_polling: bool = False,
        after_measurement: object | None = None,
    ) -> list[dict[str, object]]:
        """Synchronously run a batch of route measurements when supported."""

        count = max(1, int(count))
        return list(
            self._run_on_meter_worker(
                lambda: self._read_route_measurement_batch_now_on_worker(
                    count,
                    restart_polling,
                    after_measurement,
                )
            )
        )

    def _read_route_measurement_batch_now_on_worker(
        self,
        count: int,
        restart_polling: bool,
        after_measurement: object | None,
    ) -> list[dict[str, object]]:
        session = self._connected_session_on_worker()

        def read_batch() -> list[dict[str, object]]:
            return list(
                _read_route_measurement_batch_from_session(
                    session,
                    count,
                    after_measurement=after_measurement,
                    cannot_read_message="Measurement instrument cannot read route values.",
                )
            )

        measurements = self._run_connected_session_operation(
            session,
            read_batch,
            failure_status="Instrument read failed",
            restart_polling=restart_polling,
            before_operation=lambda: self.reading_started.emit(count),
        )
        primary_value = self._mean_resistance_from_measurements(measurements)
        self._emit_reading_summary(primary_value, len(measurements))
        return measurements

    def _connected_session_on_worker(self) -> object:
        session = self._session
        if session is None:
            raise LCRMeterError("Measurement instrument is not connected.")
        return session

    def _run_connected_session_operation(
        self,
        session: object,
        operation: Callable[[], _SessionOperationResult],
        *,
        failure_status: str,
        restart_polling: bool = False,
        before_operation: Callable[[], None] | None = None,
    ) -> _SessionOperationResult:
        self._stop_polling_session()
        if before_operation is not None:
            before_operation()
        try:
            return operation()
        except Exception as exc:
            error = self._lcr_error(exc)
            self._disconnect_session()
            self.connection_changed.emit(False, "", str(error))
            self.status_message.emit(f"{failure_status}: {error}")
            raise error from exc
        finally:
            if restart_polling and self._session is session:
                self._stop_polling.clear()
                self._start_polling_thread()

    def prepare_route_measurement_batch_now(
        self,
        count: int,
        *,
        source_list_count: int | None = None,
    ) -> None:
        """Prepare a route measurement batch when the backend supports it."""

        count = max(1, int(count))
        self._run_on_meter_worker(
            lambda: self._prepare_route_measurement_batch_now_on_worker(
                count,
                source_list_count,
            )
        )

    def _prepare_route_measurement_batch_now_on_worker(
        self,
        count: int,
        source_list_count: int | None,
    ) -> None:
        session = self._connected_session_on_worker()

        def prepare_batch() -> None:
            _prepare_route_measurement_batch_on_session(
                session,
                count,
                source_list_count=source_list_count,
            )

        self._run_connected_session_operation(
            session,
            prepare_batch,
            failure_status="Instrument preparation failed",
        )

    def _emit_reading_summary(self, primary_value: float, sample_count: int) -> None:
        count = max(1, int(sample_count))
        is_short = self._is_short_reading(primary_value)
        try:
            self.reading_updated.emit(primary_value, is_short)
            self.reading_summary_updated.emit(primary_value, is_short, count)
        except RuntimeError as exc:
            if "Signal source has been deleted" not in str(exc):
                raise
            self._stop_worker_after_deleted_signal_source()

    def _stop_worker_after_deleted_signal_source(self) -> None:
        _LCR_METER_CONTROLLERS.discard(self)
        self._shutdown_started = True
        self._stop_polling.set()
        self._worker_runtime.request_shutdown()

    @staticmethod
    def _mean_resistance_from_measurements(measurements: object) -> float:
        finite_values: list[float] = []
        for item in list(measurements) if measurements is not None else []:
            value = LCRMeterController._resistance_value_from_measurement(item)
            if math.isfinite(value):
                finite_values.append(value)
        if not finite_values:
            return math.nan
        return sum(finite_values) / len(finite_values)

    @staticmethod
    def _resistance_value_from_measurement(item: object) -> float:
        if isinstance(item, dict):
            for key in (
                "differential_resistance_ohm",
                "resistance_ohm",
                "v_over_i_ohm",
            ):
                if key not in item:
                    continue
                try:
                    return float(item[key])
                except (TypeError, ValueError):
                    continue
            return math.nan
        try:
            return float(item)
        except (TypeError, ValueError):
            return math.nan

    def abort_current_measurement(self) -> None:
        """Best-effort cancellation for a blocking route measurement read."""

        session = self._session
        abort = getattr(session, "abort_measurement", None)
        if callable(abort):
            abort()

    def visa_resource_roles(self) -> dict[str, dict[str, object]]:
        """Return station-owned VISA roles for the connected measurement backend."""

        session = self._session
        if session is None:
            return {}
        return _session_visa_resource_roles(session, meter_type=self._meter_type)

    def visa_operation(
        self,
        role: str,
        operation: str,
        *,
        command: str | None = None,
        timeout_ms: int | None = None,
        read_termination: str | None = None,
        write_termination: str | None = None,
    ) -> object:
        """Run one serialized VISA-like operation on the connected backend."""

        return self._run_on_meter_worker(
            lambda: self._visa_operation_on_worker(
                role,
                operation,
                command=command,
                timeout_ms=timeout_ms,
                read_termination=read_termination,
                write_termination=write_termination,
            )
        )

    def _visa_operation_on_worker(
        self,
        role: str,
        operation: str,
        *,
        command: str | None = None,
        timeout_ms: int | None = None,
        read_termination: str | None = None,
        write_termination: str | None = None,
    ) -> object:
        session = self._connected_session_on_worker()

        def run_visa_operation() -> object:
            return _session_visa_operation(
                session,
                role,
                operation,
                command=command,
                timeout_ms=timeout_ms,
                read_termination=read_termination,
                write_termination=write_termination,
            )

        return self._run_connected_session_operation(
            session,
            run_visa_operation,
            failure_status="Instrument VISA operation failed",
        )

    def request_connect(self) -> None:
        """Open the configured measurement instrument in a background thread."""

        if not self._submit_meter_worker_call(self._run_connect):
            self.status_message.emit("Measurement instrument task already running.")

    def connect_now(self) -> None:
        """Open the configured measurement instrument in the current thread."""

        self._run_on_meter_worker(self._connect_now_on_worker)

    def _connect_now_on_worker(self) -> None:
        if self._session is not None:
            return
        try:
            self._connect_configured_session()
        except Exception as exc:
            error = self._lcr_error(exc)
            self._disconnect_session()
            self.connection_changed.emit(False, "", str(error))
            self.status_message.emit(f"Instrument connection failed: {error}")
            raise error from exc

    def request_disconnect(self) -> None:
        """Close the current measurement instrument and stop polling."""

        if not self._submit_meter_worker_call(self._run_disconnect):
            self.status_message.emit("Measurement instrument task already running.")

    def request_reconfigure(self) -> None:
        """Apply the current configuration to an already connected meter."""

        if self._session is None:
            return
        if not self._submit_meter_worker_call(self._run_reconfigure):
            self.status_message.emit("Measurement instrument task already running.")

    def shutdown(self) -> None:
        """Stop background work before application exit."""

        if self._shutdown_started:
            return
        self._shutdown_started = True
        _LCR_METER_CONTROLLERS.discard(self)
        self._stop_polling.set()
        try:
            self._run_on_meter_worker(self._disconnect_session)
        except Exception:  # pragma: no cover - best effort shutdown
            logger.exception("Failed to close measurement instrument session during shutdown")
        self._worker_runtime.shutdown(join_timeout_s=2.0)

    def _run_connect(self) -> None:
        try:
            self._connect_configured_session()
        except Exception as exc:
            error = self._lcr_error(exc)
            self._disconnect_session()
            self.connection_changed.emit(False, "", str(error))
            self.status_message.emit(f"Instrument connection failed: {error}")

    def _run_reconfigure(self) -> None:
        try:
            session = self._session
            if session is None:
                return
            desired_resource = self._connection_key()
            connected_resource = self._connected_resource_name
            if not desired_resource:
                raise LCRMeterError(
                    "Measurement instrument resource is empty. Set it in Settings."
                )
            if desired_resource != connected_resource:
                self.status_message.emit(
                    "Instrument connection settings changed. Reconnecting."
                )
                self._disconnect_session()
                replacement = self._open_configured_session()
                identify = getattr(replacement, "identify", None)
                instrument_id = str(identify()) if callable(identify) else ""
                logger.info(
                    "Reconnected to measurement instrument %s (%s)",
                    self.connection_label(),
                    instrument_id or "IDN unavailable",
                )
                session = replacement
                self._session = session
                self._connected_resource_name = desired_resource
                backend_name = str(
                    getattr(session, "backend_name", session.__class__.__name__)
                )
                self.connection_changed.emit(
                    True, backend_name, self.connection_label()
                )
            else:
                self._stop_polling_session()
            if self._meter_type == ROUTE_METER_GWINSTEK:
                self._configure_active_session(session)
            self._stop_polling.clear()
            self.status_message.emit("Instrument settings applied.")
            self._resume_live_polling()
        except LCRMeterError as exc:
            self._disconnect_session()
            self.connection_changed.emit(False, "", str(exc))
            self.status_message.emit(f"Instrument reconfiguration failed: {exc}")

    def _run_disconnect(self) -> None:
        self._disconnect_session()
        self.connection_changed.emit(False, "", "Disconnected")
        self.status_message.emit("Measurement instrument disconnected.")

    def _connect_configured_session(self, *, resume_polling: bool = True) -> None:
        if self._meter_type == ROUTE_METER_GWINSTEK and not self._resource_name:
            raise LCRMeterError("GW Instek resource is empty. Set it in Settings.")
        if self._meter_type == ROUTE_METER_KEITHLEY:
            if not self._keithley_source_resource:
                raise LCRMeterError(
                    "Keithley 2400 resource is empty. Set it in Settings."
                )
        self._disconnect_session()
        session = self._open_configured_session()
        try:
            identify = getattr(session, "identify", None)
            instrument_id = str(identify()) if callable(identify) else ""
            self._validate_connected_session_identity(instrument_id)
            backend_name = str(
                getattr(session, "backend_name", session.__class__.__name__)
            )
            logger.info(
                "Connected to measurement instrument %s (%s)",
                self.connection_label(),
                instrument_id or "IDN unavailable",
            )
            if self._meter_type == ROUTE_METER_GWINSTEK:
                self._configure_session(session)
            elif self._meter_type == ROUTE_METER_KEITHLEY:
                self._configure_keithley_live_session(session)
        except Exception:
            closer = getattr(session, "close", None)
            if callable(closer):
                try:
                    closer()
                except Exception:  # pragma: no cover - best effort cleanup
                    logger.exception(
                        "Failed to close rejected measurement instrument session"
                    )
            raise
        self._session = session
        self._connected_resource_name = self._connection_key()
        if resume_polling:
            self._stop_polling.clear()
        self.connection_changed.emit(True, backend_name, self.connection_label())
        self.status_message.emit(
            f"Measurement instrument connected via {backend_name}."
        )
        if resume_polling:
            self._resume_live_polling()

    def _validate_connected_session_identity(self, instrument_id: str) -> None:
        if self._meter_type != ROUTE_METER_KEITHLEY:
            return
        normalized = str(instrument_id or "")
        if normalized.startswith("2400 ") or "; 2400 " in normalized:
            return
        raise LCRMeterError(
            "Keithley 2400 did not respond to *IDN?. "
            f"Check {self._keithley_source_resource or 'the source resource'} "
            "or power-cycle the source meter."
        )

    def _configure_active_session(self, session: _LCRSession) -> None:
        self._configure_session(session)

    def _configure_session(self, session: _LCRSession) -> None:
        if not isinstance(session, _LCRSession):
            return
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
            trigger_source="BUS",
            trigger_delay_s=self._trigger_delay_s,
            bias_enabled=self._bias_enabled,
            bias_level_v=self._bias_level_v,
            monitor1=self._monitor1,
            monitor2=self._monitor2,
            alc_enabled=self._alc_enabled,
        )

    def _configure_keithley_live_session(self, session: object) -> None:
        configure = getattr(session, "configure_measurement", None)
        if not callable(configure):
            raise LCRMeterError("Connected instrument is not a Keithley pair.")
        configure(
            keithley_measurement_voltage_v=KEITHLEY_LIVE_MEASUREMENT_VOLTAGE_V,
            keithley_range_mode=OHMMETER_RANGE_MANUAL,
            keithley_source_voltage_range_v=KEITHLEY_LIVE_SOURCE_VOLTAGE_RANGE_V,
            keithley_voltmeter_range_v=KEITHLEY_LIVE_VOLTMETER_RANGE_V,
            keithley_current_range_a=KEITHLEY_LIVE_CURRENT_RANGE_A,
            keithley_compliance_current_a=KEITHLEY_LIVE_COMPLIANCE_CURRENT_A,
            keithley_nplc=KEITHLEY_LIVE_NPLC,
            keithley_terminals="rear",
            keithley_use_buffer=False,
            keithley_use_trigger_link=False,
        )

    def _open_configured_session(self) -> object:
        if self._meter_type == ROUTE_METER_KEITHLEY:
            return _open_keithley_session(
                self._keithley_source_resource,
                self._keithley_voltmeter_resource,
                self.DEFAULT_TIMEOUT_MS,
            )
        return _LCRSession(self._resource_name, self.DEFAULT_TIMEOUT_MS)

    def _connection_key(self) -> str:
        if self._meter_type == ROUTE_METER_KEITHLEY:
            source = normalize_resource_name(self._keithley_source_resource)
            voltmeter = normalize_resource_name(self._keithley_voltmeter_resource)
            if not source:
                return ""
            return f"{ROUTE_METER_KEITHLEY}|{source}|{voltmeter}"
        resource = normalize_resource_name(self._resource_name)
        if not resource:
            return ""
        return f"{ROUTE_METER_GWINSTEK}|{resource}"

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
        if self._live_output_context is None:
            return
        if self._worker_runtime.has_live_worker_other_than_current():
            self._run_on_meter_worker(self._close_live_output_context_on_worker)
        else:
            self._close_live_output_context_on_worker()

    def _pause_live_polling(self) -> None:
        self._stop_polling_session()

    def _disconnect_session(self) -> None:
        self._stop_polling_session()
        session = self._session
        self._session = None
        self._connected_resource_name = ""
        if session is not None:
            try:
                closer = getattr(session, "close", None)
                if callable(closer):
                    closer()
            except Exception:  # pragma: no cover - best effort cleanup
                logger.exception(
                    "Failed to close measurement instrument session cleanly"
                )

    def _start_polling_thread(self) -> None:
        self._wake_meter_worker()

    def _resume_live_polling(self) -> None:
        if not self._live_polling_enabled or self._session is None:
            return
        self._stop_polling.clear()
        self._start_polling_thread()

    def _poll_readings(self) -> None:
        while not self._stop_polling.is_set():
            if not self._run_meter_poll_once():
                return
            time.sleep(self._poll_interval_ms / 1000.0)

    def _run_meter_poll_once(self) -> bool:
        session = self._session
        if self._stop_polling.is_set() or session is None:
            return False
        try:
            if self._stop_polling.is_set() or session is not self._session:
                return False
            self._ensure_live_output_context_on_worker(session)
            reader = getattr(session, "read_primary_value", None)
            if not callable(reader):
                raise LCRMeterError("Measurement instrument cannot read values.")
            primary_value = float(reader(trigger=True))
        except Exception as exc:
            error = self._lcr_error(exc)
            self.status_message.emit(f"Instrument read failed: {error}")
            self.connection_changed.emit(False, "", str(error))
            self._disconnect_session()
            return False
        if self._stop_polling.is_set() or session is not self._session:
            return False
        self._emit_reading_summary(primary_value, 1)
        return True

    @contextmanager
    def output(self, enabled: bool = True) -> Iterator["LCRMeterController"]:
        context = self._run_on_meter_worker(
            lambda: self._enter_output_context_on_worker(bool(enabled))
        )
        try:
            yield self
        finally:
            if context is not None:
                self._run_on_meter_worker(
                    lambda: self._exit_output_context_on_worker(context)
                )

    def _enter_output_context_on_worker(self, enabled: bool) -> object | None:
        session = self._session
        if session is None:
            raise LCRMeterError("Measurement instrument is not connected.")
        self._stop_polling.set()
        self._close_live_output_context_on_worker()
        output = getattr(session, "output", None)
        if not callable(output):
            return None
        context = output(bool(enabled))
        enter = getattr(context, "__enter__", None)
        if not callable(enter):
            return None
        enter()
        return context

    @staticmethod
    def _exit_output_context_on_worker(context: object) -> None:
        exit_method = getattr(context, "__exit__", None)
        if callable(exit_method):
            exit_method(None, None, None)

    def _ensure_live_output_context_on_worker(self, session: object) -> None:
        if self._live_output_context is not None:
            return
        output = getattr(session, "output", None)
        if not callable(output):
            return
        context = output(True)
        enter = getattr(context, "__enter__", None)
        if not callable(enter):
            return
        enter()
        self._live_output_context = context

    def _close_live_output_context_on_worker(self) -> None:
        context = self._live_output_context
        self._live_output_context = None
        if context is not None:
            self._exit_output_context_on_worker(context)

    @staticmethod
    def _lcr_error(exc: BaseException) -> LCRMeterError:
        if isinstance(exc, LCRMeterError):
            return exc
        message = str(exc).strip() or exc.__class__.__name__
        return LCRMeterError(message)
