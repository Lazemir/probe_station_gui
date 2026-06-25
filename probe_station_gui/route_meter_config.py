"""Route-meter selection and per-run settings."""

from __future__ import annotations

from dataclasses import dataclass, field

from probe_station_measure import OHMMETER_RANGE_MANUAL


ROUTE_METER_GWINSTEK = "gwinstek_lcr_76200"
ROUTE_METER_KEITHLEY = "keithley_2400_2182a"
ROUTE_METER_TYPES: tuple[str, ...] = (
    ROUTE_METER_GWINSTEK,
    ROUTE_METER_KEITHLEY,
)
ROUTE_METER_LABELS: dict[str, str] = {
    ROUTE_METER_GWINSTEK: "GW Instek LCR-76200",
    ROUTE_METER_KEITHLEY: "Keithley 2400 + 2182A",
}


@dataclass(frozen=True)
class GWInstekRouteMeterSettings:
    """Per-run GW Instek LCR settings for route measurements."""

    resource_name: str = "COM4"
    measurement_function: str = "DCR"
    range_mode: str = "AUTO"
    impedance_range: int = 3
    dcr_range: int = 4
    frequency_hz: float = 50.0
    level_mode: str = "VOLTAGE"
    voltage_level_v: float = 0.03
    current_level_a: float = 0.0001
    source_resistance_ohm: int = 100
    aperture_rate: str = "SLOW"
    aperture_averages: int = 1
    trigger_delay_s: float = 0.0
    bias_enabled: bool = False
    bias_level_v: float = 0.0
    monitor1: str = "OFF"
    monitor2: str = "OFF"
    alc_enabled: bool = False


@dataclass(frozen=True)
class KeithleyRouteMeterSettings:
    """Per-run four-wire resistance settings for a Keithley 2400 and 2182A."""

    source_resource: str = "GPIB0::1::INSTR"
    voltmeter_resource: str = "GPIB0::2::INSTR"
    measurement_voltage_v: float = 0.03
    range_mode: str = OHMMETER_RANGE_MANUAL
    expected_resistance_ohm: float | None = None
    minimum_resistance_ohm: float | None = None
    maximum_current_a: float | None = None
    voltage_range_v: float | None = None
    source_voltage_range_v: float = 0.21
    voltmeter_range_v: float = 0.1
    current_range_a: float = 10e-6
    compliance_current_a: float = 10e-6
    range_voltage_headroom: float = 1.2
    range_current_headroom: float = 2.0
    nplc: float = 1.0
    terminals: str = "rear"
    trigger_delay_s: float = 0.0
    use_buffer: bool = True
    use_trigger_link: bool = True


@dataclass(frozen=True)
class RouteMeterConfiguration:
    """Per-run meter selection and settings from the route measurement dialog."""

    meter_type: str = ROUTE_METER_KEITHLEY
    gwinstek: GWInstekRouteMeterSettings = field(
        default_factory=GWInstekRouteMeterSettings
    )
    keithley: KeithleyRouteMeterSettings = field(
        default_factory=KeithleyRouteMeterSettings
    )

    def nplc_label(self) -> str:
        """Return the CSV integration-time label for this meter."""

        if self.meter_type == ROUTE_METER_KEITHLEY:
            return f"{float(self.keithley.nplc):g}"
        return ""

    def measurement_type_label(self) -> str:
        """Return a concise CSV label for the route measurement mode."""

        if self.meter_type == ROUTE_METER_KEITHLEY:
            voltage = float(self.keithley.measurement_voltage_v)
            return f"Keithley voltage sweep +/-{voltage:g} V"
        function = str(self.gwinstek.measurement_function or "").strip()
        if function.upper() == "DCR":
            return "GW Instek DCR"
        return f"GW Instek {function or 'measurement'}"


__all__ = [
    "GWInstekRouteMeterSettings",
    "KeithleyRouteMeterSettings",
    "ROUTE_METER_GWINSTEK",
    "ROUTE_METER_KEITHLEY",
    "ROUTE_METER_LABELS",
    "ROUTE_METER_TYPES",
    "RouteMeterConfiguration",
]
