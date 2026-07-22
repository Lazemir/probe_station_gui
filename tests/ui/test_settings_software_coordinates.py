from __future__ import annotations

import os

import pytest


os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
pytest.importorskip("PySide6")

from PySide6.QtWidgets import QApplication

from probe_station_gui.coordinates import PhysicalMachinePose
from probe_station_gui.coordinates import STAGE_AXES, VISIBLE_STAGE_AXES
from probe_station_gui.dialogs.settings.coordinate_system import (
    CoordinateSystemSettingsWidget,
)
from probe_station_gui.dialogs.settings_dialog import SettingsDialog
from probe_station_gui.settings.manager import Settings
from probe_station_gui.settings.software_coordinates import SoftwareCoordinateSettings
from probe_station_gui.views.joystick_window import JoystickWindow
from probe_station_gui.views.stage_position_panel import StagePositionPanel


def _qt_app() -> QApplication:
    app = QApplication.instance()
    if app is None:
        app = QApplication([])
    return app


def test_custom_frame_crud_keeps_stable_identity_and_returns_cloned_settings() -> None:
    _qt_app()
    widget = CoordinateSystemSettingsWidget(SoftwareCoordinateSettings())

    widget.add_custom_frame()
    widget.set_current_name("fixture")
    widget.set_current_origin(x_mm=1.0, y_mm=2.0)
    first = widget.current_frame()
    widget.duplicate_current_frame()
    duplicate = widget.current_frame()
    widget.rename_current_frame("fixture copy")
    widget.delete_current_frame()

    saved = widget.settings()
    assert first.frame_id != duplicate.frame_id
    assert len(saved.custom_frames) == 1
    assert saved.custom_frames[0].frame_id == first.frame_id
    assert saved.custom_frames[0].name == "fixture"
    assert (saved.custom_frames[0].origin_x_mm, saved.custom_frames[0].origin_y_mm) == (
        1.0,
        2.0,
    )

    saved.custom_frames = ()
    assert len(widget.settings().custom_frames) == 1
    widget.deleteLater()


def test_current_position_uses_cached_pose_without_establishing_vertical_origins() -> None:
    _qt_app()
    calls = 0

    def cached_pose() -> PhysicalMachinePose:
        nonlocal calls
        calls += 1
        return PhysicalMachinePose({"X": 1.0, "Y": 2.0, "B": 3.0, "Z": 9.0, "A": 8.0})

    widget = CoordinateSystemSettingsWidget(
        SoftwareCoordinateSettings(),
        physical_pose_source=cached_pose,
    )
    widget.add_custom_frame()

    assert widget.use_current_position()
    frame = widget.current_frame()
    assert calls == 1
    assert (frame.origin_x_mm, frame.origin_y_mm, frame.reference_b_deg) == (1.0, 2.0, 3.0)
    assert frame.z_zero_mm is None
    assert frame.a_zero_mm is None
    widget.deleteLater()


def test_busy_stage_blocks_coordinate_geometry_edits() -> None:
    _qt_app()
    widget = CoordinateSystemSettingsWidget(
        SoftwareCoordinateSettings(),
        stage_idle_source=lambda: False,
    )

    widget.add_custom_frame()
    before = widget.current_frame()
    assert not widget.set_current_origin(x_mm=1.0, y_mm=2.0)
    assert widget.current_frame() == before
    assert "Stage is busy" in widget.status_message()
    widget.deleteLater()


def test_to_settings_updates_only_software_coordinate_settings() -> None:
    _qt_app()
    settings = Settings()
    widget = CoordinateSystemSettingsWidget(SoftwareCoordinateSettings())
    widget.add_custom_frame()

    widget.to_settings(settings)

    assert len(settings.software_coordinates.custom_frames) == 1
    assert settings.coordinate_system.preferred_system == "G54"
    widget.deleteLater()


def test_ordinary_controls_hide_c_without_removing_backend_axis_configuration() -> None:
    _qt_app()
    panel = StagePositionPanel(VISIBLE_STAGE_AXES)
    settings = Settings()

    assert tuple(panel.axis_fields) == ("X", "Y", "Z", "A", "B")
    assert JoystickWindow.MANUAL_JOG_AXES == ("X", "Y", "Z", "A", "B")
    assert "C" not in JoystickWindow.MANUAL_JOG_AXES
    assert STAGE_AXES == ("X", "Y", "Z", "A", "B", "C")
    assert "C" in settings.axis_calibrations

    panel.deleteLater()


def test_settings_dialog_exposes_software_coordinates_tab() -> None:
    _qt_app()
    dialog = SettingsDialog(Settings())

    dialog.coordinate_system_tab.add_custom_frame()
    dialog._collect_settings()

    assert len(dialog.result_settings().software_coordinates.custom_frames) == 1
    dialog.deleteLater()


def test_settings_dialog_blocks_busy_or_invalid_coordinate_drafts() -> None:
    _qt_app()
    stage = {"idle": False}
    dialog = SettingsDialog(
        Settings(),
        stage_idle_source=lambda: stage["idle"],
    )
    applied: list[Settings] = []
    dialog.settings_applied.connect(applied.append)

    assert not dialog._save_button.isEnabled()
    assert not dialog._apply_button.isEnabled()
    stage["idle"] = True
    dialog.refresh_coordinate_availability()
    assert dialog._save_button.isEnabled()
    dialog.coordinate_system_tab.add_custom_frame()
    dialog.coordinate_system_tab._fields["origin_x_mm"].setText("not-a-number")
    assert not dialog._save_button.isEnabled()
    dialog._apply_without_closing()

    assert applied == []
    assert "finite" in dialog.coordinate_system_tab.status_message()
    dialog.deleteLater()


def test_settings_dialog_rechecks_stage_busy_when_apply_is_invoked() -> None:
    _qt_app()
    stage = {"idle": True}
    dialog = SettingsDialog(Settings(), stage_idle_source=lambda: stage["idle"])
    applied: list[Settings] = []
    dialog.settings_applied.connect(applied.append)
    assert dialog._apply_button.isEnabled()

    stage["idle"] = False
    dialog._apply_without_closing()

    assert applied == []
    assert "Stage is busy" in dialog.coordinate_system_tab.status_message()
    dialog.deleteLater()
