"""Time-budgeted contact-quality study for Keithley 2400 + 2182A.

This script answers a different question from a fixed-N sweep: given the same
wall-clock budget per NPLC, which setting gives the most useful evidence about
contact quality? It uses a software loop so all NPLC values, including 10, use
the same robust path.
"""

from __future__ import annotations

import argparse
import csv
import datetime as dt
import math
import random
import statistics
import sys
import time
from dataclasses import dataclass
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))


@dataclass(frozen=True)
class StudyConfig:
    source_resource: str
    voltmeter_resource: str
    nplc_values: tuple[float, ...]
    seconds_per_nplc: float
    rounds: int
    warmup_pairs: int
    measurement_voltage_v: float
    source_voltage_range_v: float
    voltmeter_range_v: float
    current_range_a: float
    compliance_current_a: float
    timeout_ms: int
    output_dir: Path
    label: str
    seed: int


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Run a time-budgeted NPLC contact study.")
    parser.add_argument("--source", default=None)
    parser.add_argument("--voltmeter", default=None)
    parser.add_argument("--nplc-values", default="0.1,0.3,1,2,3,5,10")
    parser.add_argument("--seconds-per-nplc", type=float, default=30.0)
    parser.add_argument("--rounds", type=int, default=3)
    parser.add_argument("--warmup-pairs", type=int, default=2)
    parser.add_argument("--measurement-voltage-v", type=float, default=0.03)
    parser.add_argument("--source-voltage-range-v", type=float, default=0.21)
    parser.add_argument("--voltmeter-range-v", type=float, default=0.1)
    parser.add_argument("--current-range-a", type=float, default=10e-6)
    parser.add_argument("--compliance-current-a", type=float, default=10e-6)
    parser.add_argument("--timeout-ms", type=int, default=240000)
    parser.add_argument("--output-dir", type=Path, default=Path("measurements"))
    parser.add_argument("--label", default="contact_time_budget_study")
    parser.add_argument("--seed", type=int, default=2400)
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


