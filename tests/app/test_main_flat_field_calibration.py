from __future__ import annotations

from types import SimpleNamespace

from main import Main
from probe_station_gui.camera.optical_calibration_adapters import (
    OpticalCalibrationRequestAdapter,
)
from probe_station_gui.camera.optical_calibration_runtime import CalibrationStartDecision


def test_flat_start_captures_request_without_stage_or_camera_work() -> None:
    captured = []
    window = Main.__new__(Main)
    window._optical_calibration_runtime = SimpleNamespace(
        state=lambda: SimpleNamespace(active_run_id=None),
        start_flat=lambda request: (
            captured.append(request)
            or CalibrationStartDecision(True, 202, "started", request.run_id)
        ),
    )
    window._stage_serial_ready = lambda: True
    window.stage_controller = SimpleNamespace(is_busy=lambda: False)
    window._active_objective_metadata = lambda: ("X20", 20.0)
    window._active_microscope_scale = lambda: SimpleNamespace(
        pixels_to_mm=[[-0.001, 0.0], [0.0, -0.002]],
        pixel_size_x_mm=0.001,
        pixel_size_y_mm=0.002,
    )
    window._optical_calibration_request_adapter = OpticalCalibrationRequestAdapter(
        window._active_objective_metadata,
        window._active_microscope_scale,
    )
    window._coordinate_feedrate_for_axes = lambda _axes: 120.0
    window._current_needle_feedrate = lambda: 70.0
    window._show_status = lambda *_args: None

    assert Main._start_flat_field_calibration(window, wizard_run_id=17) is True

    assert len(captured) == 1
    assert captured[0].objective_name == "X20"
    assert captured[0].pixels_to_mm == ((-0.001, 0.0), (0.0, -0.002))
    assert captured[0].linear_feedrate == 120.0


def test_disconnected_preflight_does_not_query_stage_busy_state() -> None:
    window = Main.__new__(Main)
    window._optical_calibration_runtime = SimpleNamespace(
        state=lambda: SimpleNamespace(active_run_id=None)
    )
    window._stage_serial_ready = lambda: False
    window.stage_controller = SimpleNamespace(
        is_busy=lambda: (_ for _ in ()).throw(
            AssertionError("disconnected stage busy state queried")
        )
    )

    assert Main._optical_calibration_preflight(window, "flat") == (
        "Connect the stage controller before calibration."
    )
