"""Session-level route measurement helpers for LCR meter backends."""

from __future__ import annotations

from probe_station_gui.instruments.meters.lcr_helpers import (
    prepare_route_measurement_batch,
    read_route_measurement_batch,
)
from probe_station_gui.route.meter_config import (
    ROUTE_METER_GWINSTEK,
    ROUTE_METER_KEITHLEY,
    RouteMeterConfiguration,
)


class RouteSessionError(RuntimeError):
    """Raised when a route meter session cannot perform a requested operation."""


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
            raise RouteSessionError(gwinstek_error)
        _configure_gwinstek_session(session, configuration)
        return
    if configuration.meter_type == ROUTE_METER_KEITHLEY:
        if isinstance(session, gwinstek_session_type):
            raise RouteSessionError(keithley_error)
        _configure_keithley_session(session, configuration, keithley_error)
        return
    raise RouteSessionError(
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
        raise RouteSessionError(error_message)
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
        raise RouteSessionError(cannot_read_message)
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
