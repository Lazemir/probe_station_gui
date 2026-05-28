"""Buffered Keithley 2400 + 2182A NPLC comparison with fixed sample count.

The run uses Trigger Link:
- 2400 outputs a voltage list: -V, +V, -V, +V, ...
- 2182A stores externally triggered voltage readings in its buffer.
- 2400 stores source voltage/current readings in its buffer.

For each NPLC, the script records the same number of differential pairs. It
queries instrument-side buffer statistics before fetching raw buffers, then
fetches the arrays to compute exact pairwise differential resistance.
"""

from __future__ import annotations

import argparse
import csv
import datetime as dt
import math
import statistics
import sys
import time
from dataclasses import dataclass
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))


@dataclass(frozen=True)
class BufferedConfig:
    source_resource: str
    voltmeter_resource: str
    nplc_values: tuple[float, ...]
    pairs: int
    warmup_pairs: int
    measurement_voltage_v: float
    source_voltage_range_v: float
    voltmeter_range_v: float
    current_range_a: float
    compliance_current_a: float
    timeout_ms: int
    output_dir: Path
    label: str
    trigger_mode: str
    source_delay_s: float


@dataclass(frozen=True)
class BufferedResult:
    nplc: float
    pairs: int
    elapsed_s: float
    stats_query_s: float
    raw_fetch_s: float
    source_values: list[float]
    voltmeter_values: list[float]
    source_mean_values: list[float]
    source_sdev_values: list[float]
    voltmeter_mean_values: list[float]
    voltmeter_sdev_values: list[float]
    post_errors_source: list[str]
    post_errors_voltmeter: list[str]


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Compare buffered Trigger Link readings at fixed pair count."
    )
    parser.add_argument("--source", default=None)
    parser.add_argument("--voltmeter", default=None)
    parser.add_argument("--nplc-values", default="1,2,3,5,10")
    parser.add_argument("--pairs", type=int, default=60)
    parser.add_argument("--warmup-pairs", type=int, default=4)
    parser.add_argument("--measurement-voltage-v", type=float, default=0.03)
    parser.add_argument("--source-voltage-range-v", type=float, default=0.21)
    parser.add_argument("--voltmeter-range-v", type=float, default=0.1)
    parser.add_argument("--current-range-a", type=float, default=10e-6)
    parser.add_argument("--compliance-current-a", type=float, default=10e-6)
    parser.add_argument("--timeout-ms", type=int, default=180000)
    parser.add_argument("--output-dir", type=Path, default=Path("measurements"))
    parser.add_argument("--label", default="buffered_nplc_compare")
    parser.add_argument(
        "--trigger-mode",
        choices=("vmc", "oneway"),
        default="vmc",
        help="vmc waits for 2182A VMC on Trigger Link; oneway only triggers 2182A.",
    )
    parser.add_argument("--source-delay-s", type=float, default=0.0)
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        config = make_config(args)
    except argparse.ArgumentTypeError as exc:
        parser.error(str(exc))
    run(config)
    return 0


def make_config(args: argparse.Namespace) -> BufferedConfig:
    source = args.source
    voltmeter = args.voltmeter
    if not source or not voltmeter:
        source, voltmeter = load_saved_keithley_resources(source, voltmeter)
    nplc_values = tuple(
        max(0.01, min(50.0, positive_float(part.strip(), "NPLC")))
        for part in str(args.nplc_values).split(",")
        if part.strip()
    )
    if not nplc_values:
        raise argparse.ArgumentTypeError("at least one NPLC value is required")
    pairs = int(args.pairs)
    if pairs < 2 or pairs > 512:
        raise argparse.ArgumentTypeError("--pairs must be between 2 and 512")
    warmup_pairs = int(args.warmup_pairs)
    if warmup_pairs < 0 or warmup_pairs > 64:
        raise argparse.ArgumentTypeError("--warmup-pairs must be between 0 and 64")
    return BufferedConfig(
        source_resource=str(source),
        voltmeter_resource=str(voltmeter),
        nplc_values=nplc_values,
        pairs=pairs,
        warmup_pairs=warmup_pairs,
        measurement_voltage_v=positive_float(
            args.measurement_voltage_v, "measurement voltage"
        ),
        source_voltage_range_v=positive_float(
            args.source_voltage_range_v, "source voltage range"
        ),
        voltmeter_range_v=positive_float(args.voltmeter_range_v, "voltmeter range"),
        current_range_a=positive_float(args.current_range_a, "current range"),
        compliance_current_a=positive_float(
            args.compliance_current_a, "compliance current"
        ),
        timeout_ms=max(1000, int(args.timeout_ms)),
        output_dir=Path(args.output_dir),
        label=safe_label(str(args.label)),
        trigger_mode=str(args.trigger_mode),
        source_delay_s=max(0.0, float(args.source_delay_s)),
    )


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


