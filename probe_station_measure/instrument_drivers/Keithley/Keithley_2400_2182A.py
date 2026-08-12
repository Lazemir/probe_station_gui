"""Composite Keithley 2400 + 2182A voltage-list measurements.

The fast path uses the documented 2182A voltmeter-complete Trigger Link
handshake: the 2400 advances the source-voltage list only after the 2182A has
finished storing the previous voltage reading.  Differential-resistance
readings are a small convenience layer over this list measurement.
"""

from __future__ import annotations

from collections.abc import Callable, Iterator, Mapping
from contextlib import contextmanager
import math
from dataclasses import dataclass, replace
from typing import Sequence

import numpy as np

from probe_station_measure.instrument_drivers.Keithley import (
    Keithley_2400_2182A_session as _session_owner,
)
from probe_station_measure.instrument_drivers.Keithley import (
    Keithley_2400_2182A_trigger_link as _trigger_link_owner,
)

from probe_station_measure.ohmmeter import (
    OHMMETER_RANGE_MANUAL,
    AbstractOhmmeter,
    OhmmeterRangeCapabilities,
    OhmmeterRangeDefaults,
    OhmmeterRangeRequest,
    normalize_range_mode,
    resolve_ohmmeter_ranges,
)


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
    if quality.polarity_sign_mismatch_count > criteria.max_polarity_sign_mismatch_count:
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
    """One logical ohmmeter made from a 2400 source-meter and optional 2182A."""

    backend_name = "probe-station-measure"

    def __init__(
        self,
        source_resource: str,
        voltmeter_resource: str | None = None,
        *,
        timeout_ms: int = DEFAULT_VISA_TIMEOUT_MS,
        resource_manager: object | None = None,
    ) -> None:
        self._config = Keithley2400With2182AConfig().normalized()
        self._session = _session_owner.KeithleySession(
            source_resource,
            voltmeter_resource,
            timeout_ms=timeout_ms,
            resource_manager=resource_manager,
        )
        self._trigger_link = _trigger_link_owner.TriggerLinkBatch(
            self._session,
            max_source_list_points=MAX_2400_SOURCE_LIST_POINTS,
            max_source_list_points_per_command=(
                MAX_2400_SOURCE_LIST_POINTS_PER_COMMAND
            ),
            max_trace_points=MAX_2182A_TRACE_POINTS,
            default_timeout_ms=DEFAULT_VISA_TIMEOUT_MS,
            max_timeout_ms=MAX_BUFFERED_MEASUREMENT_TIMEOUT_MS,
            base_timeout_s=BUFFERED_MEASUREMENT_BASE_TIMEOUT_S,
            point_overhead_s=BUFFERED_MEASUREMENT_POINT_OVERHEAD_S,
            nplc_point_s=BUFFERED_MEASUREMENT_NPLC_POINT_S,
        )

    def identify(self) -> str:
        return self._session.identify()

    def configure(self, config: Keithley2400With2182AConfig | None = None) -> None:
        self._config = (config or self._config).normalized()
        self._session.configure_common(self._config, force=True)

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
        **aliases: object,
    ) -> None:
        """Configure from route-measurement keyword names."""

        nested_ranges = aliases.get("ranges")
        if isinstance(nested_ranges, Mapping):
            aliases = {**aliases, **nested_ranges}

        keithley_measurement_voltage_v = _setting_float(
            aliases,
            keithley_measurement_voltage_v,
            "measurement_voltage_v",
            "voltage_v",
        )
        keithley_range_mode = str(
            _setting_value(
                aliases,
                keithley_range_mode,
                "range_mode",
                "mode",
            )
        )
        keithley_expected_resistance_ohm = _setting_optional_float(
            aliases,
            keithley_expected_resistance_ohm,
            "expected_resistance_ohm",
            "resistance_ohm",
        )
        keithley_minimum_resistance_ohm = _setting_optional_float(
            aliases,
            keithley_minimum_resistance_ohm,
            "minimum_resistance_ohm",
            "min_resistance_ohm",
            "resistance_floor_ohm",
        )
        keithley_maximum_current_a = _setting_optional_float(
            aliases,
            keithley_maximum_current_a,
            "maximum_current_a",
            "max_current_a",
            "current_limit_a",
        )
        keithley_voltage_range_v = _setting_optional_float(
            aliases,
            keithley_voltage_range_v,
            "voltage_range_v",
        )
        keithley_source_voltage_range_v = _setting_float(
            aliases,
            keithley_source_voltage_range_v,
            "source_voltage_range_v",
            "source_range_v",
        )
        keithley_voltmeter_range_v = _setting_float(
            aliases,
            keithley_voltmeter_range_v,
            "voltmeter_range_v",
            "meter_voltage_range_v",
            "nanovoltmeter_range_v",
        )
        if keithley_voltage_range_v is None:
            voltage_values = _setting_value(
                aliases,
                None,
                "voltages_v",
                "voltages",
                "source_voltages_v",
                "voltage_sweep_v",
            )
            max_voltage = _max_abs_voltage(voltage_values)
            if max_voltage is not None:
                keithley_voltage_range_v = max(
                    abs(float(keithley_measurement_voltage_v)),
                    max_voltage,
                )
        keithley_current_range_a = _setting_float(
            aliases,
            keithley_current_range_a,
            "current_range_a",
        )
        keithley_compliance_current_a = _setting_float(
            aliases,
            keithley_compliance_current_a,
            "compliance_current_a",
            "current_limit_a",
            "max_current_a",
        )
        keithley_range_voltage_headroom = _setting_float(
            aliases,
            keithley_range_voltage_headroom,
            "range_voltage_headroom",
            "voltage_headroom",
        )
        keithley_range_current_headroom = _setting_float(
            aliases,
            keithley_range_current_headroom,
            "range_current_headroom",
            "current_headroom",
        )
        keithley_nplc = _setting_float(aliases, keithley_nplc, "nplc")
        keithley_terminals = str(
            _setting_value(aliases, keithley_terminals, "terminals")
        )
        keithley_trigger_delay_s = _setting_float(
            aliases,
            keithley_trigger_delay_s,
            "trigger_delay_s",
            "delay_s",
        )
        keithley_use_buffer = _setting_bool(
            aliases,
            keithley_use_buffer,
            "use_buffer",
        )
        keithley_use_trigger_link = _setting_bool(
            aliases,
            keithley_use_trigger_link,
            "use_trigger_link",
        )

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

    @contextmanager
    def output(self, enabled: bool = True) -> Iterator["Keithley2400With2182A"]:
        """Keep the 2400 output relay in one state across several operations."""

        with self._session.output(
            self._config,
            bool(enabled),
            prime_source=self._session.prepared_key is None,
        ):
            yield self

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

        if (
            self._session._voltmeter is not None
            and cfg.use_buffer
            and cfg.use_trigger_link
            and len(source_voltages) > 1
        ):
            readings: list[VoltageListReading] = []
            chunk_size = max(1, int(cfg.max_buffer_points_per_chunk))
            for offset in range(0, len(source_voltages), chunk_size):
                readings.extend(
                    self._trigger_link.measure(
                        source_voltages[offset : offset + chunk_size],
                        cfg,
                        _voltage_list_reading,
                        after_measurement=(
                            after_measurement
                            if offset + chunk_size >= len(source_voltages)
                            else None
                        ),
                    )
                )
            return readings
        return self._session.measure_software(
            source_voltages,
            cfg,
            _voltage_list_reading,
        )

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

        if self._session._voltmeter is None:
            return
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
        self._trigger_link.prepare(
            source_voltages,
            cfg,
            measure_points=measurement_count * 2,
        )

    def abort(self) -> None:
        self._session.abort()

    def abort_measurement(self) -> None:
        self.abort()

    def trace_status(self) -> dict[str, TraceBufferStatus]:
        """Return trace-buffer state for diagnostics."""

        return self._session.trace_status(TraceBufferStatus)

    def visa_resource_roles(self) -> dict[str, dict[str, object]]:
        """Return station-owned VISA roles exposed by this logical meter."""

        return self._session.visa_resource_roles()

    def visa_handle_for_role(self, role: str):
        return self._session.visa_handle_for_role(role)

    def close(self) -> None:
        self._session.close()


