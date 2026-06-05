"""Composite Keithley 2400 + 2182A voltage-list measurements.

The fast path uses the documented 2182A voltmeter-complete Trigger Link
handshake: the 2400 advances the source-voltage list only after the 2182A has
finished storing the previous voltage reading.  Differential-resistance
readings are a small convenience layer over this list measurement.
"""

from __future__ import annotations

from collections.abc import Callable, Iterator
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
import logging
import math
import re
import time
from dataclasses import dataclass, replace
from typing import Sequence

import numpy as np

from probe_station_measure.ohmmeter import (
    OHMMETER_RANGE_MANUAL,
    AbstractOhmmeter,
    OhmmeterRangeCapabilities,
    OhmmeterRangeDefaults,
    OhmmeterRangeRequest,
    normalize_range_mode,
    resolve_ohmmeter_ranges,
)


logger = logging.getLogger(__name__)

MAX_2400_SOURCE_LIST_POINTS = 2500
MAX_2400_SOURCE_LIST_POINTS_PER_COMMAND = 100
MAX_2182A_TRACE_POINTS = 1024
DEFAULT_BUFFER_POINTS_PER_CHUNK = 500
DEFAULT_VISA_TIMEOUT_MS = 10_000
MAX_BUFFERED_MEASUREMENT_TIMEOUT_MS = 600_000
BUFFERED_MEASUREMENT_BASE_TIMEOUT_S = 10.0
BUFFERED_MEASUREMENT_POINT_OVERHEAD_S = 0.08
BUFFERED_MEASUREMENT_NPLC_POINT_S = 0.05
KEITHLEY_2400_SOURCE_VOLTAGE_RANGES_V = (0.21, 2.1, 21.0, 210.0)
KEITHLEY_2182A_VOLTAGE_RANGES_V = (0.01, 0.1, 1.0, 10.0, 100.0)
KEITHLEY_2400_CURRENT_RANGES_A = (
    1e-6,
    10e-6,
    100e-6,
    1e-3,
    10e-3,
    100e-3,
    1.0,
)
KEITHLEY_RANGE_CAPABILITIES = OhmmeterRangeCapabilities(
    source_voltage_ranges_v=KEITHLEY_2400_SOURCE_VOLTAGE_RANGES_V,
    voltmeter_ranges_v=KEITHLEY_2182A_VOLTAGE_RANGES_V,
    current_ranges_a=KEITHLEY_2400_CURRENT_RANGES_A,
)


@dataclass(frozen=True)
class Keithley2400With2182AConfig:
    """Settings for one 2400+2182A voltage-list measurement batch."""

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
    max_buffer_points_per_chunk: int = DEFAULT_BUFFER_POINTS_PER_CHUNK

    def normalized(self) -> "Keithley2400With2182AConfig":
        voltage = _positive_finite(self.measurement_voltage_v, 0.03)
        range_mode = normalize_range_mode(self.range_mode)
        ranges = resolve_ohmmeter_ranges(
            measurement_voltage_v=voltage,
            request=OhmmeterRangeRequest(
                mode=range_mode,
                expected_resistance_ohm=_optional_positive_float(
                    self.expected_resistance_ohm
                ),
                minimum_resistance_ohm=_optional_positive_float(
                    self.minimum_resistance_ohm
                ),
                maximum_current_a=_optional_positive_float(self.maximum_current_a),
                voltage_range_v=_optional_positive_float(self.voltage_range_v),
                current_range_a=_optional_positive_float(self.current_range_a),
                compliance_current_a=_optional_positive_float(
                    self.compliance_current_a
                ),
                voltage_headroom=_positive_finite(self.range_voltage_headroom, 1.2),
                current_headroom=_positive_finite(self.range_current_headroom, 2.0),
            ),
            defaults=OhmmeterRangeDefaults(
                source_voltage_range_v=0.21,
                voltmeter_range_v=0.1,
                current_range_a=10e-6,
                compliance_current_a=10e-6,
            ),
            capabilities=KEITHLEY_RANGE_CAPABILITIES,
        )
        source_voltage_range = ranges.source_voltage_range_v
        voltmeter_range = ranges.voltmeter_range_v
        if range_mode == OHMMETER_RANGE_MANUAL and self.voltage_range_v is None:
            source_voltage_range = max(
                voltage,
                _positive_finite(self.source_voltage_range_v, 0.21),
            )
            voltmeter_range = max(
                voltage,
                _positive_finite(self.voltmeter_range_v, 0.1),
            )
        nplc = _finite_float(self.nplc, 1.0)
        nplc = max(0.01, min(50.0, nplc))
        terminals = str(self.terminals).strip().lower()
        terminals = "front" if terminals == "front" else "rear"
        trigger_delay = max(0.0, _finite_float(self.trigger_delay_s, 0.0))
        max_points = max(
            1,
            int(
                _finite_float(
                    self.max_buffer_points_per_chunk,
                    DEFAULT_BUFFER_POINTS_PER_CHUNK,
                )
            ),
        )
        max_points = min(
            max_points,
            MAX_2400_SOURCE_LIST_POINTS,
            MAX_2182A_TRACE_POINTS,
        )
        return Keithley2400With2182AConfig(
            measurement_voltage_v=voltage,
            range_mode=ranges.mode,
            expected_resistance_ohm=_optional_positive_float(
                self.expected_resistance_ohm
            ),
            minimum_resistance_ohm=_optional_positive_float(
                self.minimum_resistance_ohm
            ),
            maximum_current_a=_optional_positive_float(self.maximum_current_a),
            voltage_range_v=_optional_positive_float(self.voltage_range_v),
            source_voltage_range_v=source_voltage_range,
            voltmeter_range_v=voltmeter_range,
            current_range_a=ranges.current_range_a,
            compliance_current_a=ranges.compliance_current_a,
            range_voltage_headroom=max(
                1.0,
                _positive_finite(self.range_voltage_headroom, 1.2),
            ),
            range_current_headroom=max(
                1.0,
                _positive_finite(self.range_current_headroom, 2.0),
            ),
            nplc=nplc,
            terminals=terminals,
            trigger_delay_s=trigger_delay,
            use_buffer=bool(self.use_buffer),
            use_trigger_link=bool(self.use_trigger_link),
            max_buffer_points_per_chunk=max_points,
        )


