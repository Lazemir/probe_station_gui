"""Compare native and application-controlled microscope camera exposure."""

from __future__ import annotations

import argparse
import json
import math
import os
import time
from collections.abc import Mapping, Sequence
from dataclasses import asdict, dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

import cv2
import numpy as np

from probe_station_client import ProbeStationClient
from probe_station_gui.camera.api_control import OPERATOR_CAMERA_NODE_NAMES


@dataclass(frozen=True)
class ExposureDiagnosticConfig:
    target_percentile: float = 99.5
    target_level: float = 235.0
    saturation_level: int = 254
    near_clip_level: int = 245
    max_saturated_fraction: float = 0.001
    min_step_ratio: float = 0.5
    max_step_ratio: float = 2.0
    clipping_reduction_ratio: float = 0.5
    settling_frames: int = 2
    convergence_window: int = 3
    brightness_tolerance_fraction: float = 0.015
    exposure_tolerance_fraction: float = 0.01
    gain_tolerance_db: float = 0.05
    custom_target_tolerance_fraction: float = 0.02
    max_iterations: int = 12
    native_timeout_s: float = 12.0
    metric_frame_count: int = 8
    frame_timeout_ms: int = 2500
    fixed_gain_db: float = 0.0
    silicon_luminance_percentile: float = 65.0
    silicon_gradient_percentile: float = 50.0


@dataclass(frozen=True)
class CameraStateSnapshot:
    nodes: dict[str, dict[str, Any]]
    balance_ratios: dict[str, object]
    balance_selector_entries: tuple[str, ...]
    original_balance_selector: str


def next_exposure_us(
    current_us: float,
    measured_level: float,
    limits: tuple[float, float],
    config: ExposureDiagnosticConfig,
) -> float:
    """Return one bounded multiplicative exposure update."""

    current = float(current_us)
    measured = float(measured_level)
    minimum, maximum = (float(limits[0]), float(limits[1]))
    if not all(math.isfinite(value) for value in (current, measured, minimum, maximum)):
        raise ValueError("Exposure update values must be finite.")
    if measured <= 0.0 or minimum <= 0.0 or maximum < minimum:
        raise ValueError("Exposure update values are outside valid bounds.")
    ratio = float(config.target_level) / measured
    if measured >= float(config.saturation_level):
        ratio = min(ratio, float(config.clipping_reduction_ratio))
    ratio = min(float(config.max_step_ratio), max(float(config.min_step_ratio), ratio))
    return min(maximum, max(minimum, current * ratio))


def frame_metrics(
    frames: Sequence[np.ndarray],
    config: ExposureDiagnosticConfig | None = None,
) -> dict[str, float]:
    """Calculate clipping, drift, and silicon-region temporal metrics."""

    cfg = config or ExposureDiagnosticConfig()
    if not frames:
        raise ValueError("At least one frame is required.")
    arrays = [np.asarray(frame) for frame in frames]
    shape = arrays[0].shape
    if len(shape) != 3 or shape[2] != 3:
        raise ValueError("Frames must be RGB arrays.")
    if any(array.shape != shape for array in arrays):
        raise ValueError("Frame sizes do not match.")
    stack = np.stack(arrays, axis=0).astype(np.float32)
    max_rgb = np.max(stack, axis=3)
    final_max = max_rgb[-1]
    brightness_trace = np.percentile(
        max_rgb.reshape(max_rgb.shape[0], -1),
        float(cfg.target_percentile),
        axis=1,
    )
    brightness_mean = float(np.mean(brightness_trace))
    drift = (
        float(np.ptp(brightness_trace)) / brightness_mean
        if brightness_mean > 0.0
        else math.inf
    )

    luminance = np.mean(stack, axis=3)
    median_luminance = np.median(luminance, axis=0)
    grad_x = cv2.Sobel(median_luminance, cv2.CV_32F, 1, 0, ksize=3)
    grad_y = cv2.Sobel(median_luminance, cv2.CV_32F, 0, 1, ksize=3)
    gradient = cv2.magnitude(grad_x, grad_y)
    dark_limit = float(
        np.percentile(median_luminance, cfg.silicon_luminance_percentile)
    )
    gradient_limit = float(
        np.percentile(gradient, cfg.silicon_gradient_percentile)
    )
    silicon_mask = (median_luminance <= dark_limit) & (gradient <= gradient_limit)
    if not np.any(silicon_mask):
        raise ValueError("No stable silicon pixels were found.")
    silicon_series = luminance[:, silicon_mask]
    silicon_signal = float(np.mean(silicon_series))
    silicon_noise = float(np.mean(np.std(silicon_series, axis=0, ddof=0)))
    silicon_snr = silicon_signal / max(silicon_noise, 1e-9)

    return {
        "saturated_fraction": float(
            np.mean(final_max >= float(cfg.saturation_level))
        ),
        "near_saturated_fraction": float(
            np.mean(final_max >= float(cfg.near_clip_level))
        ),
        "highlight_percentile": float(
            np.percentile(final_max, float(cfg.target_percentile))
        ),
        "highlight_headroom": float(
            255.0 - np.percentile(final_max, float(cfg.target_percentile))
        ),
        "temporal_brightness_drift": drift,
        "silicon_signal": silicon_signal,
        "silicon_temporal_noise": silicon_noise,
        "silicon_snr": silicon_snr,
        "silicon_pixel_fraction": float(np.mean(silicon_mask)),
    }