class Keithley2400SourceMeter(Keithley2400With2182A):
    """Source-meter-only ohmmeter using the 2400 voltage/current readback."""

    def __init__(
        self,
        source_resource: str,
        *,
        timeout_ms: int = DEFAULT_VISA_TIMEOUT_MS,
        resource_manager: object | None = None,
    ) -> None:
        super().__init__(
            source_resource,
            None,
            timeout_ms=timeout_ms,
            resource_manager=resource_manager,
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
            abs(negative.current_a) >= threshold or abs(positive.current_a) >= threshold
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
        math.isfinite(threshold) and threshold > 0.0 and abs(current_a) >= threshold
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


def _setting_value(
    values: Mapping[str, object],
    default: object,
    *names: str,
) -> object:
    for name in names:
        if name in values and values[name] is not None:
            return values[name]
    return default


def _setting_float(
    values: Mapping[str, object],
    default: float,
    *names: str,
) -> float:
    return _finite_float(_setting_value(values, default, *names), default)


def _setting_optional_float(
    values: Mapping[str, object],
    default: float | None,
    *names: str,
) -> float | None:
    value = _setting_value(values, default, *names)
    if value is None:
        return default
    parsed = _optional_positive_float(value)
    return parsed if parsed is not None else default


def _setting_bool(
    values: Mapping[str, object],
    default: bool,
    *names: str,
) -> bool:
    value = _setting_value(values, default, *names)
    if isinstance(value, bool):
        return value
    text = str(value).strip().lower()
    if text in {"1", "true", "yes", "on"}:
        return True
    if text in {"0", "false", "no", "off"}:
        return False
    return bool(default if value is None else value)


def _max_abs_voltage(values: object) -> float | None:
    if not isinstance(values, Sequence) or isinstance(values, (str, bytes)):
        return None
    max_voltage = 0.0
    found = False
    for value in values:
        try:
            number = abs(float(value))
        except (TypeError, ValueError):
            continue
        if not math.isfinite(number):
            continue
        max_voltage = max(max_voltage, number)
        found = True
    return max_voltage if found else None


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
