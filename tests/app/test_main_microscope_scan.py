from __future__ import annotations

import types

import pytest
from PySide6.QtGui import QColor, QImage

from main import Main
from probe_station_gui.camera import microscope_scan
from probe_station_gui.camera.imaging import MicroscopeScaleCalibration
from probe_station_gui.camera.microscope_scan_runtime_adapters import (
    build_microscope_scan_plan,
)
from probe_station_gui.design.model import DesignRegistration
from probe_station_gui.coordinates.provenance import (
    RUNTIME_PROVENANCE_REASON,
    RUNTIME_PROVENANCE_STATUS,
)
from probe_station_gui.coordinates.transforms import BFrameTransform


class _FakeScanThread:
    def __init__(self, *, target, args, name, daemon):
        self.target = target
        self.args = args
        self.name = name
        self.daemon = daemon
        self.started = False

    def start(self) -> None:
        self.started = True


def _tile_signature(tiles):
    return tuple(
        (tile.index, tile.row, tile.column, tile.stage_xy, tile.label)
        for tile in tiles
    )


def _set_area_scan_launch_sources(window: Main, scale: object) -> None:
    window._active_microscope_scale = lambda: scale
    window._active_objective_metadata = lambda: ("X20", 20.0)
    window._active_objective_xy_offset = lambda: (0.0, 0.0)
    window._design_session = types.SimpleNamespace(document=None, registration=None)


def _install_usable_design_frame(
    window: Main,
    registration: DesignRegistration,
) -> None:
    frame_usability = types.SimpleNamespace(
        usable=True,
        rejection_reason=None,
        frame_id="design-a",
        frame_version=4,
    )
    window._snapshot_active_design_frame_usability = lambda: frame_usability
    window._design_frame_usability_snapshot_is_current = (
        lambda snapshot: snapshot is frame_usability
    )
    window._camera_stage_xy_from_design_usability_snapshot = (
        lambda snapshot, point, *, require_current: (
            registration.design_to_stage(point)
            if snapshot is frame_usability and not require_current
            else None
        )
    )


def test_microscope_scan_disconnected_stage_does_not_read_scale_or_camera() -> None:
    window = Main.__new__(Main)
    statuses: list[tuple[str, int]] = []
    calls: list[str] = []
    window.serial_connection = None
    window._microscope_scan_running = lambda: False
    window._show_status = lambda message, timeout_ms=0: statuses.append(
        (message, timeout_ms)
    )
    window._active_microscope_scale = lambda: calls.append("scale")
    window._wait_for_camera_frame = lambda **_kwargs: calls.append("camera")

    Main._start_microscope_scan(window, types.SimpleNamespace(overlap_fraction=0.0))

    assert statuses == [("Connect the stage controller before scanning.", 5000)]
    assert calls == []


def test_microscope_scan_missing_design_does_not_read_scale_or_camera() -> None:
    window = Main.__new__(Main)
    statuses: list[tuple[str, int]] = []
    calls: list[str] = []
    window.serial_connection = types.SimpleNamespace(is_open=True)
    window._design_session = types.SimpleNamespace(document=None, registration=None)
    window._microscope_scan_running = lambda: False
    window._show_status = lambda message, timeout_ms=0: statuses.append(
        (message, timeout_ms)
    )
    window._active_microscope_scale = lambda: calls.append("scale")
    window._wait_for_camera_frame = lambda **_kwargs: calls.append("camera")

    Main._start_microscope_scan(window, types.SimpleNamespace(overlap_fraction=0.0))

    assert statuses == [("Load a design before scanning.", 5000)]
    assert calls == []