def run(config: BufferedConfig) -> None:
    import pyvisa

    timestamp = dt.datetime.now().strftime("%Y%m%d-%H%M%S")
    config.output_dir.mkdir(parents=True, exist_ok=True)
    raw_path = config.output_dir / f"{config.label}_{timestamp}_raw.csv"
    summary_path = config.output_dir / f"{config.label}_{timestamp}_summary.csv"

    print(f"Source: {config.source_resource}")
    print(f"Voltmeter: {config.voltmeter_resource}")
    print(f"NPLC values: {', '.join(f'{value:g}' for value in config.nplc_values)}")
    print(f"Pairs per NPLC: {config.pairs}")
    print(f"Trigger mode: {config.trigger_mode}")
    print(f"Source delay: {config.source_delay_s:g} s")
    print(f"Raw CSV: {raw_path}")
    print(f"Summary CSV: {summary_path}")
    write_csv(raw_path, raw_fieldnames(), [])
    write_csv(summary_path, summary_fieldnames(), [])

    rm = pyvisa.ResourceManager()
    send_ifc_if_gpib(rm, config.source_resource, config.voltmeter_resource)
    source = rm.open_resource(config.source_resource)
    voltmeter = rm.open_resource(config.voltmeter_resource)
    for handle in (source, voltmeter):
        handle.timeout = config.timeout_ms
        handle.read_termination = "\n"
        handle.write_termination = "\n"
    raw_rows: list[dict[str, object]] = []
    summary_rows: list[dict[str, object]] = []
    try:
        reset_instruments(source, voltmeter)
        for nplc in config.nplc_values:
            print(f"\nNPLC {nplc:g}: warmup {config.warmup_pairs} pairs", flush=True)
            if config.warmup_pairs:
                run_buffered_once(source, voltmeter, config, nplc, config.warmup_pairs)
            print(f"NPLC {nplc:g}: measuring {config.pairs} pairs", flush=True)
            result = run_buffered_once(source, voltmeter, config, nplc, config.pairs)
            rows = build_pair_rows(nplc, result)
            raw_rows.extend(rows)
            summary = summarize_rows(nplc, rows, result)
            summary_rows.append(summary)
            write_csv(raw_path, raw_fieldnames(), raw_rows)
            write_csv(summary_path, summary_fieldnames(), summary_rows)
            print(
                "NPLC {n:g}: elapsed={elapsed:.3f}s rate={rate:.3f} pair/s "
                "R={mean:.3f} ohm std={std:.3f} sem={sem:.3f} "
                "stat_R={stat:.3f} rel_err={err:.4g}%".format(
                    n=nplc,
                    elapsed=summary["elapsed_s"],
                    rate=summary["pairs_per_s"],
                    mean=summary["raw_mean_ohm"],
                    std=summary["raw_std_ohm"],
                    sem=summary["raw_sem_ohm"],
                    stat=summary["stats_estimated_resistance_ohm"],
                    err=summary["stats_mean_error_percent"],
                ),
                flush=True,
            )
    finally:
        try:
            reset_instruments(source, voltmeter)
        finally:
            source.close()
            voltmeter.close()
            rm.close()

    print("\nDone")
    print(raw_path.resolve())
    print(summary_path.resolve())


def send_ifc_if_gpib(rm, *resources: str) -> None:
    buses = sorted(
        {
            resource.split("::", 1)[0]
            for resource in resources
            if resource.upper().startswith("GPIB") and "::" in resource
        }
    )
    for bus in buses:
        interface = None
        try:
            interface = rm.open_resource(f"{bus}::INTFC")
            interface.timeout = 3000
            interface.send_ifc()
        except Exception as exc:
            print(f"warning: {bus} IFC failed: {exc}", flush=True)
        finally:
            if interface is not None:
                interface.close()


