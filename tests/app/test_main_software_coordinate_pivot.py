from __future__ import annotations

from types import SimpleNamespace
from uuid import uuid4

from main import Main
from probe_station_gui.coordinates import (
    AxisReadiness,
    BFrameTransform,
    CoordinateFrameRecord,
    CoordinateFrameRegistry,
    FrameKind,
    PhysicalMachinePose,
    ReadinessStatus,
)
from probe_station_gui.coordinates.software_frames import materialize_custom_frames
from probe_station_gui.settings.manager import Settings
from probe_station_gui.settings.software_coordinates import CustomFrameSettings
from probe_station_gui.views import main_window_stage_position_panel as panel_adapter


class _SettingsManager:
    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self.saved: list[Settings] = []

    def replace_and_save(self, settings: Settings, **_kwargs: object) -> None:
        self.settings = settings.clone()
        self.saved.append(self.settings)


class _Stage:
    def __init__(self, *, busy: bool) -> None:
        self.busy = busy

    def is_busy(self) -> bool:
        return self.busy

    def homed_axes(self) -> set[str]:
        return {"X", "Y", "Z", "A", "B"}


def _owner(*, busy: bool = False) -> SimpleNamespace:
    settings = Settings()
    frame_id = str(uuid4())
    settings.software_coordinates.custom_frames = (
        settings.software_coordinates.custom_frames
        + (
            CustomFrameSettings(
                frame_id=frame_id,
                name="fixture",
                origin_x_mm=10.0,
                origin_y_mm=0.0,
                reference_b_deg=0.0,
                xy_angle_deg=0.0,
                b_zero_deg=0.0,
            ),
        )
    )
    design = CoordinateFrameRecord(
        frame_id=str(uuid4()),
        kind=FrameKind.DESIGN,
        name="design",
        version=3,
        transform=BFrameTransform(
            origin_xy_at_reference_b=(8.0, 0.0),
            reference_b_deg=0.0,
            xy_angle_at_reference_b_deg=0.0,
            b_zero_machine_deg=0.0,
        ),
        readiness={axis: AxisReadiness(ReadinessStatus.READY) for axis in "XYZAB"},
        metadata={"registration_marks": ["preserve"]},
    )
    registry = CoordinateFrameRegistry()
    registry.reset(materialize_custom_frames((design,), settings.software_coordinates))
    manager = _SettingsManager(settings)
    owner = SimpleNamespace(
        settings_manager=manager,
        stage_controller=_Stage(busy=busy),
        _coordinate_frame_registry=registry,
        _coordinate_frames_loaded=True,
        _selected_coordinate_frame_id=frame_id,
        _pending_coordinate_frame_restore_id=None,
        _stage_position_panel=None,
        _latest_physical_machine_pose=PhysicalMachinePose(
            {"X": 0.0, "Y": 0.0, "Z": 1.0, "A": 2.0, "B": 90.0}
        ),
        _objective_mutation_busy=lambda: False,
        _apply_settings=lambda **_kwargs: None,
        _show_status=lambda *_args: None,
    )
    owner._invalidate_coordinate_frames_for_calibration_change = (
        lambda axes: Main._invalidate_coordinate_frames_for_calibration_change(owner, axes)
    )
    return owner


def test_idle_pivot_edit_reprojects_display_without_replacing_frame_records() -> None:
    owner = _owner()
    before_records = owner._coordinate_frame_registry.snapshot()
    panel_adapter.update_software_coordinate_display(
        owner,
        owner._latest_physical_machine_pose,
    )
    before_display = dict(owner._stage_axis_display_values)
    updated = owner.settings_manager.settings.clone()
    updated.software_coordinates.pivot.x_mm = 1.0

    Main._apply_settings_from_dialog(owner, updated)

    after_records = owner._coordinate_frame_registry.snapshot()
    assert owner.settings_manager.settings.software_coordinates.pivot.x_mm == 1.0
    assert after_records == before_records
    assert owner._stage_axis_display_values["X"] != before_display["X"]
    design = next(record for record in after_records.records if record.kind is FrameKind.DESIGN)
    assert design.metadata["registration_marks"] == ["preserve"]
    assert all(state.available for state in design.readiness.values())


def test_same_pivot_reapply_is_registry_noop_and_busy_pivot_is_rejected() -> None:
    owner = _owner()
    unchanged = owner.settings_manager.settings.clone()
    before = owner._coordinate_frame_registry.snapshot()

    Main._apply_settings_from_dialog(owner, unchanged)

    assert owner._coordinate_frame_registry.snapshot() == before
    owner.stage_controller.busy = True
    changed = owner.settings_manager.settings.clone()
    changed.software_coordinates.pivot.x_mm = 1.0
    Main._apply_settings_from_dialog(owner, changed)

    assert owner.settings_manager.settings.software_coordinates.pivot.x_mm == 0.0
    assert owner._coordinate_frame_registry.snapshot() == before