@dataclass(frozen=True)
class PolarityReading:
    """One source polarity from a differential pair."""

    polarity: str
    source_voltage_v: float
    measured_voltage_v: float
    current_a: float
    resistance_ohm: float

    def as_dict(self) -> dict[str, float | str]:
        return {
            "polarity": self.polarity,
            "source_voltage_v": self.source_voltage_v,
            "measured_voltage_v": self.measured_voltage_v,
            "current_a": self.current_a,
            "resistance_ohm": self.resistance_ohm,
        }


@dataclass(frozen=True)
class VoltageListReading:
    """One point returned by a programmed source-voltage list."""

    source_voltage_v: float
    measured_voltage_v: float
    current_a: float
    resistance_ohm: float
    compliance_hit: bool = False

    def as_dict(self) -> dict[str, float | bool]:
        return {
            "source_voltage_v": self.source_voltage_v,
            "measured_voltage_v": self.measured_voltage_v,
            "current_a": self.current_a,
            "resistance_ohm": self.resistance_ohm,
            "compliance_hit": self.compliance_hit,
        }


@dataclass(frozen=True)
class DifferentialReading:
    """One two-polarity differential resistance reading."""

    negative: PolarityReading
    positive: PolarityReading
    differential_resistance_ohm: float
    compliance_hit: bool = False

    def as_route_dict(self) -> dict[str, object]:
        return {
            "differential_resistance_ohm": self.differential_resistance_ohm,
            "compliance_hit": self.compliance_hit,
            "negative": self.negative.as_dict(),
            "positive": self.positive.as_dict(),
        }


@dataclass(frozen=True)
class ContactQuality:
    """Robust summary for deciding whether a contact is stable enough."""

    readings: tuple[DifferentialReading, ...]
    median_ohm: float
    mean_ohm: float
    std_ohm: float
    sem_ohm: float
    mad_sigma_ohm: float
    p95_abs_deviation_ohm: float
    p95_abs_step_ohm: float
    span_ohm: float
    outlier_fraction_gt_20ohm: float
    outlier_fraction_gt_50ohm: float
    outlier_fraction_gt_100ohm: float
    polarity_sign_mismatch_count: int


@dataclass(frozen=True)
class ContactQualityCriteria:
    """Thresholds for classifying a quick contact check."""

    max_abs_median_ohm: float = 50_000.0
    max_mad_sigma_ohm: float = 300.0
    max_p95_abs_step_ohm: float = 1_000.0
    max_compliance_hits: int = 0
    max_polarity_sign_mismatch_count: int = 0


@dataclass(frozen=True)
class ContactQualityCheck:
    """Contact quality summary plus pass/fail classification."""

    quality: ContactQuality
    criteria: ContactQualityCriteria
    good: bool
    status: str
    reasons: tuple[str, ...]

    def as_dict(self) -> dict[str, object]:
        quality = self.quality
        return {
            "good": self.good,
            "status": self.status,
            "reasons": list(self.reasons),
            "count": len(quality.readings),
            "median_ohm": quality.median_ohm,
            "mean_ohm": quality.mean_ohm,
            "std_ohm": quality.std_ohm,
            "sem_ohm": quality.sem_ohm,
            "mad_sigma_ohm": quality.mad_sigma_ohm,
            "p95_abs_deviation_ohm": quality.p95_abs_deviation_ohm,
            "p95_abs_step_ohm": quality.p95_abs_step_ohm,
            "span_ohm": quality.span_ohm,
            "outlier_fraction_gt_20ohm": quality.outlier_fraction_gt_20ohm,
            "outlier_fraction_gt_50ohm": quality.outlier_fraction_gt_50ohm,
            "outlier_fraction_gt_100ohm": quality.outlier_fraction_gt_100ohm,
            "polarity_sign_mismatch_count": quality.polarity_sign_mismatch_count,
            "compliance_hits": sum(
                1 for reading in quality.readings if reading.compliance_hit
            ),
        }


@dataclass(frozen=True)
class TraceBufferStatus:
    """A compact snapshot of one instrument trace buffer."""

    points: int | None = None
    actual_points: int | None = None
    free_bytes: int | None = None
    reserved_bytes: int | None = None
    feed: str | None = None
    control: str | None = None


def summarize_contact_quality(
    readings: Sequence[DifferentialReading],
) -> ContactQuality:
    """Calculate the robust contact metrics used by the route workflow."""

    readings_tuple = tuple(readings)
    values = np.array(
        [
            reading.differential_resistance_ohm
            for reading in readings_tuple
            if math.isfinite(reading.differential_resistance_ohm)
        ],
        dtype=float,
    )
    if values.size == 0:
        median = mean = std = sem = mad_sigma = p95_dev = p95_step = span = math.nan
        out20 = out50 = out100 = math.nan
    else:
        median = float(np.median(values))
        mean = float(np.mean(values))
        std = float(np.std(values, ddof=1)) if values.size > 1 else 0.0
        sem = float(std / math.sqrt(values.size)) if values.size > 0 else math.nan
        abs_dev = np.abs(values - median)
        mad_sigma = float(1.4826 * np.median(abs_dev))
        p95_dev = float(np.percentile(abs_dev, 95))
        steps = np.abs(np.diff(values))
        p95_step = float(np.percentile(steps, 95)) if steps.size else 0.0
        span = float(np.max(values) - np.min(values))
        out20 = float(np.mean(abs_dev > 20.0))
        out50 = float(np.mean(abs_dev > 50.0))
        out100 = float(np.mean(abs_dev > 100.0))
    sign_mismatches = sum(
        1
        for reading in readings_tuple
        if (
            math.isfinite(reading.negative.current_a)
            and math.isfinite(reading.positive.current_a)
            and reading.negative.current_a * reading.positive.current_a >= 0.0
        )
    )
    return ContactQuality(
        readings=readings_tuple,
        median_ohm=median,
        mean_ohm=mean,
        std_ohm=std,
        sem_ohm=sem,
        mad_sigma_ohm=mad_sigma,
        p95_abs_deviation_ohm=p95_dev,
        p95_abs_step_ohm=p95_step,
        span_ohm=span,
        outlier_fraction_gt_20ohm=out20,
        outlier_fraction_gt_50ohm=out50,
        outlier_fraction_gt_100ohm=out100,
        polarity_sign_mismatch_count=sign_mismatches,
    )


