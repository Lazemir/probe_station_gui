"""Compare Keithley 2400 + 2182A resistance precision across NPLC values.

The script measures one Josephson-contact resistance sample as a two-polarity
cycle: source -V, read voltage/current, source +V, read voltage/current, then
fit the differential resistance from the two points. For each NPLC it collects
samples for the same measurement-time budget and reports the estimated
precision of the mean as std / sqrt(N).
"""

from __future__ import annotations

import argparse
import csv
import datetime as dt
import math
import random
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable


REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))


DEFAULT_NPLC_START = 0.01
DEFAULT_NPLC_STOP = 10.0
DEFAULT_NPLC_COUNT = 20
DEFAULT_DURATION_S = 20.0


@dataclass(frozen=True)
class SweepConfig:
    source_resource: str
    voltmeter_resource: str
    timeout_ms: int
    measurement_voltage_v: float
    source_voltage_range_v: float
    voltmeter_range_v: float
    current_range_a: float
    compliance_current_a: float
    trigger_delay_s: float
    terminals: str
    nplc_values: tuple[float, ...]
    duration_s: float
    rounds: int
    order: str
    warmup_samples: int
    output_dir: Path
    label: str


@dataclass(frozen=True)
class SummaryRow:
    nplc: float
    samples: int
    elapsed_s: float
    wall_elapsed_s: float
    mean_ohm: float
    std_ohm: float
    rms_about_mean_ohm: float
    sem_ohm: float
    sample_rate_hz: float
    negative_mean_ohm: float
    positive_mean_ohm: float
    compliance_hits: int


def parse_nplc_values(raw_values: str | None, logspace: str | None) -> tuple[float, ...]:
    if raw_values:
        values = [_positive_float(part.strip(), "NPLC") for part in raw_values.split(",")]
    else:
        start = DEFAULT_NPLC_START
        stop = DEFAULT_NPLC_STOP
        count = DEFAULT_NPLC_COUNT
        if logspace:
            parts = [part.strip() for part in logspace.split(",")]
            if len(parts) != 3:
                raise argparse.ArgumentTypeError(
                    "--nplc-logspace must be START,STOP,COUNT"
                )
            start = _positive_float(parts[0], "NPLC start")
            stop = _positive_float(parts[1], "NPLC stop")
            count = int(_positive_float(parts[2], "NPLC count"))
        if count < 2:
            values = [start]
        else:
            log_start = math.log10(start)
            log_stop = math.log10(stop)
            values = [
                10 ** (log_start + (log_stop - log_start) * index / (count - 1))
                for index in range(count)
            ]
    cleaned = []
    for value in values:
        if not math.isfinite(value) or value <= 0:
            raise argparse.ArgumentTypeError(f"NPLC must be positive, got {value!r}")
        cleaned.append(max(0.01, min(50.0, float(value))))
    return tuple(cleaned)