def test_design_scan_rejects_verified_missing_reference_draft_before_planning() -> None:
    design_kind = Main._active_design_frame_provenance_error.__globals__[
        "design_frame_provenance_error"
    ].__globals__["FrameKind"].DESIGN
    record = types.SimpleNamespace(
        frame_id="design-a",
        version=4,
        kind=design_kind,
        transform=BFrameTransform.identity(),
        readiness={
            axis: types.SimpleNamespace(
                available=False,
                reason=f"Design {axis} reference is not registered.",
            )
            for axis in ("X", "Y", "Z", "A", "B")
        },
        metadata={
            RUNTIME_PROVENANCE_STATUS: "verified",
            RUNTIME_PROVENANCE_REASON: "",
        },
    )
    window = Main.__new__(Main)
    statuses: list[tuple[str, int]] = []
    calls: list[str] = []
    window.serial_connection = types.SimpleNamespace(is_open=True)
    window._coordinate_frames_loaded = True
    window._coordinate_frame_authority_blocked_axes = set()
    window._coordinate_frame_registry = types.SimpleNamespace(
        get=lambda _frame_id: record
    )
    window._design_session = types.SimpleNamespace(
        active_frame_id="design-a",
        document=types.SimpleNamespace(bounds=(0.0, 0.0, 1.0, 1.0)),
        registration=types.SimpleNamespace(
            valid=True,
            matrix=((1.0, 0.0), (0.0, 1.0)),
            offset=(0.0, 0.0),
        ),
    )
    window._microscope_scan_running = lambda: False
    window._show_status = lambda message, timeout_ms=0: statuses.append(
        (str(message), int(timeout_ms))
    )
    window._active_microscope_scale = lambda: calls.append("scale")

    Main._start_microscope_scan(window, types.SimpleNamespace(overlap_fraction=0.0))

    assert calls == []
    assert statuses


def test_design_scan_entry_starts_worker_without_waiting_for_camera(
    monkeypatch,
) -> None:
    created_threads: list[_FakeScanThread] = []
    monkeypatch.setattr(
        Main._start_microscope_scan.__globals__["threading"],
        "Thread",
        lambda **kwargs: created_threads.append(_FakeScanThread(**kwargs))
        or created_threads[-1],
    )
    window = Main.__new__(Main)
    window.serial_connection = types.SimpleNamespace(is_open=True)
    registration = DesignRegistration.from_marks(
        ((0.0, 0.0), (1.0, 0.0)),
        ((0.0, 0.0), (1.0, 0.0)),
    )
    window._design_session = types.SimpleNamespace(
        document=types.SimpleNamespace(bounds=(0.0, 0.0, 1.0, 1.0)),
        registration=registration,
    )
    _install_usable_design_frame(window, registration)
    window._microscope_scan_running = lambda: False
    window._active_microscope_scale = lambda: types.SimpleNamespace(
        pixel_size_x_mm=0.001,
        pixel_size_y_mm=0.001,
    )
    window._active_objective_metadata = lambda: ("X20", 20.0)
    window._active_objective_xy_offset = lambda: (0.0, 0.0)
    window._wait_for_camera_frame = lambda **_kwargs: pytest.fail(
        "GUI entry path must not wait for a camera frame"
    )
    window._microscope_scan_stop_requested = types.SimpleNamespace(clear=lambda: None)
    window.microscope_scan_dialog = None
    window._update_stage_coordinate_apply_state = lambda: None
    window._show_status = lambda *_args: None

    Main._start_microscope_scan(
        window,
        types.SimpleNamespace(overlap_fraction=0.25),
    )

    assert len(created_threads) == 1
    assert created_threads[0].started is True
    assert created_threads[0].name == "MicroscopeDesignScan"


def test_design_scan_plan_uses_launch_registration_and_objective_offset_snapshot(
    monkeypatch,
) -> None:
    created_threads: list[_FakeScanThread] = []
    monkeypatch.setattr(
        Main._start_microscope_scan.__globals__["threading"],
        "Thread",
        lambda **kwargs: created_threads.append(_FakeScanThread(**kwargs))
        or created_threads[-1],
    )
    document = types.SimpleNamespace(bounds=(0.0, 0.0, 0.2, 0.2))
    launch_registration = DesignRegistration.from_marks(
        ((0.0, 0.0), (1.0, 0.0)),
        ((10.0, 20.0), (11.0, 20.0)),
    )
    scale = MicroscopeScaleCalibration(1.0, 1.0)
    objective_state = {"offset": (0.5, -0.25)}
    window = Main.__new__(Main)
    window.serial_connection = types.SimpleNamespace(is_open=True)
    window._design_session = types.SimpleNamespace(
        document=document,
        registration=launch_registration,
    )
    _install_usable_design_frame(window, launch_registration)
    window._microscope_scan_running = lambda: False
    window._active_microscope_scale = lambda: scale
    window._active_objective_metadata = lambda: ("X20", 20.0)
    window._active_objective_xy_offset = lambda: objective_state["offset"]
    window._microscope_scan_stop_requested = types.SimpleNamespace(clear=lambda: None)
    window.microscope_scan_dialog = None
    window._update_stage_coordinate_apply_state = lambda: None
    window._show_status = lambda *_args: None

    Main._start_microscope_scan(window, types.SimpleNamespace(overlap_fraction=0.0))
    planning_request = created_threads[0].args[1]
    launch_snapshot = created_threads[0].args[2]
    window._design_session.registration = DesignRegistration.from_marks(
        ((0.0, 0.0), (1.0, 0.0)),
        ((100.0, 200.0), (102.0, 200.0)),
    )
    objective_state["offset"] = (50.0, 75.0)

    actual_plan = build_microscope_scan_plan(
        planning_request,
        (100, 100),
        launch=launch_snapshot,
    )
    expected = microscope_scan.scan_plan_decision(
        document=document,
        scale=scale,
        frame_size_px=(100, 100),
        overlap_fraction=0.0,
        design_to_stage_xy=lambda point: tuple(
            value + offset
            for value, offset in zip(
                launch_registration.design_to_stage(point),
                (0.5, -0.25),
            )
        ),
    )

    assert expected.plan is not None
    assert _tile_signature(actual_plan.tiles) == _tile_signature(expected.plan.tiles)