def evaluate_contact_quality(
    quality: ContactQuality,
    criteria: ContactQualityCriteria | None = None,
) -> ContactQualityCheck:
    """Classify a contact-quality summary against practical route thresholds."""

    criteria = criteria or ContactQualityCriteria()
    reasons: list[str] = []
    compliance_hits = sum(1 for reading in quality.readings if reading.compliance_hit)
    if len(quality.readings) < 2:
        reasons.append("too_few_readings")
    if (
        not math.isfinite(quality.median_ohm)
        or abs(quality.median_ohm) > criteria.max_abs_median_ohm
    ):
        reasons.append("median_out_of_range")
    if (
        not math.isfinite(quality.mad_sigma_ohm)
        or quality.mad_sigma_ohm > criteria.max_mad_sigma_ohm
    ):
        reasons.append("mad_sigma_too_high")
    if (
        not math.isfinite(quality.p95_abs_step_ohm)
        or quality.p95_abs_step_ohm > criteria.max_p95_abs_step_ohm
    ):
        reasons.append("step_noise_too_high")
    if compliance_hits > criteria.max_compliance_hits:
        reasons.append("compliance_hit")
    if (
        quality.polarity_sign_mismatch_count
        > criteria.max_polarity_sign_mismatch_count
    ):
        reasons.append("polarity_sign_mismatch")
    good = not reasons
    return ContactQualityCheck(
        quality=quality,
        criteria=criteria,
        good=good,
        status="good" if good else "bad_contact",
        reasons=tuple(reasons),
    )