def finite_stats(values: Iterable[float]) -> tuple[int, float, float, float, float]:
    finite = [float(value) for value in values if math.isfinite(float(value))]
    count = len(finite)
    if count == 0:
        return 0, math.nan, math.nan, math.nan, math.nan
    mean = math.fsum(finite) / count
    if count == 1:
        return count, mean, math.nan, 0.0, math.nan
    square_sum = math.fsum((value - mean) ** 2 for value in finite)
    sample_std = math.sqrt(square_sum / (count - 1))
    rms_about_mean = math.sqrt(square_sum / count)
    sem = sample_std / math.sqrt(count)
    return count, mean, sample_std, rms_about_mean, sem


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Sweep Keithley NPLC and compare same-time resistance precision "
            "using two-polarity measurements."
        )
    )
    parser.add_argument(
        "--source",
        dest="source_resource",
        help="Keithley 2400 VISA resource. Defaults to saved GUI settings.",
    )
    parser.add_argument(
        "--voltmeter",
        dest="voltmeter_resource",
        help="Keithley 2182A VISA resource. Defaults to saved GUI settings.",
    )
    parser.add_argument("--timeout-ms", type=int, default=30000)
    parser.add_argument("--measurement-voltage-v", type=float, default=0.03)
    parser.add_argument("--source-voltage-range-v", type=float, default=0.21)
    parser.add_argument(
        "--voltmeter-range-v",
        type=float,
        default=0.1,
        help="Fixed 2182A voltage range. Use 0 to leave the instrument setting unchanged.",
    )
    parser.add_argument(
        "--current-range-a",
        type=float,
        default=10e-6,
        help="Fixed 2400 current measurement range. Use 0 to leave it unchanged.",
    )
    parser.add_argument("--compliance-current-a", type=float, default=500e-6)
    parser.add_argument("--trigger-delay-s", type=float, default=0.01)
    parser.add_argument("--terminals", choices=("front", "rear"), default="rear")
    parser.add_argument(
        "--nplc-values",
        help="Comma-separated NPLC values, for example 0.01,0.03,0.1,0.3,1,3,10.",
    )
    parser.add_argument(
        "--nplc-logspace",
        default=f"{DEFAULT_NPLC_START},{DEFAULT_NPLC_STOP},{DEFAULT_NPLC_COUNT}",
        help="START,STOP,COUNT used when --nplc-values is omitted.",
    )
    parser.add_argument(
        "--duration-s",
        type=float,
        default=DEFAULT_DURATION_S,
        help="Total measurement seconds per NPLC, excluding configure and warmup.",
    )
    parser.add_argument(
        "--rounds",
        type=int,
        default=1,
        help="Split each NPLC time budget into interleaved rounds to reduce drift bias.",
    )
    parser.add_argument(
        "--order",
        choices=("ascending", "descending", "random"),
        default="ascending",
        help="NPLC order within each round.",
    )
    parser.add_argument(
        "--warmup-samples",
        type=int,
        default=1,
        help="Discard this many samples after each NPLC reconfiguration.",
    )
    parser.add_argument(
        "--output-dir",
        type=Path,
        default=Path("measurements"),
        help="Directory for raw and summary CSV files.",
    )
    parser.add_argument(
        "--label",
        default="keithley_nplc_sweep",
        help="Filename label for output CSV files.",
    )
    parser.add_argument(
        "--list-resources",
        action="store_true",
        help="List VISA resources and exit.",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)

    if args.list_resources:
        list_visa_resources()
        return 0

    try:
        config = make_config(args)
    except argparse.ArgumentTypeError as exc:
        parser.error(str(exc))
    return run_sweep(config)


def make_config(args: argparse.Namespace) -> SweepConfig:
    source_resource = args.source_resource
    voltmeter_resource = args.voltmeter_resource
    if not source_resource or not voltmeter_resource:
        source_resource, voltmeter_resource = load_saved_keithley_resources(
            source_resource,
            voltmeter_resource,
        )

    duration_s = _positive_float(args.duration_s, "duration")
    rounds = int(args.rounds)
    if rounds < 1:
        raise SystemExit("--rounds must be at least 1")
    warmup_samples = int(args.warmup_samples)
    if warmup_samples < 0:
        raise SystemExit("--warmup-samples must be non-negative")

    return SweepConfig(
        source_resource=str(source_resource),
        voltmeter_resource=str(voltmeter_resource),
        timeout_ms=max(1000, int(args.timeout_ms)),
        measurement_voltage_v=_positive_float(
            args.measurement_voltage_v,
            "measurement voltage",
        ),
        source_voltage_range_v=_positive_float(
            args.source_voltage_range_v,
            "source voltage range",
        ),
        voltmeter_range_v=_nonnegative_float(
            args.voltmeter_range_v,
            "voltmeter range",
        ),
        current_range_a=_nonnegative_float(
            args.current_range_a,
            "current range",
        ),
        compliance_current_a=_positive_float(
            args.compliance_current_a,
            "compliance current",
        ),
        trigger_delay_s=max(0.0, float(args.trigger_delay_s)),
        terminals=str(args.terminals),
        nplc_values=parse_nplc_values(args.nplc_values, args.nplc_logspace),
        duration_s=duration_s,
        rounds=rounds,
        order=str(args.order),
        warmup_samples=warmup_samples,
        output_dir=Path(args.output_dir),
        label=safe_label(str(args.label)),
    )