def run_buffered_once(
    source,
    voltmeter,
    config: BufferedConfig,
    nplc: float,
    pairs: int,
) -> BufferedResult:
    points = pairs * 2
    configure_2182a(voltmeter, config, nplc, points)
    configure_2400(source, config, nplc, points)
    pre_errors_voltmeter = filter_expected_errors(query_errors(voltmeter))
    pre_errors_source = filter_expected_errors(query_errors(source))
    voltmeter.write("INIT")
    time.sleep(0.1)
    source.write(":OUTP ON")
    started = time.perf_counter()
    source.write(":INIT")
    source.query("*OPC?")
    elapsed_s = time.perf_counter() - started

    stats_started = time.perf_counter()
    voltmeter_mean = query_2182a_stat(voltmeter, "MEAN")
    voltmeter_sdev = query_2182a_stat(voltmeter, "SDEV")
    source_mean = query_2400_stat(source, "MEAN")
    source_sdev = query_2400_stat(source, "SDEV")
    stats_query_s = time.perf_counter() - stats_started

    raw_started = time.perf_counter()
    source_values = parse_floats(source.query(":TRAC:DATA?"))
    voltmeter_values = parse_floats(voltmeter.query("TRAC:DATA?"))
    raw_fetch_s = time.perf_counter() - raw_started
    source.write(":OUTP OFF")

    return BufferedResult(
        nplc=nplc,
        pairs=pairs,
        elapsed_s=elapsed_s,
        stats_query_s=stats_query_s,
        raw_fetch_s=raw_fetch_s,
        source_values=source_values,
        voltmeter_values=voltmeter_values,
        source_mean_values=source_mean,
        source_sdev_values=source_sdev,
        voltmeter_mean_values=voltmeter_mean,
        voltmeter_sdev_values=voltmeter_sdev,
        post_errors_source=pre_errors_source
        + filter_expected_errors(query_errors(source)),
        post_errors_voltmeter=pre_errors_voltmeter
        + filter_expected_errors(query_errors(voltmeter)),
    )


def configure_2182a(voltmeter, config: BufferedConfig, nplc: float, points: int) -> None:
    voltmeter.write("*CLS")
    voltmeter.write("ABOR")
    voltmeter.write("CONF:VOLT")
    voltmeter.write("SENS:CHAN 1")
    voltmeter.write(f"SENS:VOLT:RANG {config.voltmeter_range_v:.12g}")
    voltmeter.write(f"SENS:VOLT:NPLC {nplc:.12g}")
    voltmeter.write("TRIG:SOUR EXT")
    voltmeter.write("TRIG:DEL 0")
    voltmeter.write(f"TRIG:COUN {points}")
    voltmeter.write("SAMP:COUN 1")
    voltmeter.write("FORM:ELEM READ")
    voltmeter.write("TRAC:CLE")
    voltmeter.write(f"TRAC:POIN {points}")
    voltmeter.write("TRAC:FEED SENS")
    voltmeter.write("TRAC:FEED:CONT NEXT")


def configure_2400(source, config: BufferedConfig, nplc: float, points: int) -> None:
    values = [-config.measurement_voltage_v, config.measurement_voltage_v] * (
        points // 2
    )
    source.write("*CLS")
    source.write(":ABOR")
    source.write(":OUTP OFF")
    source.write(":TRIG:CLE")
    source.write(":SOUR:VOLT:MODE FIX")
    source.write(":SOUR:VOLT 0")
    source.write(":SENS:FUNC:CONC OFF")
    source.write(":SOUR:FUNC VOLT")
    source.write(':SENS:FUNC "CURR:DC"')
    source.write(f":SOUR:VOLT:RANG {config.source_voltage_range_v:.12g}")
    source.write(f":SENS:CURR:RANG {config.current_range_a:.12g}")
    source.write(f":SENS:CURR:PROT {config.compliance_current_a:.12g}")
    source.write(f":SENS:CURR:NPLC {nplc:.12g}")
    source.write(":FORM:ELEM VOLT,CURR")
    source.write(":SOUR:VOLT:MODE LIST")
    source.write(":SOUR:LIST:VOLT " + ",".join(f"{x:.12g}" for x in values[:100]))
    for start in range(100, len(values), 100):
        chunk = values[start : start + 100]
        source.write(":SOUR:LIST:VOLT:APP " + ",".join(f"{x:.12g}" for x in chunk))
    source.write(f":TRIG:COUN {points}")
    if config.trigger_mode == "oneway":
        source.write(":TRIG:SOUR IMM")
        source.write(":TRIG:DIR SOUR")
        source.write(":TRIG:OLIN 2")
        source.write(":TRIG:OUTP SOUR")
        source.write(f":SOUR:DEL {config.source_delay_s:.12g}")
    else:
        source.write(":TRIG:SOUR TLIN")
        source.write(":TRIG:DIR SOUR")
        source.write(":TRIG:INP SOUR")
        source.write(":TRIG:ILIN 1")
        source.write(":TRIG:OLIN 2")
        source.write(":TRIG:OUTP SOUR")
        source.write(":SOUR:DEL 0")
    source.write(":TRAC:CLE")
    source.write(f":TRAC:POIN {points}")
    source.write(":TRAC:FEED SENS")
    source.write(":TRAC:FEED:CONT NEXT")


