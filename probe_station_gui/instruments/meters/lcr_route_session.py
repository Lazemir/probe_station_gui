"""Session-level route measurement helpers for LCR meter backends."""

from __future__ import annotations

from contextlib import contextmanager
from typing import Iterator

from probe_station_gui.instruments.meters.gwinstek_session import GWInstekLCRSession
from probe_station_gui.instruments.meters.lcr_helpers import (
    prepare_route_measurement_batch,
    read_route_measurement_batch,
    session_visa_resource_roles,
)
from probe_station_gui.instruments.meters.lcr_session_backend import (
    DEFAULT_METER_TIMEOUT_MS,
    LCRMeterError,
    open_keithley_session,
    session_visa_operation,
)
from probe_station_gui.route.meter_config import (
    ROUTE_METER_GWINSTEK,
    ROUTE_METER_KEITHLEY,
    ROUTE_METER_KEITHLEY_TYPES,
    ROUTE_METER_LABELS,
    RouteMeterConfiguration,
)


def configure_route_session(
    session: object,
    configuration: RouteMeterConfiguration,
    *,
    gwinstek_session_type: type,
    gwinstek_error: str,
    keithley_error: str,
) -> None:
    if configuration.meter_type == ROUTE_METER_GWINSTEK:
        if not isinstance(session, gwinstek_session_type):
            raise LCRMeterError(gwinstek_error)
        _configure_gwinstek_session(session, configuration)
        return
    if configuration.meter_type in ROUTE_METER_KEITHLEY_TYPES:
        if isinstance(session, gwinstek_session_type):
            raise LCRMeterError(keithley_error)
        _configure_keithley_session(session, configuration, keithley_error)
        return
    raise LCRMeterError(
        f"Unsupported route measurement instrument: {configuration.meter_type}"
    )


def _configure_gwinstek_session(
    session: object,
    configuration: RouteMeterConfiguration,
) -> None:
    settings = configuration.gwinstek
    session.configure_measurement(
        measurement_function=settings.measurement_function,
        range_mode=settings.range_mode,
        impedance_range=settings.impedance_range,
        dcr_range=settings.dcr_range,
        frequency_hz=settings.frequency_hz,
        level_mode=settings.level_mode,
        voltage_level_v=settings.voltage_level_v,
        current_level_a=settings.current_level_a,
        source_resistance_ohm=settings.source_resistance_ohm,
        aperture_rate=settings.aperture_rate,
        aperture_averages=settings.aperture_averages,
        trigger_source="BUS",
        trigger_delay_s=settings.trigger_delay_s,
        bias_enabled=settings.bias_enabled,
        bias_level_v=settings.bias_level_v,
        monitor1=settings.monitor1,
        monitor2=settings.monitor2,
        alc_enabled=settings.alc_enabled,
    )


def _configure_keithley_session(
    session: object,
    configuration: RouteMeterConfiguration,
    error_message: str,
) -> None:
    settings = configuration.keithley
    configure = getattr(session, "configure_measurement", None)
    if not callable(configure):
        raise LCRMeterError(error_message)
    configure(
        keithley_measurement_voltage_v=settings.measurement_voltage_v,
        keithley_range_mode=settings.range_mode,
        keithley_expected_resistance_ohm=settings.expected_resistance_ohm,
        keithley_minimum_resistance_ohm=settings.minimum_resistance_ohm,
        keithley_maximum_current_a=settings.maximum_current_a,
        keithley_voltage_range_v=settings.voltage_range_v,
        keithley_source_voltage_range_v=settings.source_voltage_range_v,
        keithley_voltmeter_range_v=settings.voltmeter_range_v,
        keithley_current_range_a=settings.current_range_a,
        keithley_compliance_current_a=settings.compliance_current_a,
        keithley_range_voltage_headroom=settings.range_voltage_headroom,
        keithley_range_current_headroom=settings.range_current_headroom,
        keithley_nplc=settings.nplc,
        keithley_terminals=settings.terminals,
        keithley_trigger_delay_s=settings.trigger_delay_s,
        keithley_use_buffer=settings.use_buffer,
        keithley_use_trigger_link=settings.use_trigger_link,
    )


def read_route_measurement_from_session(
    session: object,
    *,
    cannot_read_message: str,
) -> dict[str, object]:
    reader = getattr(session, "read_route_measurement", None)
    if callable(reader):
        return dict(reader(trigger=True))
    primary_reader = getattr(session, "read_primary_value", None)
    if not callable(primary_reader):
        raise LCRMeterError(cannot_read_message)
    primary_value = primary_reader(trigger=True)
    return {"differential_resistance_ohm": primary_value}


