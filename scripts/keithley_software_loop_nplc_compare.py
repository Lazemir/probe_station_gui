"""Software-loop Keithley 2400 + 2182A NPLC comparison.

This is the deliberately non-buffered baseline:
- Python sets 2400 voltage to -V, reads 2400 current and 2182A voltage.
- Python sets 2400 voltage to +V, reads 2400 current and 2182A voltage.
- One differential resistance point is computed from the two polarities.

It uses fixed ranges and the same pair count as the buffered Trigger Link
script, but it does not use TRAC buffers or Trigger Link sequencing.
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
class SoftwareLoopConfig:
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


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Compare non-buffered software-loop readings at fixed pair count."
    )
    parser.add_argument("--source", default=None)
    parser.add_argument("--voltmeter", default=None)
    parser.add_argument("--nplc-values", default="0.1,0.3,1,2,3,5")
    parser.add_argument("--pairs", type=int, default=60)
    parser.add_argument("--warmup-pairs", type=int, default=4)
    parser.add_argument("--measurement-voltage-v", type=float, default=0.03)
    parser.add_argument("--source-voltage-range-v", type=float, default=0.21)
    parser.add_argument("--voltmeter-range-v", type=float, default=0.1)
    parser.add_argument("--current-range-a", type=float, default=10e-6)
    parser.add_argument("--compliance-current-a", type=float, default=10e-6)
    parser.add_argument("--timeout-ms", type=int, default=180000)
    parser.add_argument("--output-dir", type=Path, default=Path("measurements"))
    parser.add_argument("--label", default="software_loop_nplc_compare")
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


def make_config(args: argparse.Namespace) -> SoftwareLoopConfig:
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
    if pairs < 2:
        raise argparse.ArgumentTypeError("--pairs must be at least 2")
    warmup_pairs = int(args.warmup_pairs)
    if warmup_pairs < 0:
        raise argparse.ArgumentTypeError("--warmup-pairs must be non-negative")
    return SoftwareLoopConfig(
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


def run(config: SoftwareLoopConfig) -> None:
    import pyvisa

    timestamp = dt.datetime.now().strftime("%Y%m%d-%H%M%S")
    config.output_dir.mkdir(parents=True, exist_ok=True)
    raw_path = config.output_dir / f"{config.label}_{timestamp}_raw.csv"
    summary_path = config.output_dir / f"{config.label}_{timestamp}_summary.csv"

    print(f"Source: {config.source_resource}")
    print(f"Voltmeter: {config.voltmeter_resource}")
    print(f"NPLC values: {', '.join(f'{value:g}' for value in config.nplc_values)}")
    print(f"Pairs per NPLC: {config.pairs}")
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
        handle.clear()

    raw_rows: list[dict[str, object]] = []
    summary_rows: list[dict[str, object]] = []
    try:
        reset_instruments(source, voltmeter)
        for nplc in config.nplc_values:
            print(f"\nNPLC {nplc:g}: warmup {config.warmup_pairs} pairs", flush=True)
            configure_instruments(source, voltmeter, config, nplc)
            if config.warmup_pairs:
                run_pairs(source, voltmeter, config, nplc, config.warmup_pairs, record=False)
            print(f"NPLC {nplc:g}: measuring {config.pairs} pairs", flush=True)
            rows, elapsed_s = run_pairs(
                source, voltmeter, config, nplc, config.pairs, record=True
            )
            raw_rows.extend(rows)
            summary = summarize_rows(nplc, rows, elapsed_s)
            summary_rows.append(summary)
            write_csv(raw_path, raw_fieldnames(), raw_rows)
            write_csv(summary_path, summary_fieldnames(), summary_rows)
            print(
                "NPLC {n:g}: elapsed={elapsed:.3f}s rate={rate:.3f} pair/s "
                "R={mean:.3f} ohm std={std:.3f} sem={sem:.3f}".format(
                    n=nplc,
                    elapsed=summary["elapsed_s"],
                    rate=summary["pairs_per_s"],
                    mean=summary["raw_mean_ohm"],
                    std=summary["raw_std_ohm"],
                    sem=summary["raw_sem_ohm"],
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


def configure_instruments(source, voltmeter, config: SoftwareLoopConfig, nplc: float) -> None:
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


def run_pairs(
    source,
    voltmeter,
    config: SoftwareLoopConfig,
    nplc: float,
    pairs: int,
    *,
    record: bool,
) -> tuple[list[dict[str, object]], float]:
    rows: list[dict[str, object]] = []
    started = time.perf_counter()
    for index in range(pairs):
        negative = read_point(source, voltmeter, -config.measurement_voltage_v)
        positive = read_point(source, voltmeter, config.measurement_voltage_v)
        if not record:
            continue
        source_v_neg, current_neg, voltage_neg = negative
        source_v_pos, current_pos, voltage_pos = positive
        delta_v = voltage_pos - voltage_neg
        delta_i = current_pos - current_neg
        rows.append(
            {
                "nplc": nplc,
                "pair": index,
                "elapsed_s_total": None,
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
    elapsed_s = time.perf_counter() - started
    for row in rows:
        row["elapsed_s_total"] = elapsed_s
    return rows, elapsed_s


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


def summarize_rows(
    nplc: float,
    rows: list[dict[str, object]],
    elapsed_s: float,
) -> dict[str, object]:
    resistances = [
        float(row["differential_resistance_ohm"])
        for row in rows
        if math.isfinite(float(row["differential_resistance_ohm"]))
    ]
    raw_mean = statistics.fmean(resistances) if resistances else math.nan
    raw_std = statistics.stdev(resistances) if len(resistances) > 1 else math.nan
    raw_sem = raw_std / math.sqrt(len(resistances)) if len(resistances) > 1 else math.nan
    return {
        "nplc": nplc,
        "pairs": len(rows),
        "elapsed_s": elapsed_s,
        "pairs_per_s": len(rows) / elapsed_s if elapsed_s else math.nan,
        "raw_mean_ohm": raw_mean,
        "raw_std_ohm": raw_std,
        "raw_sem_ohm": raw_sem,
    }


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
        "raw_mean_ohm",
        "raw_std_ohm",
        "raw_sem_ohm",
    ]


def positive_float(value: object, name: str) -> float:
    try:
        result = float(value)
    except (TypeError, ValueError) as exc:
        raise argparse.ArgumentTypeError(f"{name} must be a number") from exc
    if not math.isfinite(result) or result <= 0:
        raise argparse.ArgumentTypeError(f"{name} must be positive")
    return result


def value_or_nan(values: list[float], index: int) -> float:
    return values[index] if len(values) > index else math.nan


def safe_label(value: str) -> str:
    label = "".join(char if char.isalnum() or char in "._-" else "_" for char in value)
    return label.strip("._-") or "software_loop_nplc_compare"


if __name__ == "__main__":
    raise SystemExit(main())