def list_visa_resources() -> None:
    try:
        import pyvisa
    except ImportError as exc:
        raise SystemExit("pyvisa is not installed in this environment.") from exc

    manager = pyvisa.ResourceManager()
    try:
        resources = manager.list_resources()
    finally:
        manager.close()
    for resource in resources:
        print(resource)


def load_saved_keithley_resources(
    source_resource: str | None,
    voltmeter_resource: str | None,
) -> tuple[str, str]:
    from probe_station_gui.settings_manager import SettingsManager

    settings = SettingsManager().needle_calibration_configuration()
    return (
        source_resource or settings.keithley_source_resource,
        voltmeter_resource or settings.keithley_voltmeter_resource,
    )


def run_sweep(config: SweepConfig) -> int:
    from probe_station_gui.lcr_meter import _Keithley2400With2182ASession

    timestamp = dt.datetime.now().strftime("%Y%m%d-%H%M%S")
    config.output_dir.mkdir(parents=True, exist_ok=True)
    raw_path = config.output_dir / f"{config.label}_{timestamp}_raw.csv"
    summary_path = config.output_dir / f"{config.label}_{timestamp}_summary.csv"

    print(f"Source: {config.source_resource}")
    print(f"Voltmeter: {config.voltmeter_resource}")
    print(f"NPLC values: {', '.join(f'{value:g}' for value in config.nplc_values)}")
    print(f"Measurement budget: {config.duration_s:g} s per NPLC")
    print(f"Raw CSV: {raw_path}")
    print(f"Summary CSV: {summary_path}")

    session = _Keithley2400With2182ASession(
        config.source_resource,
        config.voltmeter_resource,
        config.timeout_ms,
    )
    measurements_by_nplc: dict[float, list[dict[str, object]]] = {
        value: [] for value in config.nplc_values
    }
    started_by_nplc: dict[float, list[float]] = {value: [] for value in config.nplc_values}
    ended_by_nplc: dict[float, list[float]] = {value: [] for value in config.nplc_values}
    wall_started_by_nplc: dict[float, list[float]] = {
        value: [] for value in config.nplc_values
    }
    wall_ended_by_nplc: dict[float, list[float]] = {value: [] for value in config.nplc_values}

    try:
        with raw_path.open("w", newline="", encoding="utf-8") as raw_file:
            raw_writer = csv.DictWriter(raw_file, fieldnames=raw_fieldnames())
            raw_writer.writeheader()
            window_s = config.duration_s / config.rounds
            for round_index in range(config.rounds):
                values = ordered_nplc_values(config.nplc_values, config.order)
                print(f"\nRound {round_index + 1}/{config.rounds}")
                for nplc in values:
                    print(f"  NPLC {nplc:g}: configuring", flush=True)
                    configure_keithley_for_sweep(session, config, nplc)
                    for _ in range(config.warmup_samples):
                        session.read_route_measurement(trigger=True)
                    sample_count = collect_for_window(
                        session=session,
                        nplc=nplc,
                        round_index=round_index,
                        window_s=window_s,
                        raw_writer=raw_writer,
                        measurements=measurements_by_nplc[nplc],
                        started_chunks=started_by_nplc[nplc],
                        ended_chunks=ended_by_nplc[nplc],
                        wall_started_chunks=wall_started_by_nplc[nplc],
                        wall_ended_chunks=wall_ended_by_nplc[nplc],
                    )
                    print(f"  NPLC {nplc:g}: {sample_count} samples", flush=True)
    finally:
        session.close()

    summaries = [
        summarize_nplc(
            nplc,
            measurements_by_nplc[nplc],
            started_by_nplc[nplc],
            ended_by_nplc[nplc],
            wall_started_by_nplc[nplc],
            wall_ended_by_nplc[nplc],
        )
        for nplc in config.nplc_values
    ]
    write_summary(summary_path, summaries)
    print_summary(summaries)
    return 0