def test_area_scan_thread_start_failure_clears_unstarted_state(monkeypatch) -> None:
    class _StartFailThread(_FakeScanThread):
        def start(self) -> None:
            raise RuntimeError("thread start failed")

    monkeypatch.setattr(
        Main._api_microscope_area_scan.__globals__["threading"],
        "Thread",
        lambda **kwargs: _StartFailThread(**kwargs),
    )
    window = Main.__new__(Main)
    window.stage_controller = types.SimpleNamespace(is_busy=lambda: False)
    window._microscope_scan_running = lambda: False
    window._stage_serial_ready = lambda: True
    _set_area_scan_launch_sources(
        window,
        types.SimpleNamespace(pixels_to_mm=((0.001, 0.0), (0.0, -0.001))),
    )
    window._microscope_area_scan_default_output_dir = lambda: "C:/scan"
    window._microscope_scan_stop_requested = types.SimpleNamespace(clear=lambda: None)
    window._update_stage_coordinate_apply_state = lambda: None
    window._microscope_scan_thread = None

    response = Main._api_microscope_area_scan(
        window,
        {"rows": 2, "columns": 2, "flat_field": False},
    )

    assert response["accepted"] is False
    assert "thread start failed" in response["message"]
    assert window._microscope_scan_thread is None


def test_microscope_scan_finished_updates_thread_and_dialog_presentation() -> None:
    events: list[object] = []
    thread = types.SimpleNamespace(
        is_alive=lambda: False,
        join=lambda **kwargs: events.append(("join", kwargs)),
    )
    dialog = types.SimpleNamespace(
        set_running=lambda value: events.append(("running", value)),
        set_status=lambda message: events.append(("dialog", message)),
    )
    window = Main.__new__(Main)
    window._microscope_scan_thread = thread
    window._microscope_scan_stop_requested = types.SimpleNamespace(
        clear=lambda: events.append("clear")
    )
    window._update_stage_coordinate_apply_state = lambda: events.append("update")
    window.microscope_scan_dialog = dialog
    window._show_status = lambda message, timeout: events.append(
        ("status", message, timeout)
    )

    Main._on_microscope_scan_finished(window, True, "Scan complete.")

    assert window._microscope_scan_thread is None
    assert events == [
        ("join", {"timeout": 0.1}),
        "clear",
        "update",
        ("running", False),
        ("dialog", "Scan complete."),
        ("status", "Scan complete.", 10000),
    ]


def test_stage_position_metadata_prefers_frame_stage_xy() -> None:
    window = Main.__new__(Main)
    window.stage_controller = types.SimpleNamespace(
        latest_stage_position=lambda: (1.0, 2.0, 3.0, 4.0)
    )

    position = Main._stage_position_for_image_metadata(window, stage_xy=(10.0, 20.0))

    assert position == (10.0, 20.0, 3.0, 4.0)


def test_camera_auto_exposure_frame_reader_uses_fresh_raw_frame() -> None:
    window = Main.__new__(Main)
    image = QImage(8, 6, QImage.Format_RGB32)
    image.fill(QColor(12, 34, 56))
    calls: list[dict[str, object]] = []
    window._wait_for_raw_camera_frame = (
        lambda **kwargs: calls.append(kwargs) or (image, 18)
    )

    frame = Main._read_camera_auto_exposure_frame(window, 17, 1.25)

    assert calls == [{"after_counter": 17, "timeout_s": 1.25}]
    assert frame.counter == 18
    assert frame.rgb.shape == (6, 8, 3)
    assert frame.rgb.flags.owndata is True
    assert frame.rgb[0, 0].tolist() == [12, 34, 56]