def choose_preferred_mode(
    mode_reports: Mapping[str, Mapping[str, Any]],
    config: ExposureDiagnosticConfig,
) -> str | None:
    """Choose the best clip-compliant mode using declared metric priority."""

    candidates: list[tuple[float, float, float, str]] = []
    for name, report in mode_reports.items():
        if not bool(report.get("ok", False)):
            continue
        metrics = report.get("metrics")
        if not isinstance(metrics, Mapping):
            continue
        saturated = _finite_float(metrics.get("saturated_fraction"), math.inf)
        if saturated > float(config.max_saturated_fraction):
            continue
        snr = _finite_float(metrics.get("silicon_snr"), -math.inf)
        drift = _finite_float(metrics.get("temporal_brightness_drift"), math.inf)
        convergence = _finite_float(report.get("convergence_s"), math.inf)
        candidates.append((-snr, drift, convergence, str(name)))
    if not candidates:
        return None
    candidates.sort()
    return candidates[0][3]


def run_exposure_diagnostic(
    camera: object,
    *,
    config: ExposureDiagnosticConfig | None = None,
    output_dir: str | Path | None = None,
    apply_winner: bool = False,
) -> dict[str, Any]:
    """Run all exposure modes, restore state, and write a reproducible report."""

    cfg = config or ExposureDiagnosticConfig()
    destination = _diagnostic_output_dir(output_dir)
    destination.mkdir(parents=True, exist_ok=True)
    report: dict[str, Any] = {
        "started_at": datetime.now().astimezone().isoformat(timespec="seconds"),
        "output_dir": str(destination),
        "config": asdict(cfg),
        "modes": {},
        "preferred_mode": None,
        "restored": False,
        "applied_winner": False,
    }
    snapshot: CameraStateSnapshot | None = None
    caught: BaseException | None = None
    try:
        snapshot = _snapshot_camera_state(camera)
        report["initial_settings"] = {
            name: node.get("value") for name, node in snapshot.nodes.items()
        }
        report["initial_balance_ratios"] = dict(snapshot.balance_ratios)
        _apply_fixed_white_balance(camera, snapshot)
        mode_reports = _run_all_modes(camera, snapshot, cfg, destination)
        report["modes"] = mode_reports
        report["preferred_mode"] = choose_preferred_mode(mode_reports, cfg)
    except BaseException as exc:
        caught = exc
        report["error"] = f"{type(exc).__name__}: {exc}"
    finally:
        if snapshot is not None:
            try:
                _restore_camera_state(camera, snapshot)
                report["restored"] = True
            except BaseException as restore_exc:
                report["restore_error"] = (
                    f"{type(restore_exc).__name__}: {restore_exc}"
                )
                if caught is None:
                    caught = restore_exc

    preferred = report.get("preferred_mode")
    if caught is None and apply_winner and isinstance(preferred, str) and snapshot:
        _apply_mode(camera, preferred, snapshot, cfg, report["modes"][preferred])
        report["applied_winner"] = True
    report["finished_at"] = datetime.now().astimezone().isoformat(timespec="seconds")
    _write_report(destination, report)
    if caught is not None:
        raise caught
    return report