class Keithley2400With2182A(AbstractOhmmeter):
    """One combined instrument made from a 2400 source-meter and 2182A voltmeter."""

    backend_name = "probe-station-measure"

    def __init__(
        self,
        source_resource: str,
        voltmeter_resource: str,
        *,
        timeout_ms: int = DEFAULT_VISA_TIMEOUT_MS,
        resource_manager: object | None = None,
    ) -> None:
        if resource_manager is None:
            try:
                import pyvisa
            except ImportError as exc:  # pragma: no cover - environment specific
                raise RuntimeError(
                    "Keithley measurements require pyvisa. "
                    "Install probe-station-measure with the 'visa' extra."
                ) from exc
            resource_manager = pyvisa.ResourceManager()
            self._owns_resource_manager = True
        else:
            self._owns_resource_manager = False
        self._resource_manager = resource_manager
        self._source = None
        self._voltmeter = None
        self._config = Keithley2400With2182AConfig().normalized()
        self._common_config_key: Keithley2400With2182AConfig | None = None
        self._source_list_cache: tuple[float, ...] = ()
        self._prepared_voltage_list_key: tuple[
            Keithley2400With2182AConfig,
            tuple[float, ...],
            int,
        ] | None = None
        self._prepared_source_voltages: tuple[float, ...] = ()
        self._source = self._open_resource(source_resource, timeout_ms)
        self._voltmeter = self._open_resource(voltmeter_resource, timeout_ms)

    def _open_resource(self, address: str, timeout_ms: int):
        handle = self._resource_manager.open_resource(str(address).strip())
        handle.timeout = int(timeout_ms)
        for attribute, value in (
            ("read_termination", "\n"),
            ("write_termination", "\n"),
        ):
            try:
                setattr(handle, attribute, value)
            except Exception:
                logger.debug(
                    "VISA handle does not accept %s=%r",
                    attribute,
                    value,
                    exc_info=True,
                )
        return handle

    def identify(self) -> str:
        parts = []
        source_id = self._safe_query(self._source, "*IDN?")
        voltmeter_id = self._safe_query(self._voltmeter, "*IDN?")
        if source_id:
            parts.append(f"2400 {source_id}")
        if voltmeter_id:
            parts.append(f"2182A {voltmeter_id}")
        return "; ".join(parts)

    def configure(self, config: Keithley2400With2182AConfig | None = None) -> None:
        self._config = (config or self._config).normalized()
        self._configure_common(self._config, force=True)

    def configure_measurement(
        self,
        *,
        keithley_measurement_voltage_v: float = 0.03,
        keithley_range_mode: str = OHMMETER_RANGE_MANUAL,
        keithley_expected_resistance_ohm: float | None = None,
        keithley_minimum_resistance_ohm: float | None = None,
        keithley_maximum_current_a: float | None = None,
        keithley_voltage_range_v: float | None = None,
        keithley_source_voltage_range_v: float = 0.21,
        keithley_voltmeter_range_v: float = 0.1,
        keithley_current_range_a: float = 10e-6,
        keithley_compliance_current_a: float = 10e-6,
        keithley_range_voltage_headroom: float = 1.2,
        keithley_range_current_headroom: float = 2.0,
        keithley_nplc: float = 1.0,
        keithley_terminals: str = "rear",
        keithley_trigger_delay_s: float = 0.0,
        keithley_use_buffer: bool = True,
        keithley_use_trigger_link: bool = True,
        **_ignored: object,
    ) -> None:
        """Configure from route-measurement keyword names."""

        self.configure(
            Keithley2400With2182AConfig(
                measurement_voltage_v=keithley_measurement_voltage_v,
                range_mode=keithley_range_mode,
                expected_resistance_ohm=keithley_expected_resistance_ohm,
                minimum_resistance_ohm=keithley_minimum_resistance_ohm,
                maximum_current_a=keithley_maximum_current_a,
                voltage_range_v=keithley_voltage_range_v,
                source_voltage_range_v=keithley_source_voltage_range_v,
                voltmeter_range_v=keithley_voltmeter_range_v,
                current_range_a=keithley_current_range_a,
                compliance_current_a=keithley_compliance_current_a,
                range_voltage_headroom=keithley_range_voltage_headroom,
                range_current_headroom=keithley_range_current_headroom,
                nplc=keithley_nplc,
                terminals=keithley_terminals,
                trigger_delay_s=keithley_trigger_delay_s,
                use_buffer=keithley_use_buffer,
                use_trigger_link=keithley_use_trigger_link,
            )
        )

    def measure_pair(
        self,
        config: Keithley2400With2182AConfig | None = None,
    ) -> DifferentialReading:
        return self.measure_pairs(1, config=config)[0]

    def measure_voltage_list(
        self,
        source_voltages_v: Sequence[float],
        config: Keithley2400With2182AConfig | None = None,
        *,
        after_measurement: Callable[[], object] | None = None,
    ) -> list[VoltageListReading]:
        """Source a voltage sequence and return one measured point per value.

        Args:
            source_voltages_v: Source voltages in volts, in execution order.
            config: Instrument setup for ranges, NPLC, buffering and triggers.

        Returns:
            Flat readings in the same order as ``source_voltages_v``.
        """

        source_voltages = [float(value) for value in source_voltages_v]
        if not source_voltages:
            raise ValueError("Source voltage list cannot be empty")
        if any(not math.isfinite(value) for value in source_voltages):
            raise ValueError("Source voltage list contains a non-finite value")
        cfg = (config or self._config).normalized()
        max_source_voltage = max(abs(value) for value in source_voltages)
        if max_source_voltage > cfg.source_voltage_range_v:
            cfg = replace(cfg, source_voltage_range_v=max_source_voltage)
        self._config = cfg

        if cfg.use_buffer and cfg.use_trigger_link and len(source_voltages) > 1:
            readings: list[VoltageListReading] = []
            chunk_size = max(1, int(cfg.max_buffer_points_per_chunk))
            for offset in range(0, len(source_voltages), chunk_size):
                readings.extend(
                    self._measure_voltage_list_buffered_trigger_link(
                        source_voltages[offset : offset + chunk_size],
                        cfg,
                        after_measurement=(
                            after_measurement
                            if offset + chunk_size >= len(source_voltages)
                            else None
                        ),
                    )
                )
            return readings
        return self._measure_voltage_list_software(source_voltages, cfg)

    def measure_repeated_voltage_list(
        self,
        source_voltages_v: Sequence[float],
        repeats: int,
        config: Keithley2400With2182AConfig | None = None,
        *,
        after_measurement: Callable[[], object] | None = None,
    ) -> list[list[VoltageListReading]]:
        """Repeat one source-voltage pattern and split readings by repetition."""

        pattern = [float(value) for value in source_voltages_v]
        if not pattern:
            raise ValueError("Source voltage pattern cannot be empty")
        repeat_count = int(repeats)
        if repeat_count < 1:
            raise ValueError("Repeat count must be at least 1")
        flat_readings = self.measure_voltage_list(
            pattern * repeat_count,
            config,
            after_measurement=after_measurement,
        )
        pattern_size = len(pattern)
        return [
            flat_readings[offset : offset + pattern_size]
            for offset in range(0, len(flat_readings), pattern_size)
        ]

    def measure_pairs(
        self,
        count: int,
        config: Keithley2400With2182AConfig | None = None,
        *,
        after_measurement: Callable[[], object] | None = None,
    ) -> list[DifferentialReading]:
        cfg = (config or self._config).normalized()
        self._config = cfg
        count = max(1, int(count))
        repeated = self.measure_repeated_voltage_list(
            [-cfg.measurement_voltage_v, cfg.measurement_voltage_v],
            count,
            cfg,
            after_measurement=after_measurement,
        )
        voltage_readings = [reading for group in repeated for reading in group]
        return _differential_readings_from_voltage_list(
            voltage_readings,
            cfg.compliance_current_a,
        )

    def measure_contact_quality(
        self,
        count: int = 250,
        config: Keithley2400With2182AConfig | None = None,
    ) -> ContactQuality:
        return summarize_contact_quality(self.measure_pairs(count, config=config))

    def check_contact_quality(
        self,
        count: int = 25,
        config: Keithley2400With2182AConfig | None = None,
        criteria: ContactQualityCriteria | None = None,
    ) -> ContactQualityCheck:
        return evaluate_contact_quality(
            self.measure_contact_quality(count=count, config=config),
            criteria,
        )

    def read_primary_value(self, *, trigger: bool = False) -> float:
        _ = trigger
        return float(self.measure_pair().differential_resistance_ohm)

    def read_route_measurement(self, *, trigger: bool = False) -> dict[str, object]:
        _ = trigger
        return dict(self.measure_pair().as_route_dict())

    def read_route_measurements(
        self,
        count: int,
        *,
        trigger: bool = False,
        after_measurement: Callable[[], object] | None = None,
    ) -> list[dict[str, object]]:
        _ = trigger
        return [
            dict(reading.as_route_dict())
            for reading in self.measure_pairs(
                max(1, int(count)),
                after_measurement=after_measurement,
            )
        ]

    def prepare_route_measurements(
        self,
        count: int,
        *,
        source_list_count: int | None = None,
    ) -> None:
        """Prepare a buffered route batch without starting source output."""

        cfg = self._config.normalized()
        measurement_count = max(1, int(count))
        list_count = (
            measurement_count
            if source_list_count is None
            else max(measurement_count, int(source_list_count))
        )
        source_voltages = [
            voltage
            for _index in range(list_count)
            for voltage in (-cfg.measurement_voltage_v, cfg.measurement_voltage_v)
        ]
        self._prepare_voltage_list_buffered_trigger_link(
            source_voltages,
            cfg,
            measure_points=measurement_count * 2,
        )

    def abort(self) -> None:
        if self._voltmeter is not None:
            self._try_write(self._voltmeter, "ABOR")
        if self._source is not None:
            self._try_write(self._source, ":ABOR")
            self._try_write(self._source, ":SOUR:VOLT 0")
            self._try_write(self._source, ":OUTP OFF")

    def abort_measurement(self) -> None:
        self.abort()

    def trace_status(self) -> dict[str, TraceBufferStatus]:
        """Return trace-buffer state for diagnostics."""

        return {
            "source": self._trace_status(
                self._require_source(),
                supports_actual_points=True,
            ),
            "voltmeter": self._trace_status(
                self._require_voltmeter(),
                supports_actual_points=False,
            ),
        }

    def close(self) -> None:
        source = self._source
        voltmeter = self._voltmeter
        self._source = None
        self._voltmeter = None
        if source is not None:
            self._try_write(source, ":SOUR:VOLT 0")
            self._try_write(source, ":OUTP OFF")
            _close_handle(source)
        if voltmeter is not None:
            _close_handle(voltmeter)
        if self._owns_resource_manager:
            _close_handle(self._resource_manager)

    def _configure_common(
        self,
        cfg: Keithley2400With2182AConfig,
        *,
        force: bool = False,
    ) -> None:
        if not force and self._common_config_key == cfg:
            return
        source = self._require_source()
        voltmeter = self._require_voltmeter()
        terminal_scpi = "FRON" if cfg.terminals == "front" else "REAR"
        self._prepared_voltage_list_key = None

        self._try_clear(source)
        self._try_clear(voltmeter)
        self._write(source, "*CLS")
        self._write(voltmeter, "*CLS")
        self._try_write(voltmeter, "INIT:CONT OFF")
        self._try_write(source, ":ABOR")
        self._try_write(voltmeter, "ABOR")
        self._try_write(source, ":TRIG:CLE")
        self._try_write(source, f":ROUT:TERM {terminal_scpi}")

        self._write(source, ":ARM:SOUR IMM")
        self._write(source, ":ARM:COUN 1")
        self._write(source, ":ARM:DIR ACC")
        self._write(source, ":ARM:OUTP NONE")
        self._write(source, ":TRIG:SOUR IMM")
        self._write(source, ":TRIG:COUN 1")
        self._write(source, ":TRIG:DIR ACC")
        self._write(source, ":TRIG:INP NONE")
        self._write(source, ":TRIG:OUTP NONE")
        self._write(source, ":TRIG:DEL 0")

        self._write(voltmeter, "CONF:VOLT")
        self._write(voltmeter, "SENS:CHAN 1")
        self._write(voltmeter, f"SENS:VOLT:RANG {cfg.voltmeter_range_v:.12g}")
        self._write(voltmeter, f"SENS:VOLT:NPLC {cfg.nplc:.12g}")
        self._write(voltmeter, "TRIG:SOUR IMM")
        self._write(voltmeter, "TRIG:COUN 1")
        self._write(voltmeter, "SAMP:COUN 1")
        self._write(voltmeter, "TRIG:DEL 0")
        self._try_write(voltmeter, "TRIG:DEL:AUTO OFF")
        self._try_write(voltmeter, "SENS:VOLT:DFIL:STAT OFF")
        self._try_write(voltmeter, "FORM:ELEM READ")
        self._try_write(voltmeter, "TRAC:CLE")

        self._try_write(source, ":SENS:FUNC:CONC OFF")
        self._write(source, ':SENS:FUNC "CURR:DC"')
        self._write(source, ":SOUR:FUNC VOLT")
        self._write(source, ":SOUR:VOLT:MODE FIX")
        self._write(source, f":SOUR:VOLT:RANG {cfg.source_voltage_range_v:.12g}")
        self._write(source, f":SENS:CURR:RANG {cfg.current_range_a:.12g}")
        self._write(source, f":SENS:CURR:PROT {cfg.compliance_current_a:.12g}")
        self._write(source, f":SENS:CURR:NPLC {cfg.nplc:.12g}")
        self._try_write(source, ":FORM:ELEM VOLT,CURR")
        self._write(source, ":SOUR:VOLT 0")
        self._write(source, ":OUTP OFF")
        self._raise_scpi_errors(source, "2400 source", "configuration")
        self._raise_scpi_errors(voltmeter, "2182A voltmeter", "configuration")
        self._common_config_key = cfg

    def _measure_voltage_list_software(
        self,
        source_voltages: Sequence[float],
        cfg: Keithley2400With2182AConfig,
    ) -> list[VoltageListReading]:
        self._configure_common(cfg, force=True)
        source = self._require_source()
        voltmeter = self._require_voltmeter()
        readings: list[VoltageListReading] = []
        try:
            self._write(source, ":OUTP ON")
            for voltage in source_voltages:
                self._write(source, f":SOUR:VOLT {voltage:.12g}")
                if cfg.trigger_delay_s > 0:
                    time.sleep(cfg.trigger_delay_s)
                self._write(source, "INIT")
                voltage_reading = float(self._query(voltmeter, "READ?"))
                source_values = _parse_float_list(self._query(source, "FETC?"))
                if len(source_values) < 2:
                    raise RuntimeError("2400 FETC? did not return voltage,current")
                current = float(source_values[1])
                readings.append(
                    _voltage_list_reading(
                        source_voltage_v=float(voltage),
                        measured_voltage_v=voltage_reading,
                        current_a=current,
                        compliance_current_a=cfg.compliance_current_a,
                    )
                )
        finally:
            self._try_write(source, ":SOUR:VOLT 0")
            self._try_write(source, ":OUTP OFF")
        self._raise_scpi_errors(source, "2400 source", "software measurement")
        self._raise_scpi_errors(voltmeter, "2182A voltmeter", "software measurement")
        return readings

    def _measure_voltage_list_buffered_trigger_link(
        self,
        source_voltages: Sequence[float],
        cfg: Keithley2400With2182AConfig,
        *,
        after_measurement: Callable[[], object] | None = None,
    ) -> list[VoltageListReading]:
        source = self._require_source()
        voltmeter = self._require_voltmeter()
        points = len(source_voltages)
        if points < 1:
            raise ValueError("Source voltage list cannot be empty")
        if points > 2500:
            raise ValueError("2400 trace buffer accepts at most 2500 readings")
        if points > MAX_2182A_TRACE_POINTS:
            raise ValueError("2182A trace buffer accepts at most 1024 readings")
        requested_voltages = tuple(float(value) for value in source_voltages)
        if not self._prepared_voltage_list_matches(requested_voltages, cfg, points):
            self._prepare_voltage_list_buffered_trigger_link(
                requested_voltages,
                cfg,
                measure_points=points,
            )

        try:
            self._write(voltmeter, "INIT")
            time.sleep(0.1)
            self._write(source, ":OUTP ON")
            self._write(source, ":INIT")
            try:
                with _temporary_timeout(
                    source,
                    _buffered_measurement_timeout_ms(points, cfg),
                ):
                    self._query(source, "*OPC?")
            except Exception as exc:
                self._try_clear(source)
                self._try_write(source, ":ABOR")
                self._try_write(source, ":TRIG:CLE")
                raise RuntimeError(
                    "2400 buffered Trigger Link sequence did not complete "
                    f"for {points} source points. Check Trigger Link line "
                    "mapping and the 2182A external-trigger state."
                ) from exc
        finally:
            self._try_write(source, ":SOUR:VOLT 0")
            self._try_write(source, ":OUTP OFF")
            self._prepared_voltage_list_key = None

        if after_measurement is not None:
            after_measurement()

        source_trace, voltmeter_trace = self._read_trace_buffers_parallel()
        self._try_write(source, "TRAC:FEED:CONT NEV")
        self._try_write(voltmeter, "TRAC:FEED:CONT NEV")
        self._try_write(source, ":SOUR:VOLT:MODE FIX")
        self._raise_scpi_errors(source, "2400 source", "buffered measurement")
        self._raise_scpi_errors(voltmeter, "2182A voltmeter", "buffered measurement")

        if len(source_trace) < points * 2:
            raise RuntimeError(
                f"2400 buffer returned {len(source_trace)} values for {points} points"
            )
        if len(voltmeter_trace) < points:
            raise RuntimeError(
                f"2182A buffer returned {len(voltmeter_trace)} values for {points} points"
            )
        readings: list[VoltageListReading] = []
        for index, source_voltage in enumerate(source_voltages):
            current = float(source_trace[index * 2 + 1])
            measured_voltage = float(voltmeter_trace[index])
            readings.append(
                _voltage_list_reading(
                    source_voltage_v=float(source_voltage),
                    measured_voltage_v=measured_voltage,
                    current_a=current,
                    compliance_current_a=cfg.compliance_current_a,
                )
            )
        return readings

    def _prepare_voltage_list_buffered_trigger_link(
        self,
        source_voltages: Sequence[float],
        cfg: Keithley2400With2182AConfig,
        *,
        measure_points: int | None = None,
    ) -> None:
        source = self._require_source()
        voltmeter = self._require_voltmeter()
        source_list = tuple(float(value) for value in source_voltages)
        points = len(source_list) if measure_points is None else int(measure_points)
        if points < 1:
            raise ValueError("Source voltage list cannot be empty")
        if points > len(source_list):
            raise ValueError("Prepared source list is shorter than trigger count")
        if len(source_list) > MAX_2400_SOURCE_LIST_POINTS:
            raise ValueError("2400 source list accepts at most 2500 readings")
        if points > MAX_2182A_TRACE_POINTS:
            raise ValueError("2182A trace buffer accepts at most 1024 readings")

        self._configure_common(cfg)

        self._try_write(source, ":ABOR")
        self._try_write(voltmeter, "ABOR")
        self._try_write(source, "TRAC:FEED:CONT NEV")
        self._try_write(voltmeter, "TRAC:FEED:CONT NEV")
        self._try_write(source, "*CLS")
        self._try_write(voltmeter, "*CLS")
        self._try_write(source, ":TRIG:CLE")
        self._write(source, ":SOUR:VOLT:MODE LIST")
        if not self._source_list_covers(source_list):
            self._write_source_voltage_list(source_list)
            self._source_list_cache = source_list
        self._write(source, f":TRIG:COUN {points}")
        self._write(source, ":TRIG:SOUR TLIN")
        self._write(source, ":TRIG:DIR SOUR")
        self._write(source, ":TRIG:INP SOUR")
        self._write(source, ":TRIG:ILIN 1")
        self._write(source, ":TRIG:OLIN 2")
        self._write(source, ":TRIG:OUTP SOUR")
        self._write(source, "TRIG:DEL 0")
        self._write(source, ":SOUR:DEL 0")
        self._write(source, "TRAC:CLE")
        self._write(source, f"TRAC:POIN {points}")
        self._write(source, "TRAC:FEED SENS")
        self._raise_scpi_errors(source, "2400 source", "source list setup")

        self._write(voltmeter, "TRIG:SOUR EXT")
        self._write(voltmeter, "TRIG:DEL 0")
        self._write(voltmeter, f"TRIG:COUN {points}")
        self._write(voltmeter, "SAMP:COUN 1")
        self._write(voltmeter, "TRAC:CLE")
        self._write(voltmeter, f"TRAC:POIN {points}")
        self._write(voltmeter, "TRAC:FEED SENS")
        self._raise_scpi_errors(voltmeter, "2182A voltmeter", "buffer setup")

        self._write(voltmeter, "TRAC:FEED:CONT NEXT")
        self._write(source, "TRAC:FEED:CONT NEXT")
        self._prepared_source_voltages = source_list
        self._prepared_voltage_list_key = (
            cfg,
            source_list[:points],
            points,
        )

    def _prepared_voltage_list_matches(
        self,
        requested_voltages: tuple[float, ...],
        cfg: Keithley2400With2182AConfig,
        points: int,
    ) -> bool:
        return self._prepared_voltage_list_key == (
            cfg,
            requested_voltages[:points],
            points,
        )

    def _source_list_covers(self, requested_voltages: tuple[float, ...]) -> bool:
        cached = self._source_list_cache
        return (
            len(cached) >= len(requested_voltages)
            and cached[: len(requested_voltages)] == requested_voltages
        )

    def _read_trace_buffers_parallel(self) -> tuple[list[float], list[float]]:
        source = self._require_source()
        voltmeter = self._require_voltmeter()
        with ThreadPoolExecutor(max_workers=2) as executor:
            source_future = executor.submit(
                lambda: _parse_float_list(self._query(source, "TRAC:DATA?"))
            )
            voltmeter_future = executor.submit(
                lambda: _parse_float_list(self._query(voltmeter, "TRAC:DATA?"))
            )
            return source_future.result(), voltmeter_future.result()

    def _write_source_voltage_list(self, values: Sequence[float]) -> None:
        source = self._require_source()
        values = [float(value) for value in values]
        if not values:
            raise ValueError("Source voltage list cannot be empty")
        if len(values) > MAX_2400_SOURCE_LIST_POINTS:
            raise ValueError(
                "2400 source list accepts at most "
                f"{MAX_2400_SOURCE_LIST_POINTS} points"
            )
        for offset in range(0, len(values), MAX_2400_SOURCE_LIST_POINTS_PER_COMMAND):
            chunk = values[offset : offset + MAX_2400_SOURCE_LIST_POINTS_PER_COMMAND]
            command_name = (
                ":SOUR:LIST:VOLT"
                if offset == 0
                else ":SOUR:LIST:VOLT:APPend"
            )
            command = (
                f"{command_name} "
                + ",".join(f"{value:.12g}" for value in chunk)
            )
            try:
                self._write(source, command)
            except Exception as exc:
                details = "; ".join(
                    self._read_scpi_errors(
                        source,
                        "2400 source",
                        "source list setup",
                    )
                )
                suffix = f" SCPI errors: {details}" if details else ""
                raise RuntimeError(
                    "2400 source-list write failed "
                    f"({len(values)} points total, chunk offset {offset}, "
                    f"{len(chunk)} points, {len(command)} characters)."
                    f"{suffix}"
                ) from exc
            self._raise_scpi_errors(source, "2400 source", "source list setup")
    def _require_source(self):
        if self._source is None:
            raise RuntimeError("Keithley 2400 source is not open")
        return self._source

    def _require_voltmeter(self):
        if self._voltmeter is None:
            raise RuntimeError("Keithley 2182A voltmeter is not open")
        return self._voltmeter

    @staticmethod
    def _write(handle, command: str) -> None:
        handle.write(command)

    @staticmethod
    def _query(handle, query: str) -> str:
        if hasattr(handle, "query"):
            return str(handle.query(query)).strip()
        return str(handle.ask(query)).strip()

    def _try_write(self, handle, command: str) -> None:
        try:
            self._write(handle, command)
        except Exception:
            logger.debug("Keithley command failed: %s", command, exc_info=True)

    def _try_clear(self, handle) -> None:
        clearer = getattr(handle, "clear", None)
        if not callable(clearer):
            return
        try:
            clearer()
        except Exception:
            logger.debug("Keithley device clear failed", exc_info=True)

    def _safe_query(self, handle, query: str) -> str:
        if handle is None:
            return ""
        try:
            return self._query(handle, query).strip()
        except Exception:
            logger.debug("Keithley query failed: %s", query, exc_info=True)
            return ""

    def _trace_status(
        self,
        handle,
        *,
        supports_actual_points: bool,
    ) -> TraceBufferStatus:
        points = _optional_int(self._safe_query(handle, "TRAC:POIN?"))
        actual_points = (
            _optional_int(self._safe_query(handle, "TRAC:POIN:ACT?"))
            if supports_actual_points
            else None
        )
        free_response = self._safe_query(handle, "TRAC:FREE?")
        free_bytes, reserved_bytes = _optional_int_pair(free_response)
        feed = self._safe_query(handle, "TRAC:FEED?") or None
        control = self._safe_query(handle, "TRAC:FEED:CONT?") or None
        return TraceBufferStatus(
            points=points,
            actual_points=actual_points,
            free_bytes=free_bytes,
            reserved_bytes=reserved_bytes,
            feed=feed,
            control=control,
        )

    def _raise_scpi_errors(self, handle, label: str, context: str) -> None:
        errors = self._read_scpi_errors(handle, label, context)
        if errors:
            details = "; ".join(errors)
            raise RuntimeError(f"{label} SCPI error after {context}: {details}")

    def _read_scpi_errors(self, handle, label: str, context: str) -> list[str]:
        errors: list[str] = []
        for _index in range(8):
            response = self._safe_query(handle, "SYST:ERR?")
            if not response:
                break
            code = _scpi_error_code(response)
            if code == 0:
                break
            errors.append(response)
        return errors