def reset_instruments(source, voltmeter) -> None:
    for command in (
        ":ABOR",
        ":OUTP OFF",
        ":TRIG:CLE",
        ":SOUR:VOLT:MODE FIX",
        ":SOUR:VOLT 0",
        ":TRIG:SOUR IMM",
        ":TRIG:COUN 1",
        "*CLS",
    ):
        safe_write(source, command)
    for command in (
        "ABOR",
        "TRIG:SOUR IMM",
        "TRIG:COUN 1",
        "SAMP:COUN 1",
        "*CLS",
    ):
        safe_write(voltmeter, command)


def safe_write(handle, command: str) -> None:
    try:
        handle.write(command)
    except Exception:
        pass


def query_2182a_stat(voltmeter, statistic: str) -> list[float]:
    voltmeter.write(f"CALC2:FORM {statistic}")
    voltmeter.write("CALC2:STAT ON")
    return parse_floats(voltmeter.query("CALC2:IMM?"))


def query_2400_stat(source, statistic: str) -> list[float]:
    source.write(f":CALC3:FORM {statistic}")
    return parse_floats(source.query(":CALC3:DATA?"))


def build_pair_rows(nplc: float, result: BufferedResult) -> list[dict[str, object]]:
    rows: list[dict[str, object]] = []
    count = min(
        result.pairs,
        len(result.source_values) // 4,
        len(result.voltmeter_values) // 2,
    )
    for index in range(count):
        source_v_neg = result.source_values[4 * index]
        current_neg = result.source_values[4 * index + 1]
        source_v_pos = result.source_values[4 * index + 2]
        current_pos = result.source_values[4 * index + 3]
        voltage_neg = result.voltmeter_values[2 * index]
        voltage_pos = result.voltmeter_values[2 * index + 1]
        delta_v = voltage_pos - voltage_neg
        delta_i = current_pos - current_neg
        rows.append(
            {
                "nplc": nplc,
                "pair": index,
                "elapsed_s_total": result.elapsed_s,
                "source_v_neg": source_v_neg,
                "current_neg_a": current_neg,
                "voltage_neg_v": voltage_neg,
                "source_v_pos": source_v_pos,
                "current_pos_a": current_pos,
                "voltage_pos_v": voltage_pos,
                "delta_v": delta_v,
                "delta_i": delta_i,
                "differential_resistance_ohm": delta_v / delta_i
                if delta_i != 0
                else math.inf,
            }
        )
    return rows


def summarize_rows(
    nplc: float,
    rows: list[dict[str, object]],
    result: BufferedResult,
) -> dict[str, object]:
    resistances = [
        float(row["differential_resistance_ohm"])
        for row in rows
        if math.isfinite(float(row["differential_resistance_ohm"]))
    ]
    raw_mean = statistics.fmean(resistances) if resistances else math.nan
    raw_std = statistics.stdev(resistances) if len(resistances) > 1 else math.nan
    raw_sem = raw_std / math.sqrt(len(resistances)) if len(resistances) > 1 else math.nan
    stats_r = estimate_resistance_from_alternating_stats(result)
    error_ohm = stats_r - raw_mean if math.isfinite(stats_r) else math.nan
    error_percent = error_ohm / raw_mean * 100.0 if raw_mean else math.nan
    return {
        "nplc": nplc,
        "pairs": len(rows),
        "elapsed_s": result.elapsed_s,
        "pairs_per_s": len(rows) / result.elapsed_s if result.elapsed_s else math.nan,
        "stats_query_s": result.stats_query_s,
        "raw_fetch_s": result.raw_fetch_s,
        "raw_mean_ohm": raw_mean,
        "raw_std_ohm": raw_std,
        "raw_sem_ohm": raw_sem,
        "stats_estimated_resistance_ohm": stats_r,
        "stats_mean_error_ohm": error_ohm,
        "stats_mean_error_percent": error_percent,
        "voltmeter_mean_v": first_or_nan(result.voltmeter_mean_values),
        "voltmeter_sdev_v": first_or_nan(result.voltmeter_sdev_values),
        "source_current_mean_a": value_or_nan(result.source_mean_values, 1),
        "source_current_sdev_a": value_or_nan(result.source_sdev_values, 1),
        "post_errors_2182": "; ".join(result.post_errors_voltmeter),
        "post_errors_2400": "; ".join(result.post_errors_source),
    }