def _snapshot_camera_state(camera: object) -> CameraStateSnapshot:
    response = camera.settings(list(OPERATOR_CAMERA_NODE_NAMES))
    nodes = _node_map(response)
    required = {
        "ExposureAuto",
        "ExposureTime",
        "GainAuto",
        "Gain",
        "BalanceWhiteAuto",
        "BalanceRatioSelector",
        "BalanceRatio",
    }
    missing = sorted(required - set(nodes))
    unavailable = sorted(
        name for name in required if name in nodes and not nodes[name].get("available", True)
    )
    if missing or unavailable:
        details = ", ".join(missing + unavailable)
        raise RuntimeError(f"Required camera settings are unavailable: {details}.")
    selector_node = nodes["BalanceRatioSelector"]
    entries = tuple(str(item) for item in selector_node.get("enum_entries") or [])
    original_selector = str(selector_node.get("value") or "")
    if not entries or original_selector not in entries:
        raise RuntimeError("Balance ratio selector metadata is incomplete.")
    snapshot = CameraStateSnapshot(
        nodes=nodes,
        balance_ratios={},
        balance_selector_entries=entries,
        original_balance_selector=original_selector,
    )
    ratios: dict[str, object] = {}
    try:
        camera.update_settings([("BalanceWhiteAuto", "Off")])
        for selector in entries:
            camera.update_settings([("BalanceRatioSelector", selector)])
            ratio_nodes = _node_map(camera.settings(["BalanceRatio"]))
            ratios[selector] = ratio_nodes["BalanceRatio"].get("value")
        camera.update_settings([("BalanceRatioSelector", original_selector)])
    except BaseException:
        _restore_camera_state(camera, snapshot)
        raise
    return CameraStateSnapshot(
        nodes=nodes,
        balance_ratios=ratios,
        balance_selector_entries=entries,
        original_balance_selector=original_selector,
    )


def _restore_camera_state(camera: object, snapshot: CameraStateSnapshot) -> None:
    camera.update_settings([("BalanceWhiteAuto", "Off")])
    for selector in snapshot.balance_selector_entries:
        if selector not in snapshot.balance_ratios:
            continue
        camera.update_settings(
            [
                ("BalanceRatioSelector", selector),
                ("BalanceRatio", snapshot.balance_ratios[selector]),
            ]
        )
    camera.update_settings(
        [("BalanceRatioSelector", snapshot.original_balance_selector)]
    )
    camera.update_settings(
        [
            ("ExposureAuto", "Off"),
            ("GainAuto", "Off"),
        ]
    )
    manual: list[tuple[str, object]] = [
        ("ExposureTime", snapshot.nodes["ExposureTime"].get("value")),
        ("Gain", snapshot.nodes["Gain"].get("value")),
    ]
    black_level = snapshot.nodes.get("BlackLevel")
    if black_level and black_level.get("available", True) and black_level.get("writable", True):
        manual.append(("BlackLevel", black_level.get("value")))
    camera.update_settings(manual)
    camera.update_settings(
        [
            ("GainAuto", snapshot.nodes["GainAuto"].get("value")),
            ("ExposureAuto", snapshot.nodes["ExposureAuto"].get("value")),
            (
                "BalanceWhiteAuto",
                snapshot.nodes["BalanceWhiteAuto"].get("value"),
            ),
        ]
    )


def _apply_fixed_white_balance(camera: object, snapshot: CameraStateSnapshot) -> None:
    camera.update_settings([("BalanceWhiteAuto", "Off")])
    for selector in snapshot.balance_selector_entries:
        camera.update_settings(
            [
                ("BalanceRatioSelector", selector),
                ("BalanceRatio", snapshot.balance_ratios[selector]),
            ]
        )
    camera.update_settings(
        [("BalanceRatioSelector", snapshot.original_balance_selector)]
    )


def _run_all_modes(
    camera: object,
    snapshot: CameraStateSnapshot,
    config: ExposureDiagnosticConfig,
    output_dir: Path,
) -> dict[str, dict[str, Any]]:
    reports: dict[str, dict[str, Any]] = {}
    for mode in ("native_full", "native_exposure_only", "custom"):
        try:
            _apply_fixed_white_balance(camera, snapshot)
            if mode == "custom":
                reports[mode] = _run_custom_mode(
                    camera, snapshot, config, output_dir
                )
            else:
                reports[mode] = _run_native_mode(
                    camera, mode, snapshot, config, output_dir
                )
        except Exception as exc:
            reports[mode] = {
                "ok": False,
                "error": f"{type(exc).__name__}: {exc}",
            }
    return reports