def _area_scan_window() -> Main:
    window = Main.__new__(Main)
    window.stage_controller = types.SimpleNamespace(
        is_busy=lambda: False,
        latest_stage_position=lambda: (10.0, 20.0, 3.0),
    )
    window._microscope_scan_running = lambda: False
    window._stage_serial_ready = lambda: True
    _set_area_scan_launch_sources(
        window,
        types.SimpleNamespace(pixels_to_mm=((0.001, 0.0), (0.0, -0.001))),
    )
    window._microscope_area_scan_default_output_dir = lambda: "C:/scan"
    window._microscope_scan_stop_requested = types.SimpleNamespace(clear=lambda: None)
    window._update_stage_coordinate_apply_state = lambda: None
    return window


def test_api_microscope_area_scan_builds_stitch_debug_plan(monkeypatch) -> None:
    created_threads: list[_FakeScanThread] = []
    monkeypatch.setattr(
        "main.threading.Thread",
        lambda **kwargs: created_threads.append(_FakeScanThread(**kwargs))
        or created_threads[-1],
    )
    window = _area_scan_window()

    response = Main._api_microscope_area_scan(
        window,
        {"pattern": "stitch_debug", "structure_size_mm": 0.4, "flat_field": False},
    )

    assert response["accepted"] is True
    assert response["pattern"] == "stitch_debug"
    configuration, planning_request, launch_snapshot = created_threads[0].args
    assert configuration.refine_scale_from_overlaps is False
    plan = build_microscope_scan_plan(
        planning_request,
        (1000, 800),
        launch=launch_snapshot,
        center_stage_xy=(10.0, 20.0),
    )
    expected = microscope_scan.stitch_debug_scan_plan_from_pixel_matrix(
        center_stage_xy=(10.0, 20.0),
        frame_size_px=(1000, 800),
        pixels_to_mm=((0.001, 0.0), (0.0, -0.001)),
        structure_size_mm=0.4,
        placement_fraction=1.0,
        overlap_fraction=0.25,
    )
    assert _tile_signature(plan.tiles) == _tile_signature(expected.tiles)


def test_api_microscope_area_scan_defaults_to_large_area_overlap(monkeypatch) -> None:
    created_threads: list[_FakeScanThread] = []
    monkeypatch.setattr(
        "main.threading.Thread",
        lambda **kwargs: created_threads.append(_FakeScanThread(**kwargs))
        or created_threads[-1],
    )
    window = _area_scan_window()

    response = Main._api_microscope_area_scan(window, {"flat_field": False})

    assert response["accepted"] is True
    configuration, _planning_request, launch_snapshot = created_threads[0].args
    assert configuration.overlap_fraction == pytest.approx(0.25)
    assert launch_snapshot.objective_name == "X20"
    assert launch_snapshot.scan_name == "design_scan"


def test_api_microscope_area_scan_rejects_removed_auto_exposure(monkeypatch) -> None:
    created_threads: list[_FakeScanThread] = []
    monkeypatch.setattr(
        "main.threading.Thread",
        lambda **kwargs: created_threads.append(_FakeScanThread(**kwargs))
        or created_threads[-1],
    )
    window = _area_scan_window()

    response = Main._api_microscope_area_scan(
        window,
        {"pattern": "stitch_debug", "structure_size_mm": 0.4, "auto_exposure": False},
    )

    assert response == {
        "accepted": False,
        "status_code": 400,
        "message": "auto_exposure is no longer supported for area scans.",
    }
    assert created_threads == []


@pytest.mark.parametrize("field", ("tile_approach_mm", "approach_mm"))
def test_api_microscope_area_scan_rejects_removed_approach(field: str) -> None:
    window = Main.__new__(Main)
    window._microscope_scan_running = lambda: pytest.fail(
        "obsolete approach must be rejected before scan state is read"
    )

    response = Main._api_microscope_area_scan(window, {field: 0.01})

    assert response == {
        "accepted": False,
        "status_code": 400,
        "message": f"{field} is no longer supported for area scans.",
    }
