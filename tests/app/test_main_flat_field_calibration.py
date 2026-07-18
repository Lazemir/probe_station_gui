from __future__ import annotations

from types import SimpleNamespace

from PySide6.QtGui import QColor, QImage

from main import Main


class _FakeStage:
    def __init__(self, events: list[tuple[object, ...]]) -> None:
        self.events = events

    def begin_external_task(self, label: str) -> None:
        self.events.append(("begin", label))

    def run_external_needles_action(self, action: str, feedrate: float) -> None:
        self.events.append(("needles", action, feedrate))

    def run_external_move_to_xy(
        self,
        x_mm: float,
        y_mm: float,
        *,
        feedrate: float | None = None,
    ) -> None:
        self.events.append(("move", x_mm, y_mm, feedrate))

    def finish_external_task(self) -> None:
        self.events.append(("finish",))


class _FakeStore:
    def __init__(self, events: list[tuple[object, ...]]) -> None:
        self.events = events
        self.calls: list[dict[str, object]] = []

    def install(self, objective_name, frames, **kwargs):
        call = {
            "objective_name": objective_name,
            "frames": tuple(frames),
            **kwargs,
        }
        self.calls.append(call)
        self.events.append(("install", objective_name, len(frames)))
        return SimpleNamespace(
            current_manifest="C:/config/calibrations/flat-field/X20/current.json"
        )


def _frame() -> QImage:
    frame = QImage(100, 50, QImage.Format_RGB32)
    frame.fill(QColor(100, 110, 120))
    return frame


def test_flat_field_capture_offsets_form_shifted_three_by_three() -> None:
    scale = SimpleNamespace(pixel_size_x_mm=0.001, pixel_size_y_mm=0.002)

    offsets = Main._flat_field_capture_offsets_mm((100, 50), scale)

    assert len(offsets) == 9
    assert offsets[0] == (0.0, 0.0)
    assert {round(x, 9) for x, _y in offsets} == {-0.02, 0.0, 0.02}
    assert {round(y, 9) for _x, y in offsets} == {-0.02, 0.0, 0.02}


def test_flat_field_runner_captures_raw_grid_and_restores_stage(monkeypatch) -> None:
    events: list[tuple[object, ...]] = []
    finished: list[tuple[bool, str, object]] = []
    store = _FakeStore(events)
    stage = _FakeStage(events)
    frames = [_frame() for _ in range(10)]
    frame_counter = 0

    def wait_for_frame(*, after_counter=None, timeout_s=2.0):
        nonlocal frame_counter
        frame_counter += 1
        events.append(("frame", after_counter, timeout_s))
        return frames.pop(0), frame_counter

    window = Main.__new__(Main)
    window.stage_controller = stage
    window._flat_field_calibration_store = store
    window._active_microscope_scale = lambda: SimpleNamespace(
        pixel_size_x_mm=0.001,
        pixel_size_y_mm=0.002,
    )
    window._active_objective_metadata = lambda: ("X20", 20.0)
    window._run_camera_auto_exposure = lambda: (
        events.append(("auto_exposure",))
        or {
            "accepted": True,
            "converged": True,
            "final_exposure_us": 4000.0,
            "final_gain_db": 0.0,
        }
    )
    window._wait_for_raw_camera_frame = wait_for_frame
    window._latest_raw_camera_counter = lambda: frame_counter
    window._apply_microscope_scan_camera_lock = lambda settings: (
        events.append(("camera_lock", settings.enabled, settings.settings)) or "lock-key"
    )
    window._restore_microscope_scan_camera_lock = lambda key: (
        events.append(("camera_restore", key)) or ""
    )
    window._show_status = lambda message, _timeout_ms=0: events.append(
        ("status", message)
    )
    window._emit_flat_field_calibration_finished = (
        lambda success, message, payload: finished.append((success, message, payload))
    )
    monkeypatch.setattr("main.time.sleep", lambda _seconds: None)

    Main._run_flat_field_calibration(window, (10.0, 20.0), 120.0, 70.0)

    assert events.index(("begin", "flat-field calibration")) < events.index(
        ("auto_exposure",)
    )
    assert events.index(("camera_lock", True, (
        ("ExposureAuto", "Off"),
        ("GainAuto", "Off"),
        ("BalanceWhiteAuto", "Off"),
    ))) > events.index(("begin", "flat-field calibration"))
    capture_moves = [event for event in events if event[0] == "move"]
    assert len(capture_moves) == 10
    assert capture_moves[0][1:3] == (10.0, 20.0)
    assert capture_moves[-1] == ("move", 10.0, 20.0, 120.0)
    assert len(store.calls) == 1
    assert store.calls[0]["objective_name"] == "X20"
    assert len(store.calls[0]["frames"]) == 9
    assert "blur_radius_px" not in store.calls[0]
    assert "max_gain" not in store.calls[0]
    metadata = store.calls[0]["metadata"]
    assert metadata["capture_grid"] == [3, 3]
    assert metadata["overlap_fraction"] == 0.8
    assert metadata["auto_exposure"]["final_exposure_us"] == 4000.0
    assert events[-3:] == [
        ("move", 10.0, 20.0, 120.0),
        ("camera_restore", "lock-key"),
        ("finish",),
    ]
    assert finished[0][0] is True
    assert "saved" in finished[0][1].lower()


def test_flat_field_runner_does_not_move_when_auto_exposure_fails() -> None:
    events: list[tuple[object, ...]] = []
    finished: list[tuple[bool, str, object]] = []
    window = Main.__new__(Main)
    window.stage_controller = _FakeStage(events)
    window._flat_field_calibration_store = _FakeStore(events)
    window._active_microscope_scale = lambda: SimpleNamespace(
        pixel_size_x_mm=0.001,
        pixel_size_y_mm=0.001,
    )
    window._active_objective_metadata = lambda: ("X20", 20.0)
    window._run_camera_auto_exposure = lambda: {
        "accepted": False,
        "message": "Exposure did not converge.",
    }
    window._show_status = lambda *_args: None
    window._emit_flat_field_calibration_finished = (
        lambda success, message, payload: finished.append((success, message, payload))
    )

    Main._run_flat_field_calibration(window, (10.0, 20.0), 120.0, 70.0)

    assert not any(event[0] == "move" for event in events)
    assert events == [
        ("begin", "flat-field calibration"),
        ("finish",),
    ]
    assert finished == [(False, "Flat-field calibration failed: Exposure did not converge.", None)]