def configure_keithley_for_sweep(session, config: SweepConfig, nplc: float) -> None:
    """Configure only the SCPI subset needed by the NPLC sweep.

    The GUI route driver uses a few best-effort compatibility commands for
    different Keithley firmware variants. Some 2400/2182A units report those as
    -113 Undefined header on the front panel even though the measurement works.
    This sweep avoids those optional commands so the instrument error queue
    stays quiet while we repeatedly reconfigure NPLC.
    """

    source = session._require_source()
    voltmeter = session._require_voltmeter()
    measurement_voltage_v = abs(float(config.measurement_voltage_v))
    source_voltage_range_v = max(
        abs(float(config.source_voltage_range_v)),
        measurement_voltage_v,
    )
    compliance_current_a = abs(float(config.compliance_current_a))
    nplc = max(0.01, min(50.0, float(nplc)))
    terminal_scpi = "FRON" if config.terminals == "front" else "REAR"

    session._measurement_voltage_v = measurement_voltage_v
    session._trigger_delay_s = max(0.0, float(config.trigger_delay_s))
    session._compliance_current_a = compliance_current_a

    session._write(source, "*CLS")
    session._write(voltmeter, "*CLS")
    session._try_write(source, ":ABOR")
    session._try_write(voltmeter, "ABOR")
    session._try_write(voltmeter, "INIT:CONT OFF")

    session._write(voltmeter, "CONF:VOLT")
    session._try_write(voltmeter, "SENS:CHAN 1")
    if config.voltmeter_range_v > 0:
        session._try_write(voltmeter, f"SENS:VOLT:RANG {config.voltmeter_range_v:.12g}")
    session._write(voltmeter, f"SENS:VOLT:NPLC {nplc:.12g}")
    session._try_write(voltmeter, "TRIG:SOUR IMM")
    session._try_write(voltmeter, "TRIG:COUN 1")
    session._try_write(voltmeter, "SAMP:COUN 1")

    session._try_write(source, f":ROUT:TERM {terminal_scpi}")
    session._try_write(source, ":TRIG:SOUR IMM")
    session._try_write(source, ":TRIG:COUN 1")
    session._write(source, ":SOUR:FUNC VOLT")
    session._try_write(source, ":SOUR:VOLT:MODE FIX")
    session._write(source, f":SOUR:VOLT:RANG {source_voltage_range_v:.12g}")
    if config.current_range_a > 0:
        session._write(source, f":SENS:CURR:RANG {config.current_range_a:.12g}")
    session._write(source, f":SENS:CURR:PROT {compliance_current_a:.12g}")
    session._write(source, f":SENS:CURR:NPLC {nplc:.12g}")
    session._try_write(source, ":FORM:ELEM VOLT,CURR")
    session._write(source, ":SOUR:VOLT 0")
    session._write(source, ":OUTP ON")

    scpi_errors = []
    scpi_errors.extend(session._log_scpi_errors(source, "2400 source", "sweep config"))
    scpi_errors.extend(
        session._log_scpi_errors(voltmeter, "2182A voltmeter", "sweep config")
    )
    if scpi_errors:
        from probe_station_gui.lcr_meter import LCRMeterError

        raise LCRMeterError(
            "Keithley instrument reported SCPI error after sweep config: "
            + "; ".join(scpi_errors)
        )


def collect_for_window(
    *,
    session,
    nplc: float,
    round_index: int,
    window_s: float,
    raw_writer: csv.DictWriter,
    measurements: list[dict[str, object]],
    started_chunks: list[float],
    ended_chunks: list[float],
    wall_started_chunks: list[float],
    wall_ended_chunks: list[float],
) -> int:
    wall_started = time.perf_counter()
    deadline = wall_started + window_s
    sample_count = 0
    chunk_first_started = math.nan
    chunk_last_ended = math.nan
    while sample_count == 0 or time.perf_counter() < deadline:
        sample_started = time.perf_counter()
        measurement = dict(session.read_route_measurement(trigger=True))
        sample_ended = time.perf_counter()
        if math.isnan(chunk_first_started):
            chunk_first_started = sample_started
        chunk_last_ended = sample_ended
        row = flatten_measurement(
            measurement,
            nplc=nplc,
            round_index=round_index,
            sample_index=sample_count,
            sample_started_s=sample_started,
            sample_ended_s=sample_ended,
        )
        raw_writer.writerow(row)
        measurements.append(row)
        sample_count += 1
    wall_ended = time.perf_counter()
    if not math.isnan(chunk_first_started):
        started_chunks.append(chunk_first_started)
        ended_chunks.append(chunk_last_ended)
    wall_started_chunks.append(wall_started)
    wall_ended_chunks.append(wall_ended)
    return sample_count