def estimate_resistance_from_alternating_stats(result: BufferedResult) -> float:
    total_points = result.pairs * 2
    if total_points < 2:
        return math.nan
    # Instrument SDEV is over alternating -/+ readings. For a balanced two-level
    # sequence, half-amplitude ~= sample_std * sqrt((N - 1) / N), ignoring
    # measurement noise and polarity asymmetry.
    correction = math.sqrt((total_points - 1) / total_points)
    voltage_half_delta = first_or_nan(result.voltmeter_sdev_values) * correction
    current_half_delta = value_or_nan(result.source_sdev_values, 1) * correction
    if current_half_delta == 0 or not math.isfinite(current_half_delta):
        return math.nan
    return voltage_half_delta / current_half_delta


def query_errors(handle) -> list[str]:
    errors: list[str] = []
    previous_timeout = getattr(handle, "timeout", None)
    try:
        handle.timeout = min(int(previous_timeout or 2000), 2000)
        for _ in range(8):
            try:
                error = str(handle.query("SYST:ERR?")).strip()
            except Exception as exc:
                errors.append(f"error-query failed: {exc}")
                break
            if error.startswith("0"):
                break
            errors.append(error)
    finally:
        if previous_timeout is not None:
            handle.timeout = previous_timeout
    return errors


def filter_expected_errors(errors: list[str]) -> list[str]:
    return [error for error in errors if not error.startswith("800,")]


def parse_floats(text: object) -> list[float]:
    values: list[float] = []
    for part in str(text).replace("\n", ",").split(","):
        token = part.strip()
        if not token:
            continue
        try:
            values.append(float(token))
        except ValueError:
            pass
    return values


def write_csv(path: Path, fieldnames: list[str], rows: list[dict[str, object]]) -> None:
    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(rows)


def raw_fieldnames() -> list[str]:
    return [
        "nplc",
        "pair",
        "elapsed_s_total",
        "source_v_neg",
        "current_neg_a",
        "voltage_neg_v",
        "source_v_pos",
        "current_pos_a",
        "voltage_pos_v",
        "delta_v",
        "delta_i",
        "differential_resistance_ohm",
    ]


def summary_fieldnames() -> list[str]:
    return [
        "nplc",
        "pairs",
        "elapsed_s",
        "pairs_per_s",
        "stats_query_s",
        "raw_fetch_s",
        "raw_mean_ohm",
        "raw_std_ohm",
        "raw_sem_ohm",
        "stats_estimated_resistance_ohm",
        "stats_mean_error_ohm",
        "stats_mean_error_percent",
        "voltmeter_mean_v",
        "voltmeter_sdev_v",
        "source_current_mean_a",
        "source_current_sdev_a",
        "post_errors_2182",
        "post_errors_2400",
    ]


def positive_float(value: object, name: str) -> float:
    try:
        result = float(value)
    except (TypeError, ValueError) as exc:
        raise argparse.ArgumentTypeError(f"{name} must be a number") from exc
    if not math.isfinite(result) or result <= 0:
        raise argparse.ArgumentTypeError(f"{name} must be positive")
    return result


def first_or_nan(values: list[float]) -> float:
    return values[0] if values else math.nan


def value_or_nan(values: list[float], index: int) -> float:
    return values[index] if len(values) > index else math.nan


def safe_label(value: str) -> str:
    label = "".join(char if char.isalnum() or char in "._-" else "_" for char in value)
    return label.strip("._-") or "buffered_nplc_compare"


if __name__ == "__main__":
    raise SystemExit(main())
