from __future__ import annotations

import tempfile
from pathlib import Path
from unittest.mock import patch

import numpy as np
import pytest
import cv2

from probe_station_client import CameraFrame

from probe_station_gui.camera.exposure_diagnostic import (
    ExposureDiagnosticConfig,
    choose_preferred_mode,
    frame_metrics,
    next_exposure_us,
    run_exposure_diagnostic,
)


def test_next_exposure_uses_bounded_multiplicative_update() -> None:
    config = ExposureDiagnosticConfig(
        target_level=235.0,
        min_step_ratio=0.5,
        max_step_ratio=2.0,
        clipping_reduction_ratio=0.5,
    )

    increased = next_exposure_us(1000.0, 100.0, (100.0, 5000.0), config)
    clipped = next_exposure_us(1000.0, 255.0, (100.0, 5000.0), config)
    limited = next_exposure_us(4000.0, 100.0, (100.0, 5000.0), config)

    assert increased == 2000.0
    assert clipped == 500.0
    assert limited == 5000.0


def test_frame_metrics_measure_clipping_drift_and_silicon_noise() -> None:
    stable_frames = []
    noisy_frames = []
    rng = np.random.default_rng(3)
    for index in range(6):
        stable = np.full((40, 60, 3), 60 + index % 2, dtype=np.uint8)
        stable[:8, :, :] = 230
        stable_frames.append(stable)
        noise = rng.normal(0.0, 8.0, stable.shape)
        noisy_frames.append(np.clip(stable.astype(np.float32) + noise, 0, 255).astype(np.uint8))

    stable = frame_metrics(stable_frames)
    noisy = frame_metrics(noisy_frames)

    assert stable["saturated_fraction"] == 0.0
    assert stable["highlight_percentile"] >= 229.0
    assert stable["temporal_brightness_drift"] < 0.01
    assert stable["silicon_temporal_noise"] < noisy["silicon_temporal_noise"]
    assert stable["silicon_snr"] > noisy["silicon_snr"]


def test_choose_preferred_mode_requires_clip_compliance_then_uses_snr() -> None:
    config = ExposureDiagnosticConfig(max_saturated_fraction=0.001)
    reports = {
        "native_full": {
            "ok": True,
            "metrics": {
                "saturated_fraction": 0.002,
                "silicon_snr": 30.0,
                "temporal_brightness_drift": 0.001,
            },
            "convergence_s": 0.5,
        },
        "native_exposure_only": {
            "ok": True,
            "metrics": {
                "saturated_fraction": 0.0005,
                "silicon_snr": 12.0,
                "temporal_brightness_drift": 0.003,
            },
            "convergence_s": 1.0,
        },
        "custom": {
            "ok": True,
            "metrics": {
                "saturated_fraction": 0.0005,
                "silicon_snr": 18.0,
                "temporal_brightness_drift": 0.004,
            },
            "convergence_s": 2.0,
        },
    }

    assert choose_preferred_mode(reports, config) == "custom"
    reports["custom"]["metrics"]["saturated_fraction"] = 0.01
    reports["native_exposure_only"]["metrics"]["saturated_fraction"] = 0.01
    assert choose_preferred_mode(reports, config) is None


def test_run_restores_original_settings_and_balance_ratios_after_failure() -> None:
    camera = FakeCameraClient()
    original_state = dict(camera.state)
    original_ratios = dict(camera.balance_ratios)
    with tempfile.TemporaryDirectory() as tmpdir:
        with patch(
            "probe_station_gui.camera.exposure_diagnostic._run_all_modes",
            side_effect=RuntimeError("interrupted"),
        ):
            with pytest.raises(RuntimeError, match="interrupted"):
                run_exposure_diagnostic(
                    camera,
                    config=ExposureDiagnosticConfig(settling_frames=0),
                    output_dir=Path(tmpdir),
                )

    assert camera.state == original_state
    assert camera.balance_ratios == original_ratios