def make_config(args: argparse.Namespace) -> StudyConfig:
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
    rounds = int(args.rounds)
    if rounds < 1:
        raise argparse.ArgumentTypeError("--rounds must be at least 1")
    warmup_pairs = int(args.warmup_pairs)
    if warmup_pairs < 0:
        raise argparse.ArgumentTypeError("--warmup-pairs must be non-negative")
    return StudyConfig(
        source_resource=str(source),
        voltmeter_resource=str(voltmeter),
        nplc_values=nplc_values,
        seconds_per_nplc=positive_float(args.seconds_per_nplc, "seconds per NPLC"),
        rounds=rounds,
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
        seed=int(args.seed),
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


def run(config: StudyConfig) -> None:
    import pyvisa

    timestamp = dt.datetime.now().strftime("%Y%m%d-%H%M%S")
    config.output_dir.mkdir(parents=True, exist_ok=True)
    raw_path = config.output_dir / f"{config.label}_{timestamp}_raw.csv"
    block_path = config.output_dir / f"{config.label}_{timestamp}_blocks.csv"
    summary_path = config.output_dir / f"{config.label}_{timestamp}_summary.csv"

    print(f"Source: {config.source_resource}")
    print(f"Voltmeter: {config.voltmeter_resource}")
    print(f"NPLC values: {', '.join(f'{value:g}' for value in config.nplc_values)}")
    print(f"Rounds: {config.rounds}")
    print(f"Seconds per NPLC per round: {config.seconds_per_nplc:g}")
    print(f"Raw CSV: {raw_path}")
    print(f"Block CSV: {block_path}")
    print(f"Summary CSV: {summary_path}")
    write_csv(raw_path, raw_fieldnames(), [])
    write_csv(block_path, block_fieldnames(), [])
    write_csv(summary_path, summary_fieldnames(), [])

    rm = pyvisa.ResourceManager()
    send_ifc_if_gpib(rm, config.source_resource, config.voltmeter_resource)
    source = rm.open_resource(config.source_resource)
    voltmeter = rm.open_resource(config.voltmeter_resource)
    for handle in (source, voltmeter):
        handle.timeout = config.timeout_ms
        handle.read_termination = "\n"
        handle.write_termination = "\n"
        handle.clear()

    rng = random.Random(config.seed)
    raw_rows: list[dict[str, object]] = []
    block_rows: list[dict[str, object]] = []
    try:
        reset_instruments(source, voltmeter)
        for round_index in range(config.rounds):
            order = list(config.nplc_values)
            rng.shuffle(order)
            print(
                f"\nRound {round_index + 1}/{config.rounds}: "
                + ", ".join(f"{value:g}" for value in order),
                flush=True,
            )
            for nplc in order:
                configure_instruments(source, voltmeter, config, nplc)
                if config.warmup_pairs:
                    run_warmup(source, voltmeter, config, config.warmup_pairs)
                rows, elapsed_s = run_time_block(
                    source, voltmeter, config, nplc, round_index
                )
                raw_rows.extend(rows)
                block = summarize_block(nplc, round_index, rows, elapsed_s)
                block_rows.append(block)
                summary_rows = summarize_all(block_rows)
                write_csv(raw_path, raw_fieldnames(), raw_rows)
                write_csv(block_path, block_fieldnames(), block_rows)
                write_csv(summary_path, summary_fieldnames(), summary_rows)
                print(
                    "round={round_num} NPLC={n:g}: elapsed={elapsed:.2f}s "
                    "pairs={pairs} rate={rate:.3f}/s median={median:.2f} "
                    "robust_sigma={robust:.2f} sem={sem:.2f} span={span:.2f}".format(
                        round_num=round_index + 1,
                        n=nplc,
                        elapsed=block["elapsed_s"],
                        pairs=block["pairs"],
                        rate=block["pairs_per_s"],
                        median=block["median_ohm"],
                        robust=block["mad_sigma_ohm"],
                        sem=block["sem_ohm"],
                        span=block["span_ohm"],
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

    write_csv(summary_path, summary_fieldnames(), summarize_all(block_rows))
    print("\nDone")
    print(raw_path.resolve())
    print(block_path.resolve())
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


def configure_instruments(source, voltmeter, config: StudyConfig, nplc: float) -> None:
    source.write("*CLS")
    source.write(":ABOR")
    source.write(":OUTP OFF")
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
    source.write(":TRIG:SOUR IMM")
    source.write(":TRIG:COUN 1")
    source.write(":SOUR:DEL 0")

    voltmeter.write("*CLS")
    voltmeter.write("ABOR")
    voltmeter.write("CONF:VOLT")
    voltmeter.write("SENS:CHAN 1")
    voltmeter.write(f"SENS:VOLT:RANG {config.voltmeter_range_v:.12g}")
    voltmeter.write(f"SENS:VOLT:NPLC {nplc:.12g}")
    voltmeter.write("TRIG:SOUR IMM")
    voltmeter.write("TRIG:DEL 0")
    voltmeter.write("TRIG:COUN 1")
    voltmeter.write("SAMP:COUN 1")
    voltmeter.write("FORM:ELEM READ")
    source.write(":OUTP ON")


def run_warmup(source, voltmeter, config: StudyConfig, pairs: int) -> None:
    for _ in range(pairs):
        read_point(source, voltmeter, -config.measurement_voltage_v)
        read_point(source, voltmeter, config.measurement_voltage_v)


def run_time_block(
    source,
    voltmeter,
    config: StudyConfig,
    nplc: float,
    round_index: int,
) -> tuple[list[dict[str, object]], float]:
    rows: list[dict[str, object]] = []
    started = time.perf_counter()
    deadline = started + config.seconds_per_nplc
    pair_index = 0
    while True:
        pair_started = time.perf_counter()
        if rows and pair_started >= deadline:
            break
        negative = read_point(source, voltmeter, -config.measurement_voltage_v)
        positive = read_point(source, voltmeter, config.measurement_voltage_v)
        pair_finished = time.perf_counter()
        source_v_neg, current_neg, voltage_neg = negative
        source_v_pos, current_pos, voltage_pos = positive
        delta_v = voltage_pos - voltage_neg
        delta_i = current_pos - current_neg
        rows.append(
            {
                "round": round_index,
                "nplc": nplc,
                "pair": pair_index,
                "t_s": pair_finished - started,
                "pair_elapsed_s": pair_finished - pair_started,
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
        pair_index += 1
    return rows, time.perf_counter() - started


def read_point(source, voltmeter, source_voltage_v: float) -> tuple[float, float, float]:
    source.write(f":SOUR:VOLT {source_voltage_v:.12g}")
    source.write(":INIT")
    voltmeter.write("INIT")
    voltage_voltmeter = parse_first_float(voltmeter.query("FETC?"))
    source_values = parse_floats(source.query(":FETC?"))
    voltage_source = value_or_nan(source_values, 0)
    current = value_or_nan(source_values, 1)
    return voltage_source, current, voltage_voltmeter


def reset_instruments(source, voltmeter) -> None:
    for command in (
        ":ABOR",
        ":OUTP OFF",
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


def summarize_block(
    nplc: float,
    round_index: int,
    rows: list[dict[str, object]],
    elapsed_s: float,
) -> dict[str, object]:
    values = finite_resistances(rows)
    return stats_row(
        {
            "round": round_index,
            "nplc": nplc,
            "pairs": len(values),
            "elapsed_s": elapsed_s,
            "pairs_per_s": len(values) / elapsed_s if elapsed_s else math.nan,
        },
        values,
        [float(row["t_s"]) for row in rows if finite_resistance(row)],
    )


def summarize_all(block_rows: list[dict[str, object]]) -> list[dict[str, object]]:
    result: list[dict[str, object]] = []
    nplc_values = sorted({float(row["nplc"]) for row in block_rows})
    for nplc in nplc_values:
        rows = [row for row in block_rows if float(row["nplc"]) == nplc]
        medians = [float(row["median_ohm"]) for row in rows if finite_number(row["median_ohm"])]
        sems = [float(row["sem_ohm"]) for row in rows if finite_number(row["sem_ohm"])]
        robust_sigmas = [
            float(row["mad_sigma_ohm"])
            for row in rows
            if finite_number(row["mad_sigma_ohm"])
        ]
        spans = [float(row["span_ohm"]) for row in rows if finite_number(row["span_ohm"])]
        rates = [float(row["pairs_per_s"]) for row in rows if finite_number(row["pairs_per_s"])]
        pairs = [int(row["pairs"]) for row in rows]
        result.append(
            {
                "nplc": nplc,
                "rounds": len(rows),
                "total_pairs": sum(pairs),
                "median_pairs_per_round": median_or_nan([float(pair) for pair in pairs]),
                "median_pairs_per_s": median_or_nan(rates),
                "median_of_block_medians_ohm": median_or_nan(medians),
                "span_of_block_medians_ohm": max(medians) - min(medians)
                if medians
                else math.nan,
                "median_block_sem_ohm": median_or_nan(sems),
                "median_block_mad_sigma_ohm": median_or_nan(robust_sigmas),
                "median_block_span_ohm": median_or_nan(spans),
                "eff_sem_ohm_sqrt_s": median_or_nan(sems)
                * math.sqrt(positive_or_nan(config_seconds(rows))),
            }
        )
    return result


def config_seconds(rows: list[dict[str, object]]) -> float:
    elapsed = [float(row["elapsed_s"]) for row in rows if finite_number(row["elapsed_s"])]
    return median_or_nan(elapsed)


def stats_row(
    prefix: dict[str, object],
    values: list[float],
    times_s: list[float],
) -> dict[str, object]:
    if not values:
        return {
            **prefix,
            "mean_ohm": math.nan,
            "median_ohm": math.nan,
            "std_ohm": math.nan,
            "sem_ohm": math.nan,
            "mad_sigma_ohm": math.nan,
            "iqr_ohm": math.nan,
            "span_ohm": math.nan,
            "p95_abs_dev_ohm": math.nan,
            "median_abs_step_ohm": math.nan,
            "p95_abs_step_ohm": math.nan,
            "drift_slope_ohm_per_s": math.nan,
        }
    median = statistics.median(values)
    abs_devs = [abs(value - median) for value in values]
    steps = [abs(values[index] - values[index - 1]) for index in range(1, len(values))]
    return {
        **prefix,
        "mean_ohm": statistics.fmean(values),
        "median_ohm": median,
        "std_ohm": statistics.stdev(values) if len(values) > 1 else math.nan,
        "sem_ohm": statistics.stdev(values) / math.sqrt(len(values))
        if len(values) > 1
        else math.nan,
        "mad_sigma_ohm": 1.4826 * statistics.median(abs_devs),
        "iqr_ohm": percentile(values, 75.0) - percentile(values, 25.0),
        "span_ohm": max(values) - min(values),
        "p95_abs_dev_ohm": percentile(abs_devs, 95.0),
        "median_abs_step_ohm": statistics.median(steps) if steps else math.nan,
        "p95_abs_step_ohm": percentile(steps, 95.0) if steps else math.nan,
        "drift_slope_ohm_per_s": slope(times_s, values),
    }


def finite_resistances(rows: list[dict[str, object]]) -> list[float]:
    return [
        float(row["differential_resistance_ohm"])
        for row in rows
        if finite_resistance(row)
    ]


def finite_resistance(row: dict[str, object]) -> bool:
    return finite_number(row["differential_resistance_ohm"])


def finite_number(value: object) -> bool:
    try:
        return math.isfinite(float(value))
    except (TypeError, ValueError):
        return False


def percentile(values: list[float], percent: float) -> float:
    if not values:
        return math.nan
    ordered = sorted(values)
    if len(ordered) == 1:
        return ordered[0]
    position = (len(ordered) - 1) * percent / 100.0
    lower = math.floor(position)
    upper = math.ceil(position)
    if lower == upper:
        return ordered[int(position)]
    weight = position - lower
    return ordered[lower] * (1.0 - weight) + ordered[upper] * weight


def slope(xs: list[float], ys: list[float]) -> float:
    if len(xs) != len(ys) or len(xs) < 2:
        return math.nan
    mean_x = statistics.fmean(xs)
    mean_y = statistics.fmean(ys)
    denom = sum((x - mean_x) ** 2 for x in xs)
    if denom == 0:
        return math.nan
    return sum((x - mean_x) * (y - mean_y) for x, y in zip(xs, ys)) / denom


def parse_first_float(text: object) -> float:
    values = parse_floats(text)
    return values[0] if values else math.nan


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
        "round",
        "nplc",
        "pair",
        "t_s",
        "pair_elapsed_s",
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


def block_fieldnames() -> list[str]:
    return [
        "round",
        "nplc",
        "pairs",
        "elapsed_s",
        "pairs_per_s",
        "mean_ohm",
        "median_ohm",
        "std_ohm",
        "sem_ohm",
        "mad_sigma_ohm",
        "iqr_ohm",
        "span_ohm",
        "p95_abs_dev_ohm",
        "median_abs_step_ohm",
        "p95_abs_step_ohm",
        "drift_slope_ohm_per_s",
    ]


def summary_fieldnames() -> list[str]:
    return [
        "nplc",
        "rounds",
        "total_pairs",
        "median_pairs_per_round",
        "median_pairs_per_s",
        "median_of_block_medians_ohm",
        "span_of_block_medians_ohm",
        "median_block_sem_ohm",
        "median_block_mad_sigma_ohm",
        "median_block_span_ohm",
        "eff_sem_ohm_sqrt_s",
    ]


def positive_float(value: object, name: str) -> float:
    try:
        result = float(value)
    except (TypeError, ValueError) as exc:
        raise argparse.ArgumentTypeError(f"{name} must be a number") from exc
    if not math.isfinite(result) or result <= 0:
        raise argparse.ArgumentTypeError(f"{name} must be positive")
    return result


def positive_or_nan(value: float) -> float:
    return value if math.isfinite(value) and value > 0 else math.nan


def median_or_nan(values: list[float]) -> float:
    return statistics.median(values) if values else math.nan


def value_or_nan(values: list[float], index: int) -> float:
    return values[index] if len(values) > index else math.nan


def safe_label(value: str) -> str:
    label = "".join(char if char.isalnum() or char in "._-" else "_" for char in value)
    return label.strip("._-") or "contact_time_budget_study"


if __name__ == "__main__":
    raise SystemExit(main())