@contextmanager
def _temporary_timeout(handle, timeout_ms: int) -> Iterator[None]:
    previous_timeout = getattr(handle, "timeout", None)
    if previous_timeout is None:
        yield
        return
    handle.timeout = int(timeout_ms)
    try:
        yield
    finally:
        handle.timeout = previous_timeout


def _buffered_measurement_timeout_ms(
    points: int,
    cfg: Keithley2400With2182AConfig,
) -> int:
    point_count = max(1, int(points))
    nplc = max(0.01, _finite_float(cfg.nplc, 1.0))
    trigger_delay_s = max(0.0, _finite_float(cfg.trigger_delay_s, 0.0))
    timeout_s = BUFFERED_MEASUREMENT_BASE_TIMEOUT_S + point_count * (
        BUFFERED_MEASUREMENT_POINT_OVERHEAD_S
        + BUFFERED_MEASUREMENT_NPLC_POINT_S * nplc
        + trigger_delay_s
    )
    return max(
        DEFAULT_VISA_TIMEOUT_MS,
        min(
            MAX_BUFFERED_MEASUREMENT_TIMEOUT_MS,
            int(math.ceil(timeout_s * 1000.0)),
        ),
    )


def _differential_reading(
    negative: PolarityReading,
    positive: PolarityReading,
    compliance_current_a: float,
) -> DifferentialReading:
    resistance = _resistance_from_two_points(
        (negative.measured_voltage_v, positive.measured_voltage_v),
        (negative.current_a, positive.current_a),
    )
    threshold = abs(compliance_current_a) * 0.99
    compliance_hit = (
        math.isfinite(threshold)
        and threshold > 0.0
        and (
            abs(negative.current_a) >= threshold
            or abs(positive.current_a) >= threshold
        )
    )
    return DifferentialReading(
        negative=negative,
        positive=positive,
        differential_resistance_ohm=resistance,
        compliance_hit=compliance_hit,
    )


