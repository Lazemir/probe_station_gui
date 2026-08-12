"""Qt adapter for live resistance-meter operation and delivery."""

from __future__ import annotations

import atexit
import logging
import math
import threading
import weakref
from contextlib import contextmanager
from typing import Callable, Iterator, TypeVar

from PySide6.QtCore import QObject, Signal

from probe_station_gui.instruments.meters.gwinstek_session import (
    GWInstekLCRSession as _GWInstekLCRSession,
)
from probe_station_gui.instruments.meters.lcr_helpers import (
    voltage_sweep_point_to_dict as _voltage_sweep_point_to_dict,
)
from probe_station_gui.instruments.meters.lcr_live_session import (
    LiveLCRSession as _LiveLCRSession,
)
from probe_station_gui.instruments.meters.lcr_route_session import (
    configure_route_session as _configure_route_session,
    prepare_route_measurement_batch_on_session as _prepare_route_measurement_batch_on_session,
    read_route_measurement_batch_from_session as _read_route_measurement_batch_from_session,
    read_route_measurement_from_session as _read_route_measurement_from_session,
)
from probe_station_gui.instruments.meters.lcr_session_backend import (
    DEFAULT_METER_TIMEOUT_MS as _DEFAULT_METER_TIMEOUT_MS,
    LCRMeterError as _LCRMeterError,
    LCRSessionConfiguration as _LCRSessionConfiguration,
    coerce_lcr_error as _coerce_lcr_error,
    route_meter_label as _route_meter_label,
    session_roles as _session_roles,
    session_visa_operation as _session_visa_operation,
)
from probe_station_gui.instruments.meters.worker import (
    MeterWorkerRuntime as _MeterWorkerRuntime,
    meter_worker_poll_timeout,
)
from probe_station_gui.route.meter_config import (
    ROUTE_METER_GWINSTEK,
    ROUTE_METER_KEITHLEY_TYPES,
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


class LCRMeterController(QObject):
    """Serialize live meter work and publish Qt-facing lifecycle events."""

    connection_changed: Signal = Signal(bool, str, str)
    reading_started: Signal = Signal(int)
    reading_summary_updated: Signal = Signal(float, bool, int)
    reading_updated: Signal = Signal(float, bool)
    status_message: Signal = Signal(str)

    DEFAULT_TIMEOUT_MS = _DEFAULT_METER_TIMEOUT_MS
    TASK_WAIT_TIMEOUT_S = 45.0

    def __init__(self) -> None:
        super().__init__()
        configuration = _LCRSessionConfiguration.normalized(
            meter_type=ROUTE_METER_GWINSTEK,
            resource_name="",
            keithley_source_resource="",
            keithley_voltmeter_resource="",
            measurement_function="DCR",
            range_mode="HOLD",
            auto_range_enabled=False,
            impedance_range=3,
            dcr_range=3,
            frequency_hz=1000.0,
            level_mode="VOLTAGE",
            voltage_level_v=0.01,
            current_level_a=0.0001,
            source_resistance_ohm=30,
            aperture_rate="FAST",
            aperture_averages=1,
            trigger_source="INT",
            trigger_delay_s=0.0,
            bias_enabled=False,
            bias_level_v=0.0,
            monitor1="OFF",
            monitor2="OFF",
            alc_enabled=False,
            short_threshold_ohm=10.0,
            poll_interval_ms=250,
        )
        self._live_session = _LiveLCRSession(
            configuration,
            timeout_ms=self.DEFAULT_TIMEOUT_MS,
        )
        self._worker_runtime = _MeterWorkerRuntime(
            name="LCRMeterWorker",
            poll_timeout=self._meter_worker_poll_timeout,
            poll_once=self._run_meter_poll_once,
            logger=logger,
        )
        self._pending_route_meter_configuration: RouteMeterConfiguration | None = None
        self._shutdown_lock = threading.Lock()
        self._shutdown_started = False
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

        self._live_session.apply_configuration(
            _LCRSessionConfiguration.normalized(
                meter_type=meter_type,
                resource_name=resource_name,
                keithley_source_resource=keithley_source_resource,
                keithley_voltmeter_resource=keithley_voltmeter_resource,
                measurement_function=measurement_function,
                range_mode=range_mode,
                auto_range_enabled=auto_range_enabled,
                impedance_range=impedance_range,
                dcr_range=dcr_range,
                frequency_hz=frequency_hz,
                level_mode=level_mode,
                voltage_level_v=voltage_level_v,
                current_level_a=current_level_a,
                source_resistance_ohm=source_resistance_ohm,
                aperture_rate=aperture_rate,
                aperture_averages=aperture_averages,
                trigger_source=trigger_source,
                trigger_delay_s=trigger_delay_s,
                bias_enabled=bias_enabled,
                bias_level_v=bias_level_v,
                monitor1=monitor1,
                monitor2=monitor2,
                alc_enabled=alc_enabled,
                short_threshold_ohm=short_threshold_ohm,
                poll_interval_ms=poll_interval_ms,
            )
        )

    def is_connected(self) -> bool:
        """Return True when the measurement backend is connected."""

        return self._live_session.snapshot().session is not None

    def live_polling_enabled(self) -> bool:
        """Return True when standby resistance polling is enabled."""

        return self._live_session.snapshot().live_polling_enabled

    def set_live_polling_enabled(self, enabled: bool) -> None:
        """Enable or disable standby resistance polling."""

        self._live_session.set_live_polling_enabled(enabled)
        if enabled:
            self._resume_live_polling()
        elif self._worker_runtime.is_current_worker_thread():
            self._live_session.pause_polling()
        else:
            self._worker_runtime.submit(
                lambda: (
                    self._live_session.pause_polling()
                    if not self._live_session.snapshot().live_polling_enabled
                    else None
                ),
                queue_if_busy=True,
            )

    def wait_until_idle(self, timeout_s: float | None = None) -> bool:
        """Wait until the current background meter task finishes."""

        return self._worker_runtime.wait_until_idle(
            timeout_s=timeout_s,
            default_timeout_s=self.TASK_WAIT_TIMEOUT_S,
        )

    def _run_on_meter_worker(self, func: Callable[[], object]) -> object:
        return self._worker_runtime.run(func)

    def _meter_worker_poll_timeout(self) -> float | None:
        state = self._live_session.snapshot()
        return meter_worker_poll_timeout(
            live_polling_enabled=state.live_polling_enabled,
            stop_polling=state.stop_requested,
            has_session=state.session is not None,
            poll_interval_ms=state.configuration.poll_interval_ms,
        )

    def meter_type(self) -> str:
        """Return the currently configured meter type."""

        return self._live_session.snapshot().configuration.meter_type

    def connection_label(self) -> str:
        """Return a human readable connection target for the configured meter."""

        return self._live_session.snapshot().configuration.connection_label

    def apply_route_meter_configuration(
        self,
        configuration: RouteMeterConfiguration,
    ) -> None:
        """Apply per-run measurement settings to the connected meter."""

        live_configuration = self._live_session.snapshot().configuration
        if configuration.meter_type != live_configuration.meter_type:
            configured_label = _route_meter_label(live_configuration.meter_type)
            requested_label = _route_meter_label(configuration.meter_type)
            raise _LCRMeterError(
                f"Connected instrument is {configured_label}; "
                f"route requested {requested_label}."
            )
        self._pending_route_meter_configuration = configuration
        self._run_on_meter_worker(
            lambda: self._apply_route_meter_configuration_on_worker(configuration)
        )

    def _apply_route_meter_configuration_on_worker(
        self,
        configuration: RouteMeterConfiguration,
    ) -> None:
        session = self._live_session.connected_session()
        self._live_session.pause_polling()
        try:
            state = self._live_session.snapshot()
            if session is not state.session:
                raise _LCRMeterError("Measurement instrument is not connected.")
            _configure_route_session(
                session,
                configuration,
                gwinstek_session_type=_GWInstekLCRSession,
                gwinstek_error="Connected instrument is not a GW Instek LCR.",
                keithley_error="Connected instrument is not a Keithley 2400.",
            )
        except Exception as exc:
            failure: BaseException = exc
            if configuration.meter_type in ROUTE_METER_KEITHLEY_TYPES and bool(
                state.configuration.connection_key
            ):
                try:
                    self.status_message.emit(
                        "Instrument setup failed; reconnecting Keithley."
                    )
                    self._live_session.disconnect()
                    self._connect_configured_session(resume_polling=False)
                    replacement = self._live_session.connected_session()
                    _configure_route_session(
                        replacement,
                        configuration,
                        gwinstek_session_type=_GWInstekLCRSession,
                        gwinstek_error=("Connected instrument is not a GW Instek LCR."),
                        keithley_error=("Connected instrument is not a Keithley 2400."),
                    )
                    return
                except Exception as retry_exc:
                    failure = retry_exc
            error = _coerce_lcr_error(failure)
            self._live_session.disconnect()
            self.connection_changed.emit(False, "", str(error))
            self.status_message.emit(f"Instrument setup failed: {error}")
            raise error from failure

    def apply_route_meter_runtime_configuration(
        self,
        configuration: RouteMeterConfiguration,
    ) -> None:
        """Store route-meter connection settings for a future connection."""

        self._pending_route_meter_configuration = configuration
        live_configuration = self._live_session.snapshot().configuration
        self._live_session.apply_configuration(
            live_configuration.with_route_configuration(configuration)
        )

    def open(self) -> None:
        """Open the configured instrument for route-runner compatibility."""

        self.connect_now()
        configuration = self._pending_route_meter_configuration
        if configuration is not None:
            self.apply_route_meter_configuration(configuration)

    def is_short_reading(self, primary_value: float) -> bool:
        """Return whether a reading satisfies the configured short threshold."""

        return self._live_session.snapshot().configuration.is_short_reading(
            float(primary_value)
        )

    def short_threshold_ohm(self) -> float:
        """Return the configured short-circuit resistance threshold."""

        return self._live_session.snapshot().configuration.short_threshold_ohm

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
        session = self._live_session.connected_session()

        def read_measurement() -> dict[str, object]:
            measurement = _read_route_measurement_from_session(
                session,
                cannot_read_message=(
                    "Measurement instrument cannot read route values."
                ),
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
        session = self._live_session.connected_session()
        voltage_list_reader = getattr(session, "measure_voltage_list", None)
        if not callable(voltage_list_reader):
            raise _LCRMeterError(
                "Raw voltage sweeps require a Keithley 2400 instrument."
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
        session = self._live_session.connected_session()

        def read_batch() -> list[dict[str, object]]:
            return list(
                _read_route_measurement_batch_from_session(
                    session,
                    count,
                    after_measurement=after_measurement,
                    cannot_read_message=(
                        "Measurement instrument cannot read route values."
                    ),
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

    def _run_connected_session_operation(
        self,
        session: object,
        operation: Callable[[], _SessionOperationResult],
        *,
        failure_status: str,
        restart_polling: bool = False,
        before_operation: Callable[[], None] | None = None,
    ) -> _SessionOperationResult:
        self._live_session.pause_polling()
        if before_operation is not None:
            before_operation()
        try:
            return operation()
        except Exception as exc:
            error = _coerce_lcr_error(exc)
            self._live_session.disconnect()
            if not self._shutdown_started:
                self.connection_changed.emit(False, "", str(error))
                self.status_message.emit(f"{failure_status}: {error}")
            raise error from exc
        finally:
            if (
                restart_polling
                and not self._shutdown_started
                and self._live_session.snapshot().session is session
            ):
                self._live_session.resume_polling()
                self._worker_runtime.wake()

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
        session = self._live_session.connected_session()

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
        if self._shutdown_started:
            return
        count = max(1, int(sample_count))
        is_short = self._live_session.snapshot().configuration.is_short_reading(
            primary_value
        )
        try:
            self.reading_updated.emit(primary_value, is_short)
            self.reading_summary_updated.emit(primary_value, is_short, count)
        except RuntimeError as exc:
            if "Signal source has been deleted" not in str(exc):
                raise
            self._stop_worker_after_deleted_signal_source()

    def _stop_worker_after_deleted_signal_source(self) -> None:
        _LCR_METER_CONTROLLERS.discard(self)
        with self._shutdown_lock:
            self._shutdown_started = True
        self._live_session.request_stop()
        self._worker_runtime.request_shutdown(retire=self._live_session.disconnect)

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

        self._live_session.abort_current_measurement()

    def visa_resource_roles(self) -> dict[str, dict[str, object]]:
        """Return station-owned VISA roles for the connected backend."""

        state = self._live_session.snapshot()
        if state.session is None:
            return {}
        return _session_roles(state.session, state.configuration)

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
        session = self._live_session.connected_session()

        def run_visa_operation() -> object:
            state = self._live_session.snapshot()
            return _session_visa_operation(
                state.session,
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

        if not self._worker_runtime.submit(self._run_connect):
            self.status_message.emit("Measurement instrument task already running.")

    def connect_now(self) -> None:
        """Open the configured measurement instrument in the current thread."""

        self._run_on_meter_worker(self._connect_now_on_worker)

    def _connect_now_on_worker(self) -> None:
        if self._live_session.snapshot().session is not None:
            return
        try:
            self._connect_configured_session()
        except Exception as exc:
            error = _coerce_lcr_error(exc)
            self._live_session.disconnect()
            self.connection_changed.emit(False, "", str(error))
            self.status_message.emit(f"Instrument connection failed: {error}")
            raise error from exc

    def request_disconnect(self) -> None:
        """Close the current measurement instrument and stop polling."""

        if not self._worker_runtime.submit(self._run_disconnect):
            self.status_message.emit("Measurement instrument task already running.")

    def request_reconfigure(self) -> None:
        """Apply the current configuration to an already connected meter."""

        if self._live_session.snapshot().session is None:
            return
        if not self._worker_runtime.submit(self._run_reconfigure):
            self.status_message.emit("Measurement instrument task already running.")

    def shutdown(self) -> None:
        """Stop background work before application exit."""

        with self._shutdown_lock:
            if self._shutdown_started:
                return
            self._shutdown_started = True
        _LCR_METER_CONTROLLERS.discard(self)
        self._live_session.request_stop()
        if not self._worker_runtime.wait_until_idle(
            timeout_s=0.0,
            default_timeout_s=0.0,
        ):
            try:
                self.abort_current_measurement()
            except Exception:  # pragma: no cover - best effort cancellation
                logger.exception(
                    "Failed to abort measurement instrument during shutdown"
                )
        self._worker_runtime.shutdown(
            join_timeout_s=2.0,
            retire=self._live_session.disconnect,
        )

    def _run_connect(self) -> None:
        try:
            self._connect_configured_session()
        except Exception as exc:
            error = _coerce_lcr_error(exc)
            self._live_session.disconnect()
            self.connection_changed.emit(False, "", str(error))
            self.status_message.emit(f"Instrument connection failed: {error}")

    def _run_reconfigure(self) -> None:
        try:
            state = self._live_session.snapshot()
            session = state.session
            if session is None:
                return
            desired_resource = state.configuration.connection_key
            connected_resource = state.connected_resource_name
            if not desired_resource:
                raise _LCRMeterError(
                    "Measurement instrument resource is empty. Set it in Settings."
                )
            if desired_resource != connected_resource:
                self.status_message.emit(
                    "Instrument connection settings changed. Reconnecting."
                )
                connection = self._live_session.replace_for_reconfigure()
                logger.info(
                    "Reconnected to measurement instrument %s (%s)",
                    self.connection_label(),
                    connection.instrument_id or "IDN unavailable",
                )
                session = self._live_session.connected_session()
                self.connection_changed.emit(
                    True,
                    connection.backend_name,
                    self.connection_label(),
                )
            self._live_session.finish_reconfigure()
            self.status_message.emit("Instrument settings applied.")
            self._resume_live_polling()
        except Exception as exc:
            error = _coerce_lcr_error(exc)
            self._live_session.disconnect()
            self.connection_changed.emit(False, "", str(error))
            self.status_message.emit(f"Instrument reconfiguration failed: {error}")

    def _run_disconnect(self) -> None:
        self._live_session.disconnect()
        self.connection_changed.emit(False, "", "Disconnected")
        self.status_message.emit("Measurement instrument disconnected.")

    def _connect_configured_session(self, *, resume_polling: bool = True) -> None:
        connection = self._live_session.connect(resume_polling=resume_polling)
        logger.info(
            "Connected to measurement instrument %s (%s)",
            self.connection_label(),
            connection.instrument_id or "IDN unavailable",
        )
        self.connection_changed.emit(
            True,
            connection.backend_name,
            self.connection_label(),
        )
        self.status_message.emit(
            f"Measurement instrument connected via {connection.backend_name}."
        )
        if resume_polling:
            self._resume_live_polling()

    def _resume_live_polling(self) -> None:
        if self._live_session.resume_polling():
            self._worker_runtime.wake()

    def _run_meter_poll_once(self) -> bool:
        try:
            primary_value = self._live_session.poll_once()
        except Exception as exc:
            error = _coerce_lcr_error(exc)
            if self._shutdown_started or self._live_session.snapshot().stop_requested:
                return False
            self.status_message.emit(f"Instrument read failed: {error}")
            self.connection_changed.emit(False, "", str(error))
            self._live_session.disconnect()
            return False
        if primary_value is None:
            return False
        self._emit_reading_summary(primary_value, 1)
        return True

    @contextmanager
    def output(self, enabled: bool = True) -> Iterator[LCRMeterController]:
        context = self._run_on_meter_worker(
            lambda: self._live_session.begin_output(bool(enabled))
        )
        try:
            yield self
        finally:
            if context is not None:
                try:
                    self._run_on_meter_worker(
                        lambda: self._live_session.finish_output(context)
                    )
                except RuntimeError as exc:
                    if not (
                        self._worker_runtime.shutdown_requested
                        and exc.args == ("Measurement instrument worker is stopping.",)
                    ):
                        raise


__all__ = ["LCRMeterController"]