def _run_native_mode(
    camera: object,
    mode: str,
    snapshot: CameraStateSnapshot,
    config: ExposureDiagnosticConfig,
    output_dir: Path,
) -> dict[str, Any]:
    del snapshot
    camera.update_settings(
        [
            ("ExposureAuto", "Off"),
            ("GainAuto", "Off"),
            ("Gain", config.fixed_gain_db),
        ]
    )
    if mode == "native_full":
        settings = [
            ("GainAuto", "Continuous"),
            ("ExposureAuto", "Continuous"),
        ]
    elif mode == "native_exposure_only":
        settings = [("ExposureAuto", "Continuous")]
    else:
        raise ValueError(f"Unsupported native exposure mode: {mode}.")
    response = camera.update_settings(settings)
    counter = int(response.get("frame_counter_at_completion", 0))
    counter = _discard_frames(camera, counter, config)
    started = time.monotonic()
    trace: list[dict[str, float]] = []
    converged = False
    while len(trace) < int(config.max_iterations):
        frame, array = _fresh_frame(camera, counter, config)
        counter = int(frame.counter)
        nodes = _node_map(camera.settings(["ExposureTime", "Gain"]))
        trace.append(
            {
                "elapsed_s": time.monotonic() - started,
                "frame_counter": float(counter),
                "brightness": _highlight_level(array, config),
                "exposure_us": _node_float(nodes, "ExposureTime"),
                "gain_db": _node_float(nodes, "Gain"),
            }
        )
        if _native_trace_converged(trace, config):
            converged = True
            break
        if time.monotonic() - started >= float(config.native_timeout_s):
            break
    metric_frames, final_png, counter = _capture_metric_frames(
        camera, counter, config
    )
    metrics = frame_metrics(metric_frames, config)
    final_path = output_dir / f"{mode}.png"
    final_path.write_bytes(final_png)
    final_nodes = _node_map(camera.settings(["ExposureTime", "Gain"]))
    return {
        "ok": converged,
        "converged": converged,
        "convergence_s": time.monotonic() - started,
        "final_exposure_us": _node_float(final_nodes, "ExposureTime"),
        "final_gain_db": _node_float(final_nodes, "Gain"),
        "final_frame_counter": counter,
        "final_frame": str(final_path),
        "trace": trace,
        "metrics": metrics,
    }


def _run_custom_mode(
    camera: object,
    snapshot: CameraStateSnapshot,
    config: ExposureDiagnosticConfig,
    output_dir: Path,
) -> dict[str, Any]:
    exposure_node = snapshot.nodes["ExposureTime"]
    limits = (
        _finite_float(exposure_node.get("minimum"), 1.0),
        _finite_float(exposure_node.get("maximum"), 30_000_000.0),
    )
    exposure = min(
        limits[1],
        max(limits[0], _finite_float(exposure_node.get("value"), limits[0])),
    )
    response = camera.update_settings(
        [
            ("ExposureAuto", "Off"),
            ("GainAuto", "Off"),
            ("Gain", config.fixed_gain_db),
            ("ExposureTime", exposure),
        ]
    )
    counter = int(response.get("frame_counter_at_completion", 0))
    started = time.monotonic()
    trace: list[dict[str, float]] = []
    converged_count = 0
    converged = False
    while len(trace) < int(config.max_iterations):
        counter = _discard_frames(camera, counter, config)
        frame, array = _fresh_frame(camera, counter, config)
        counter = int(frame.counter)
        measured = _highlight_level(array, config)
        trace.append(
            {
                "elapsed_s": time.monotonic() - started,
                "frame_counter": float(counter),
                "brightness": measured,
                "exposure_us": exposure,
                "gain_db": float(config.fixed_gain_db),
            }
        )
        relative_error = abs(measured - config.target_level) / config.target_level
        if relative_error <= float(config.custom_target_tolerance_fraction):
            converged_count += 1
            if converged_count >= int(config.convergence_window):
                converged = True
                break
        else:
            converged_count = 0
        exposure = next_exposure_us(exposure, measured, limits, config)
        response = camera.update_settings([("ExposureTime", exposure)])
        counter = int(response.get("frame_counter_at_completion", counter))
    metric_frames, final_png, counter = _capture_metric_frames(
        camera, counter, config
    )
    metrics = frame_metrics(metric_frames, config)
    final_path = output_dir / "custom.png"
    final_path.write_bytes(final_png)
    final_nodes = _node_map(camera.settings(["ExposureTime", "Gain"]))
    return {
        "ok": converged,
        "converged": converged,
        "convergence_s": time.monotonic() - started,
        "final_exposure_us": _node_float(final_nodes, "ExposureTime"),
        "final_gain_db": _node_float(final_nodes, "Gain"),
        "final_frame_counter": counter,
        "final_frame": str(final_path),
        "trace": trace,
        "metrics": metrics,
    }


