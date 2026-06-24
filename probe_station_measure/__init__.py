"""Measurement helpers for probe-station electrical checks."""

from __future__ import annotations

from importlib import import_module
from typing import Any

from .ohmmeter import (
    OHMMETER_RANGE_CODE_AUTO,
    OHMMETER_RANGE_DEVICE_AUTO,
    OHMMETER_RANGE_MANUAL,
    OHMMETER_RANGE_MODES,
    AbstractOhmmeter,
    OhmmeterRangeCapabilities,
    OhmmeterRangeDefaults,
    OhmmeterRangeRequest,
    ResolvedOhmmeterRanges,
    normalize_range_mode,
    resolve_ohmmeter_ranges,
)

_LAZY_EXPORTS = {
    "ContactQuality": (
        "probe_station_measure.instrument_drivers.Keithley.Keithley_2400_2182A",
        "ContactQuality",
    ),
    "ContactQualityCheck": (
        "probe_station_measure.instrument_drivers.Keithley.Keithley_2400_2182A",
        "ContactQualityCheck",
    ),
    "ContactQualityCriteria": (
        "probe_station_measure.instrument_drivers.Keithley.Keithley_2400_2182A",
        "ContactQualityCriteria",
    ),
    "DifferentialReading": (
        "probe_station_measure.instrument_drivers.Keithley.Keithley_2400_2182A",
        "DifferentialReading",
    ),
    "Keithley2400SourceMeter": (
        "probe_station_measure.instrument_drivers.Keithley.Keithley_2400_2182A",
        "Keithley2400SourceMeter",
    ),
    "Keithley2400With2182A": (
        "probe_station_measure.instrument_drivers.Keithley.Keithley_2400_2182A",
        "Keithley2400With2182A",
    ),
    "Keithley2400With2182AConfig": (
        "probe_station_measure.instrument_drivers.Keithley.Keithley_2400_2182A",
        "Keithley2400With2182AConfig",
    ),
    "PolarityReading": (
        "probe_station_measure.instrument_drivers.Keithley.Keithley_2400_2182A",
        "PolarityReading",
    ),
    "TraceBufferStatus": (
        "probe_station_measure.instrument_drivers.Keithley.Keithley_2400_2182A",
        "TraceBufferStatus",
    ),
    "VoltageListReading": (
        "probe_station_measure.instrument_drivers.Keithley.Keithley_2400_2182A",
        "VoltageListReading",
    ),
    "evaluate_contact_quality": (
        "probe_station_measure.instrument_drivers.Keithley.Keithley_2400_2182A",
        "evaluate_contact_quality",
    ),
    "summarize_contact_quality": (
        "probe_station_measure.instrument_drivers.Keithley.Keithley_2400_2182A",
        "summarize_contact_quality",
    ),
}

__all__ = [
    "OHMMETER_RANGE_CODE_AUTO",
    "OHMMETER_RANGE_DEVICE_AUTO",
    "OHMMETER_RANGE_MANUAL",
    "OHMMETER_RANGE_MODES",
    "AbstractOhmmeter",
    "ContactQuality",
    "ContactQualityCheck",
    "ContactQualityCriteria",
    "DifferentialReading",
    "Keithley2400SourceMeter",
    "Keithley2400With2182A",
    "Keithley2400With2182AConfig",
    "OhmmeterRangeCapabilities",
    "OhmmeterRangeDefaults",
    "OhmmeterRangeRequest",
    "PolarityReading",
    "ResolvedOhmmeterRanges",
    "TraceBufferStatus",
    "VoltageListReading",
    "evaluate_contact_quality",
    "normalize_range_mode",
    "resolve_ohmmeter_ranges",
    "summarize_contact_quality",
]


def __getattr__(name: str) -> Any:
    try:
        module_name, attribute_name = _LAZY_EXPORTS[name]
    except KeyError as exc:
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}") from exc
    value = getattr(import_module(module_name), attribute_name)
    globals()[name] = value
    return value