def read_route_measurement_batch_from_session(
    session: object,
    count: int,
    *,
    after_measurement: object | None,
    cannot_read_message: str,
) -> list[dict[str, object]]:
    batch_reader = getattr(session, "read_route_measurements", None)
    if callable(batch_reader):
        return [
            dict(item)
            for item in read_route_measurement_batch(
                batch_reader,
                count,
                after_measurement=after_measurement,
            )
        ]
    return [
        read_route_measurement_from_session(
            session,
            cannot_read_message=cannot_read_message,
        )
        for _index in range(count)
    ]


def prepare_route_measurement_batch_on_session(
    session: object,
    count: int,
    *,
    source_list_count: int | None,
) -> None:
    preparer = getattr(session, "prepare_route_measurements", None)
    prepare_route_measurement_batch(
        preparer,
        count,
        source_list_count=source_list_count,
    )


class RouteMeter:
    """Own one configured measurement backend for a route run."""

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
            self._configuration.meter_type,
            self._configuration.meter_type,
        )

    def open(self) -> None:
        if self._session is not None:
            return
        if self._configuration.meter_type == ROUTE_METER_GWINSTEK:
            settings = self._configuration.gwinstek
            session = GWInstekLCRSession(settings.resource_name, self._timeout_ms)
            try:
                session.identify()
                configure_route_session(
                    session,
                    self._configuration,
                    gwinstek_session_type=GWInstekLCRSession,
                    gwinstek_error="Open route instrument is not a GW Instek LCR.",
                    keithley_error="Open route instrument is not a Keithley 2400.",
                )
            except Exception:
                session.close()
                raise
            self._session = session
            return
        if self._configuration.meter_type in ROUTE_METER_KEITHLEY_TYPES:
            settings = self._configuration.keithley
            voltmeter_resource = (
                str(settings.voltmeter_resource or "").strip()
                if self._configuration.meter_type == ROUTE_METER_KEITHLEY
                else ""
            )
            session = open_keithley_session(
                settings.source_resource,
                voltmeter_resource,
                self._timeout_ms,
            )
            try:
                identify = getattr(session, "identify", None)
                if callable(identify):
                    identify()
                configure_route_session(
                    session,
                    self._configuration,
                    gwinstek_session_type=GWInstekLCRSession,
                    gwinstek_error="Open route instrument is not a GW Instek LCR.",
                    keithley_error="Open route instrument is not a Keithley 2400.",
                )
            except Exception:
                closer = getattr(session, "close", None)
                if callable(closer):
                    closer()
                raise
            self._session = session
            return
        raise LCRMeterError(
            "Unsupported route measurement instrument: "
            f"{self._configuration.meter_type}"
        )

    def apply_route_meter_configuration(
        self,
        configuration: RouteMeterConfiguration,
    ) -> None:
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
                f"Open route instrument is {configured_label}; "
                f"route requested {requested_label}."
            )
        configure_route_session(
            self._session,
            configuration,
            gwinstek_session_type=GWInstekLCRSession,
            gwinstek_error="Open route instrument is not a GW Instek LCR.",
            keithley_error="Open route instrument is not a Keithley 2400.",
        )
        self._configuration = configuration

    def read_primary_value_now(self, *, restart_polling: bool = False) -> float:
        _ = restart_polling
        session = self._open_session()
        reader = getattr(session, "read_primary_value", None)
        if not callable(reader):
            raise LCRMeterError("Route measurement instrument cannot read values.")
        return float(reader(trigger=True))

    def read_route_measurement_now(
        self,
        *,
        restart_polling: bool = False,
    ) -> dict[str, object]:
        _ = restart_polling
        return read_route_measurement_from_session(
            self._open_session(),
            cannot_read_message=("Route measurement instrument cannot read values."),
        )

    def read_route_measurement_batch_now(
        self,
        count: int,
        *,
        restart_polling: bool = False,
        after_measurement: object | None = None,
    ) -> list[dict[str, object]]:
        _ = restart_polling
        return read_route_measurement_batch_from_session(
            self._open_session(),
            max(1, int(count)),
            after_measurement=after_measurement,
            cannot_read_message=("Route measurement instrument cannot read values."),
        )

    def prepare_route_measurement_batch_now(
        self,
        count: int,
        *,
        source_list_count: int | None = None,
    ) -> None:
        prepare_route_measurement_batch_on_session(
            self._open_session(),
            max(1, int(count)),
            source_list_count=source_list_count,
        )

    @contextmanager
    def output(self, enabled: bool = True) -> Iterator[RouteMeter]:
        output = getattr(self._open_session(), "output", None)
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
        return session_visa_resource_roles(
            self._open_session(),
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
        return session_visa_operation(
            self._open_session(),
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

    def _open_session(self) -> object:
        if self._session is None:
            self.open()
        if self._session is None:
            raise LCRMeterError("Route measurement instrument is not open.")
        return self._session


__all__ = ["RouteMeter"]