def _apply_mode(
    camera: object,
    mode: str,
    snapshot: CameraStateSnapshot,
    config: ExposureDiagnosticConfig,
    report: Mapping[str, Any],
) -> None:
    _apply_fixed_white_balance(camera, snapshot)
    camera.update_settings(
        [
            ("ExposureAuto", "Off"),
            ("GainAuto", "Off"),
            ("Gain", config.fixed_gain_db),
        ]
    )
    if mode == "native_full":
        settings = [
            ("GainAuto", "Continuous"),
            ("ExposureAuto", "Continuous"),
        ]
    elif mode == "native_exposure_only":
        settings = [("ExposureAuto", "Continuous")]
    elif mode == "custom":
        settings = [("ExposureTime", report.get("final_exposure_us"))]
    else:
        raise ValueError(f"Unsupported exposure mode: {mode}.")
    camera.update_settings(settings)


def _discard_frames(camera: object, counter: int, config: ExposureDiagnosticConfig) -> int:
    current = int(counter)
    for _index in range(max(0, int(config.settling_frames))):
        frame = camera.frame(
            space="raw",
            after_counter=current,
            timeout_ms=int(config.frame_timeout_ms),
        )
        current = int(frame.counter)
    return current


def _fresh_frame(
    camera: object,
    counter: int,
    config: ExposureDiagnosticConfig,
) -> tuple[object, np.ndarray]:
    frame = camera.frame(
        space="raw",
        after_counter=int(counter),
        timeout_ms=int(config.frame_timeout_ms),
    )
    return frame, _decode_png(frame.data)


def _capture_metric_frames(
    camera: object,
    counter: int,
    config: ExposureDiagnosticConfig,
) -> tuple[list[np.ndarray], bytes, int]:
    arrays: list[np.ndarray] = []
    final_png = b""
    current = int(counter)
    for _index in range(max(2, int(config.metric_frame_count))):
        frame, array = _fresh_frame(camera, current, config)
        current = int(frame.counter)
        arrays.append(array)
        final_png = bytes(frame.data)
    return arrays, final_png, current


def _decode_png(data: bytes) -> np.ndarray:
    encoded = np.frombuffer(bytes(data), dtype=np.uint8)
    bgr = cv2.imdecode(encoded, cv2.IMREAD_COLOR)
    if bgr is None:
        raise RuntimeError("Camera frame is not a decodable PNG.")
    return cv2.cvtColor(bgr, cv2.COLOR_BGR2RGB)


def _highlight_level(frame: np.ndarray, config: ExposureDiagnosticConfig) -> float:
    return float(
        np.percentile(
            np.max(np.asarray(frame), axis=2),
            float(config.target_percentile),
        )
    )


def _native_trace_converged(
    trace: Sequence[Mapping[str, float]],
    config: ExposureDiagnosticConfig,
) -> bool:
    window_size = max(2, int(config.convergence_window))
    if len(trace) < window_size:
        return False
    window = trace[-window_size:]
    brightness = np.asarray([item["brightness"] for item in window], dtype=float)
    exposure = np.asarray([item["exposure_us"] for item in window], dtype=float)
    gain = np.asarray([item["gain_db"] for item in window], dtype=float)
    brightness_stable = np.ptp(brightness) <= (
        max(float(np.mean(brightness)), 1.0) * config.brightness_tolerance_fraction
    )
    exposure_stable = np.ptp(exposure) <= (
        max(float(np.mean(exposure)), 1.0) * config.exposure_tolerance_fraction
    )
    gain_stable = np.ptp(gain) <= float(config.gain_tolerance_db)
    return bool(brightness_stable and exposure_stable and gain_stable)