def _differential_readings_from_voltage_list(
    readings: Sequence[VoltageListReading],
    compliance_current_a: float,
) -> list[DifferentialReading]:
    if len(readings) % 2:
        raise ValueError("Differential readings require an even number of points")
    differential_readings: list[DifferentialReading] = []
    for index in range(0, len(readings), 2):
        negative = _polarity_reading_from_voltage_list(
            readings[index],
            polarity="negative",
        )
        positive = _polarity_reading_from_voltage_list(
            readings[index + 1],
            polarity="positive",
        )
        differential_readings.append(
            _differential_reading(negative, positive, compliance_current_a)
        )
    return differential_readings


def _polarity_reading_from_voltage_list(
    reading: VoltageListReading,
    *,
    polarity: str,
) -> PolarityReading:
    return PolarityReading(
        polarity=polarity,
        source_voltage_v=reading.source_voltage_v,
        measured_voltage_v=reading.measured_voltage_v,
        current_a=reading.current_a,
        resistance_ohm=reading.resistance_ohm,
    )


def _voltage_list_reading(
    *,
    source_voltage_v: float,
    measured_voltage_v: float,
    current_a: float,
    compliance_current_a: float,
) -> VoltageListReading:
    threshold = abs(compliance_current_a) * 0.99
    compliance_hit = (
        math.isfinite(threshold)
        and threshold > 0.0
        and abs(current_a) >= threshold
    )
    return VoltageListReading(
        source_voltage_v=source_voltage_v,
        measured_voltage_v=measured_voltage_v,
        current_a=current_a,
        resistance_ohm=_resistance_from_voltage_current(measured_voltage_v, current_a),
        compliance_hit=compliance_hit,
    )