def summarize_nplc(
    nplc: float,
    rows: list[dict[str, object]],
    started_chunks: list[float],
    ended_chunks: list[float],
    wall_started_chunks: list[float],
    wall_ended_chunks: list[float],
) -> SummaryRow:
    count, mean, std, rms, sem = finite_stats(
        float(row["differential_resistance_ohm"]) for row in rows
    )
    _, negative_mean, _, _, _ = finite_stats(
        float(row["negative_resistance_ohm"]) for row in rows
    )
    _, positive_mean, _, _, _ = finite_stats(
        float(row["positive_resistance_ohm"]) for row in rows
    )
    elapsed_s = math.fsum(
        max(0.0, ended - started)
        for started, ended in zip(started_chunks, ended_chunks)
    )
    wall_elapsed_s = math.fsum(
        max(0.0, ended - started)
        for started, ended in zip(wall_started_chunks, wall_ended_chunks)
    )
    compliance_hits = sum(1 for row in rows if row["compliance_hit"])
    sample_rate = count / elapsed_s if elapsed_s > 0 else math.nan
    return SummaryRow(
        nplc=nplc,
        samples=count,
        elapsed_s=elapsed_s,
        wall_elapsed_s=wall_elapsed_s,
        mean_ohm=mean,
        std_ohm=std,
        rms_about_mean_ohm=rms,
        sem_ohm=sem,
        sample_rate_hz=sample_rate,
        negative_mean_ohm=negative_mean,
        positive_mean_ohm=positive_mean,
        compliance_hits=compliance_hits,
    )


def write_summary(path: Path, summaries: Iterable[SummaryRow]) -> None:
    with path.open("w", newline="", encoding="utf-8") as summary_file:
        writer = csv.DictWriter(summary_file, fieldnames=summary_fieldnames())
        writer.writeheader()
        for summary in summaries:
            writer.writerow(summary_to_dict(summary))


def print_summary(summaries: list[SummaryRow]) -> None:
    print("\nSummary")
    print(
        "NPLC        N      elapsed_s      mean_ohm        std_ohm        sem_ohm"
    )
    for row in summaries:
        print(
            f"{row.nplc:<10.4g} {row.samples:<6d} {row.elapsed_s:<14.3f} "
            f"{format_float(row.mean_ohm):<15} {format_float(row.std_ohm):<15} "
            f"{format_float(row.sem_ohm):<15}"
        )
    ranked = [row for row in summaries if math.isfinite(row.sem_ohm)]
    ranked.sort(key=lambda row: row.sem_ohm)
    if ranked:
        best = ranked[0]
        print(
            "\nBest same-time mean precision: "
            f"NPLC={best.nplc:g}, SEM={best.sem_ohm:.6g} ohm, "
            f"N={best.samples}, elapsed={best.elapsed_s:.3f} s"
        )


def ordered_nplc_values(values: tuple[float, ...], order: str) -> list[float]:
    ordered = list(values)
    if order == "descending":
        ordered.reverse()
    elif order == "random":
        random.shuffle(ordered)
    return ordered