def _node_map(response: Mapping[str, Any]) -> dict[str, dict[str, Any]]:
    nodes = response.get("nodes")
    if not isinstance(nodes, list):
        raise RuntimeError("Camera settings response has no node list.")
    result = {
        str(node.get("name") or ""): dict(node)
        for node in nodes
        if isinstance(node, Mapping) and str(node.get("name") or "")
    }
    return result


def _node_float(nodes: Mapping[str, Mapping[str, Any]], name: str) -> float:
    if name not in nodes:
        raise RuntimeError(f"Camera settings response omitted {name}.")
    return _finite_float(nodes[name].get("value"), math.nan)


def _finite_float(value: object, default: float) -> float:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return float(default)
    return number if math.isfinite(number) else float(default)


def _diagnostic_output_dir(output_dir: str | Path | None) -> Path:
    if output_dir is not None:
        return Path(output_dir).expanduser().resolve()
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    return Path(__file__).resolve().parents[2] / ".scratch" / f"camera-auto-exposure-{stamp}"


def _write_report(output_dir: Path, report: Mapping[str, Any]) -> None:
    (output_dir / "report.json").write_text(
        json.dumps(report, indent=2, ensure_ascii=False, allow_nan=False),
        encoding="utf-8",
    )
    lines = [
        "# Camera Auto-Exposure Diagnostic",
        "",
        f"Preferred mode: {report.get('preferred_mode') or 'none'}",
        f"Restored: {bool(report.get('restored'))}",
        f"Applied winner: {bool(report.get('applied_winner'))}",
        "",
        "| Mode | OK | Saturated | Silicon SNR | Drift | Exposure (us) | Gain (dB) |",
        "|---|---:|---:|---:|---:|---:|---:|",
    ]
    modes = report.get("modes")
    if isinstance(modes, Mapping):
        for name, raw_mode in modes.items():
            mode = raw_mode if isinstance(raw_mode, Mapping) else {}
            metrics = mode.get("metrics")
            metrics = metrics if isinstance(metrics, Mapping) else {}
            lines.append(
                "| {name} | {ok} | {sat:.6f} | {snr:.3f} | {drift:.6f} | {exp:.2f} | {gain:.3f} |".format(
                    name=name,
                    ok=bool(mode.get("ok")),
                    sat=_finite_float(metrics.get("saturated_fraction"), math.nan),
                    snr=_finite_float(metrics.get("silicon_snr"), math.nan),
                    drift=_finite_float(metrics.get("temporal_brightness_drift"), math.nan),
                    exp=_finite_float(mode.get("final_exposure_us"), math.nan),
                    gain=_finite_float(mode.get("final_gain_db"), math.nan),
                )
            )
    if report.get("error"):
        lines.extend(["", f"Error: {report['error']}"])
    if report.get("restore_error"):
        lines.extend(["", f"Restore error: {report['restore_error']}"])
    (output_dir / "summary.md").write_text("\n".join(lines) + "\n", encoding="utf-8")


def _argument_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--base-url",
        default=os.environ.get("PROBE_STATION_API_URL", "http://127.0.0.1:8765"),
    )
    parser.add_argument("--api-key", default=None)
    parser.add_argument("--output-dir", type=Path, default=None)
    parser.add_argument("--apply-winner", action="store_true")
    parser.add_argument("--target-percentile", type=float, default=99.5)
    parser.add_argument("--target-level", type=float, default=235.0)
    parser.add_argument("--max-saturated-fraction", type=float, default=0.001)
    parser.add_argument("--settling-frames", type=int, default=2)
    parser.add_argument("--metric-frame-count", type=int, default=8)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = _argument_parser().parse_args(argv)
    config = ExposureDiagnosticConfig(
        target_percentile=args.target_percentile,
        target_level=args.target_level,
        max_saturated_fraction=args.max_saturated_fraction,
        settling_frames=args.settling_frames,
        metric_frame_count=args.metric_frame_count,
    )
    client = ProbeStationClient(base_url=args.base_url, api_key=args.api_key)
    report = run_exposure_diagnostic(
        client.camera,
        config=config,
        output_dir=args.output_dir,
        apply_winner=args.apply_winner,
    )
    print(f"Report: {report['output_dir']}")
    print(f"Preferred mode: {report.get('preferred_mode') or 'none'}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())


__all__ = [
    "ExposureDiagnosticConfig",
    "choose_preferred_mode",
    "frame_metrics",
    "next_exposure_us",
    "run_exposure_diagnostic",
]