def _resistance_from_two_points(
    measured_voltage: Sequence[float],
    measured_current: Sequence[float],
) -> float:
    if len(measured_voltage) != 2 or len(measured_current) != 2:
        raise ValueError("Differential resistance needs exactly two points")
    delta_v = float(measured_voltage[1]) - float(measured_voltage[0])
    delta_i = float(measured_current[1]) - float(measured_current[0])
    if delta_i == 0.0:
        return math.inf
    return float(delta_v / delta_i)


def _resistance_from_voltage_current(voltage: float, current: float) -> float:
    if current == 0.0:
        return math.inf
    return float(voltage / current)


def _parse_float_list(response: str) -> list[float]:
    values: list[float] = []
    for token in re.split(r"[\s,]+", str(response).strip()):
        if not token:
            continue
        values.append(float(token))
    return values


def _optional_int(response: str) -> int | None:
    try:
        return int(float(str(response).strip()))
    except (TypeError, ValueError):
        return None


def _optional_int_pair(response: str) -> tuple[int | None, int | None]:
    values = [item.strip() for item in str(response).split(",", 1)]
    if len(values) != 2:
        return None, None
    return _optional_int(values[0]), _optional_int(values[1])


def _scpi_error_code(response: str) -> int | None:
    match = re.match(r"\s*([+-]?\d+)", str(response))
    if match is None:
        return None
    try:
        return int(match.group(1))
    except ValueError:
        return None


def _positive_finite(value: object, default: float) -> float:
    value_float = abs(_finite_float(value, default))
    return value_float if value_float > 0.0 else float(default)


def _optional_positive_float(value: object) -> float | None:
    try:
        result = float(value)
    except (TypeError, ValueError):
        return None
    if not math.isfinite(result) or result <= 0.0:
        return None
    return result


def _finite_float(value: object, default: float) -> float:
    try:
        result = float(value)
    except (TypeError, ValueError):
        return float(default)
    if not math.isfinite(result):
        return float(default)
    return result


def _close_handle(handle) -> None:
    try:
        handle.close()
    except Exception:
        logger.debug("Failed to close handle", exc_info=True)