def flatten_measurement(
    measurement: dict[str, object],
    *,
    nplc: float,
    round_index: int,
    sample_index: int,
    sample_started_s: float,
    sample_ended_s: float,
) -> dict[str, object]:
    negative = _polarity_dict(measurement.get("negative"))
    positive = _polarity_dict(measurement.get("positive"))
    return {
        "nplc": nplc,
        "round": round_index + 1,
        "sample_index": sample_index,
        "sample_wall_time": dt.datetime.now().isoformat(timespec="milliseconds"),
        "sample_started_s": sample_started_s,
        "sample_ended_s": sample_ended_s,
        "sample_elapsed_s": sample_ended_s - sample_started_s,
        "differential_resistance_ohm": float(
            measurement["differential_resistance_ohm"]
        ),
        "compliance_hit": bool(measurement.get("compliance_hit", False)),
        "negative_source_voltage_v": negative["source_voltage_v"],
        "negative_measured_voltage_v": negative["measured_voltage_v"],
        "negative_current_a": negative["current_a"],
        "negative_resistance_ohm": negative["resistance_ohm"],
        "positive_source_voltage_v": positive["source_voltage_v"],
        "positive_measured_voltage_v": positive["measured_voltage_v"],
        "positive_current_a": positive["current_a"],
        "positive_resistance_ohm": positive["resistance_ohm"],
    }


def _polarity_dict(value: object) -> dict[str, float]:
    if not isinstance(value, dict):
        value = {}
    return {
        "source_voltage_v": _float_or_nan(value.get("source_voltage_v")),
        "measured_voltage_v": _float_or_nan(value.get("measured_voltage_v")),
        "current_a": _float_or_nan(value.get("current_a")),
        "resistance_ohm": _float_or_nan(value.get("resistance_ohm")),
    }


def raw_fieldnames() -> list[str]:
    return [
        "nplc",
        "round",
        "sample_index",
        "sample_wall_time",
        "sample_started_s",
        "sample_ended_s",
        "sample_elapsed_s",
        "differential_resistance_ohm",
        "compliance_hit",
        "negative_source_voltage_v",
        "negative_measured_voltage_v",
        "negative_current_a",
        "negative_resistance_ohm",
        "positive_source_voltage_v",
        "positive_measured_voltage_v",
        "positive_current_a",
        "positive_resistance_ohm",
    ]


def summary_fieldnames() -> list[str]:
    return [
        "nplc",
        "samples",
        "elapsed_s",
        "wall_elapsed_s",
        "mean_ohm",
        "std_ohm",
        "rms_about_mean_ohm",
        "sem_ohm",
        "sample_rate_hz",
        "negative_mean_ohm",
        "positive_mean_ohm",
        "compliance_hits",
    ]


def summary_to_dict(summary: SummaryRow) -> dict[str, object]:
    return {
        "nplc": summary.nplc,
        "samples": summary.samples,
        "elapsed_s": summary.elapsed_s,
        "wall_elapsed_s": summary.wall_elapsed_s,
        "mean_ohm": summary.mean_ohm,
        "std_ohm": summary.std_ohm,
        "rms_about_mean_ohm": summary.rms_about_mean_ohm,
        "sem_ohm": summary.sem_ohm,
        "sample_rate_hz": summary.sample_rate_hz,
        "negative_mean_ohm": summary.negative_mean_ohm,
        "positive_mean_ohm": summary.positive_mean_ohm,
        "compliance_hits": summary.compliance_hits,
    }


def safe_label(value: str) -> str:
    label = "".join(char if char.isalnum() or char in "._-" else "_" for char in value)
    return label.strip("._-") or "keithley_nplc_sweep"


def format_float(value: float) -> str:
    if not math.isfinite(value):
        return "nan"
    return f"{value:.6g}"


def _positive_float(value: object, name: str) -> float:
    try:
        result = float(value)
    except (TypeError, ValueError) as exc:
        raise argparse.ArgumentTypeError(f"{name} must be a number") from exc
    if not math.isfinite(result) or result <= 0:
        raise argparse.ArgumentTypeError(f"{name} must be positive")
    return result


def _nonnegative_float(value: object, name: str) -> float:
    try:
        result = float(value)
    except (TypeError, ValueError) as exc:
        raise argparse.ArgumentTypeError(f"{name} must be a number") from exc
    if not math.isfinite(result) or result < 0:
        raise argparse.ArgumentTypeError(f"{name} must be non-negative")
    return result


def _float_or_nan(value: object) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return math.nan


if __name__ == "__main__":
    raise SystemExit(main())