def test_full_diagnostic_uses_valid_batches_and_writes_all_mode_frames() -> None:
    camera = SyntheticCameraClient()
    with tempfile.TemporaryDirectory() as tmpdir:
        report = run_exposure_diagnostic(
            camera,
            config=ExposureDiagnosticConfig(
                settling_frames=0,
                convergence_window=2,
                max_iterations=8,
                native_timeout_s=2.0,
                metric_frame_count=2,
            ),
            output_dir=Path(tmpdir),
        )

        assert set(report["modes"]) == {
            "native_full",
            "native_exposure_only",
            "custom",
        }
        assert all(mode["ok"] for mode in report["modes"].values())
        assert (Path(tmpdir) / "native_full.png").exists()
        assert (Path(tmpdir) / "native_exposure_only.png").exists()
        assert (Path(tmpdir) / "custom.png").exists()
        assert (Path(tmpdir) / "report.json").exists()
        assert report["restored"] is True


class FakeCameraClient:
    def __init__(self) -> None:
        self.state = {
            "ExposureAuto": "Continuous",
            "ExposureTime": "3076.14",
            "GainAuto": "Continuous",
            "Gain": "1.5",
            "BlackLevel": "0.25",
            "BalanceWhiteAuto": "Continuous",
            "BalanceRatioSelector": "Red",
        }
        self.balance_ratios = {"Red": "1.25", "Blue": "1.75"}
        self.counter = 10
        self.calls: list[list[tuple[str, object]]] = []

    def settings(self, names=None):
        requested = list(names or self.state)
        nodes = []
        for name in requested:
            value = (
                self.balance_ratios[self.state["BalanceRatioSelector"]]
                if name == "BalanceRatio"
                else self.state[name]
            )
            node = {
                "name": name,
                "value": value,
                "available": True,
                "readable": True,
                "writable": True,
                "enum_entries": [],
            }
            if name == "BalanceRatioSelector":
                node["enum_entries"] = ["Red", "Blue"]
            if name == "ExposureTime":
                node["minimum"] = 100.0
                node["maximum"] = 10000.0
            nodes.append(node)
        return {
            "accepted": True,
            "nodes": nodes,
            "frame_counter_at_completion": self.counter,
        }

    def update_settings(self, settings):
        ordered = list(settings)
        names = [name for name, _value in ordered]
        if len(names) != len(set(names)):
            raise RuntimeError("duplicate camera setting")
        self.calls.append(ordered)
        for name, value in ordered:
            if name == "BalanceRatio":
                selector = self.state["BalanceRatioSelector"]
                self.balance_ratios[selector] = str(value)
            else:
                self.state[name] = str(value)
        self.counter += 1
        return {
            "accepted": True,
            "frame_counter_at_completion": self.counter,
            "nodes": [],
        }


class SyntheticCameraClient(FakeCameraClient):
    def update_settings(self, settings):
        response = super().update_settings(settings)
        ordered = list(settings)
        for name, value in ordered:
            if name == "GainAuto" and value == "Continuous":
                self.state["Gain"] = "1.0"
            if name == "ExposureAuto" and value == "Continuous":
                self.state["ExposureTime"] = "2000.0"
        return response

    def frame(self, *, space, after_counter, timeout_ms):
        del timeout_ms
        assert space == "raw"
        self.counter = max(self.counter + 1, int(after_counter) + 1)
        exposure = float(self.state["ExposureTime"])
        highlight = int(np.clip(exposure / 2000.0 * 235.0, 0.0, 255.0))
        image = np.full((40, 60, 3), 60, dtype=np.uint8)
        image[:8, :, :] = highlight
        ok, encoded = cv2.imencode(".png", cv2.cvtColor(image, cv2.COLOR_RGB2BGR))
        assert ok
        return CameraFrame(
            data=encoded.tobytes(),
            counter=self.counter,
            space="raw",
            width=60,
            height=40,
        )
