import types

import pytest
from PySide6.QtWidgets import QDialog, QInputDialog, QMessageBox

from main import Main
from probe_station_gui.design.objective_offsets import ObjectiveOffsetReference
from probe_station_gui.design.session import AlignmentPreparation
from probe_station_gui.settings.manager import Settings
from probe_station_gui.settings.objective_config import (
    ObjectiveCalibrationSettings,
    ObjectivesSettings,
)
from probe_station_gui.stage.types import StageTaskToken
from probe_station_gui.views.main_window_auxiliary import open_settings_dialog


class _SettingsManager:
    def __init__(self, settings: Settings | None = None) -> None:
        self.settings = settings or _settings()
        self.saved_count = 0
        self.replaced: list[Settings] = []

    def replace(self, settings: Settings) -> None:
        self.settings = settings
        self.replaced.append(settings)

    def save(self) -> None:
        self.saved_count += 1

    def replace_and_save(
        self,
        settings: Settings,
        *,
        preserve_exposure_policy: bool = False,
    ) -> None:
        updated = settings.clone()
        if preserve_exposure_policy:
            updated.exposure_policy = self.settings.exposure_policy.clone()
        self.replace(updated)
        self.save()

    def objectives_configuration(self) -> ObjectivesSettings:
        return self.settings.objectives


class _Stage:
    def __init__(self) -> None:
        self.busy = False
        self.latest_position: tuple[float, ...] | None = (10.0, 20.0, 3.0)
        self.current_position: tuple[float, ...] = (10.0, 20.0, 3.0)
        self.absolute_moves: list[tuple[dict[str, float], float, bool]] = []
        self.accept_absolute_move = True
        self.rotations: list[float] = []
        self.applied_objectives: list[object] = []
        self.status_refreshes = 0

    def is_busy(self) -> bool:
        return self.busy

    def latest_stage_position(self) -> tuple[float, ...] | None:
        return self.latest_position

    def current_stage_position(self) -> tuple[float, ...]:
        return self.current_position

    def request_absolute_axis_targets_move(
        self,
        raw_targets: dict[str, float],
        *,
        feedrate: float,
        allow_unhomed: bool,
    ) -> bool:
        self.absolute_moves.append((dict(raw_targets), float(feedrate), allow_unhomed))
        return self.accept_absolute_move

    def request_rotate_b(self, rotation_deg: float) -> None:
        self.rotations.append(float(rotation_deg))

    def request_status_refresh(self) -> None:
        self.status_refreshes += 1

    def calibrated_axis_display_value(self, _axis: str, raw_value: float) -> float:
        return float(raw_value) + 100.0

    def calibrated_axis_raw_value(self, _axis: str, display_value: float) -> float:
        return float(display_value) - 100.0

    def apply_objective_configuration(self, *args: object) -> None:
        self.applied_objectives.append(args)


def _settings() -> Settings:
    settings = Settings()
    settings.objectives = ObjectivesSettings(
        active_name="X5",
        objectives={
            "X5": ObjectiveCalibrationSettings(
                name="X5",
                magnification=5.0,
                xy_offset_configured=True,
                z_offset_mm=0.5,
                z_offset_configured=True,
            ),
            "X20": ObjectiveCalibrationSettings(
                name="X20",
                magnification=20.0,
                xy_offset_x_mm=0.12,
                xy_offset_y_mm=-0.08,
                xy_offset_configured=True,
                z_offset_mm=0.8,
                z_offset_configured=True,
            ),
        },
    )
    return settings


def _window() -> tuple[Main, _Stage, _SettingsManager, list[str]]:
    window = Main.__new__(Main)
    stage = _Stage()
    manager = _SettingsManager()
    statuses: list[str] = []
    window.stage_controller = stage
    window.settings_manager = manager
    window._objective_combo = None
    window._click_calibration_action = None
    window._click_calibration_dialog = None
    window._objective_offset_reference = None
    window._design_session = types.SimpleNamespace(document=None)
    window._current_linear_feedrate = lambda: 123.0
    window._refresh_design_position = lambda: setattr(
        window,
        "_design_refreshes",
        getattr(window, "_design_refreshes", 0) + 1,
    )
    window._refresh_click_calibration_ui = lambda: setattr(
        window,
        "_calibration_refreshes",
        getattr(window, "_calibration_refreshes", 0) + 1,
    )
    window._show_status = lambda message, _timeout_ms=0: statuses.append(str(message))
    return window, stage, manager, statuses


def _preparation(rotation_deg: float) -> AlignmentPreparation:
    return AlignmentPreparation(
        design_marks=((0.0, 0.0), (1.0, 0.0)),
        stage_marks_before_rotation=((10.0, 20.0), (11.0, 20.0)),
        stage_marks_after_rotation=((10.0, 20.0), (11.0, 20.0)),
        pivot_stage=(0.0, 0.0),
        rotation_deg=rotation_deg,
        design_distance_mm=1.0,
        stage_distance_mm=1.0,
        distance_ratio=1.0,
    )


def test_apply_objective_change_offset_preserves_targets_and_status() -> None:
    window, stage, _manager, statuses = _window()

    Main._apply_objective_change_offset(window, "X5", "X20")

    assert stage.absolute_moves == [
        ({"X": 10.12, "Y": 19.92, "Z": 3.299999999999997}, 123.0, False)
    ]
    assert statuses == ["Applying X20 objective offset on X, Y, Z."]


def test_set_active_objective_busy_restores_combo_and_does_not_save() -> None:
    window, stage, manager, statuses = _window()
    stage.busy = True
    restored: list[str] = []
    window._sync_objective_combo = lambda name: restored.append(name)

    Main._set_active_objective(window, "X20", apply_motion=True)

    assert restored == ["X5"]
    assert manager.saved_count == 0
    assert statuses == ["Stage is busy; objective not changed."]


def test_objective_combo_change_is_rejected_for_alive_microscope_scan() -> None:
    window, stage, manager, statuses = _window()
    restored: list[str] = []
    window._microscope_scan_thread = types.SimpleNamespace(is_alive=lambda: True)
    window._objective_combo = types.SimpleNamespace(currentData=lambda: "X20")
    window._sync_objective_combo = lambda name: restored.append(name)

    Main._on_objective_combo_changed(window, 1)

    assert manager.settings.objectives.active_name == "X5"
    assert manager.saved_count == 0
    assert stage.absolute_moves == []
    assert restored == ["X5"]
    assert statuses == ["Stage is busy; objective not changed."]


def test_settings_objective_change_is_rejected_for_alive_microscope_scan() -> None:
    window, stage, manager, statuses = _window()
    current_profile = manager.settings.objectives.objectives["X5"]
    current_profile.pixels_to_mm = [[0.01, 0.0], [0.0, 0.01]]
    current_profile.xy_calibration_configured = True
    current_profile.distortion_correction = {"camera_matrix": [[5.0]]}
    current_profile.distortion_correction_configured = True
    original_active_profile = current_profile.to_dict()

    submitted = manager.settings.clone()
    submitted.design_last_directory = "C:/updated-designs"
    submitted.objectives.active_name = "X20"
    submitted.objectives.objectives["X5"].magnification = 99.0
    submitted.objectives.objectives["X5"].distortion_correction = {
        "camera_matrix": [[99.0]]
    }
    submitted.objectives.objectives["X20"].magnification = 25.0

    class _Signal:
        def __init__(self) -> None:
            self._slots = []

        def connect(self, slot) -> None:
            self._slots.append(slot)

        def emit(self, value: object) -> None:
            for slot in self._slots:
                slot(value)

    class _SettingsDialog:
        instance = None

        def __init__(self, *_args, **_kwargs) -> None:
            self.settings_applied = _Signal()
            self.restored_active_names: list[str] = []
            type(self).instance = self

        def exec(self):
            self.settings_applied.emit(submitted.clone())
            return QDialog.Accepted

        def was_applied(self) -> bool:
            return True

        def set_objectives(self, objectives: ObjectivesSettings) -> None:
            self.restored_active_names.append(str(objectives.active_name))

    window._microscope_scan_thread = types.SimpleNamespace(is_alive=lambda: True)
    window._sync_objective_combo = lambda _name: None
    window._refresh_objective_calibration_ui = lambda: None
    window._apply_settings = lambda *, apply_objective_runtime=True: (
        Main._apply_objective_settings(window)
        if apply_objective_runtime
        else None
    )
    window._stop_telegram_bot_service = lambda: None
    window._configure_telegram_bot_from_settings = lambda: None
    window.grabber = None
    window._exposure_policy_adapter = None
    window._api_key_store = None

    open_settings_dialog(window, dialog_class=_SettingsDialog)

    assert manager.settings.design_last_directory == "C:/updated-designs"
    assert manager.settings.objectives.active_name == "X5"
    assert (
        manager.settings.objectives.objectives["X5"].to_dict()
        == original_active_profile
    )
    assert manager.settings.objectives.objectives["X20"].magnification == 25.0
    assert stage.applied_objectives == []
    assert _SettingsDialog.instance is not None
    assert _SettingsDialog.instance.restored_active_names == ["X5"]
    assert statuses == ["Stage is busy; active objective settings not changed."]


def test_settings_apply_during_click_calibration_keeps_computed_runtime_matrix() -> None:
    window, stage, manager, _statuses = _window()
    stage.busy = True
    computed_matrix = [[0.025, 0.0], [0.0, 0.025]]
    submitted = manager.settings.clone()
    submitted.design_last_directory = "C:/updated-designs"
    submitted.objectives.objectives["X20"].magnification = 25.0
    objective_runtime_applies: list[bool] = []
    unrelated_applies: list[bool] = []

    def apply_settings(*, apply_objective_runtime: bool = True) -> None:
        unrelated_applies.append(True)
        if apply_objective_runtime:
            objective_runtime_applies.append(True)

    window._apply_settings = apply_settings
    stage.runtime_matrix = computed_matrix

    Main._apply_settings_from_dialog(window, submitted)

    assert manager.settings.design_last_directory == "C:/updated-designs"
    assert manager.settings.objectives.objectives["X20"].magnification == 25.0
    assert unrelated_applies == [True]
    assert objective_runtime_applies == []
    assert stage.runtime_matrix == computed_matrix


def test_add_objective_rechecks_busy_after_name_dialog_returns(monkeypatch) -> None:
    window, _stage, manager, statuses = _window()

    def get_text(*_args, **_kwargs):
        window._microscope_scan_thread = types.SimpleNamespace(is_alive=lambda: True)
        return "X50", True

    monkeypatch.setattr(QInputDialog, "getText", get_text)

    Main._add_objective_profile(window)

    assert "X50" not in manager.settings.objectives.objectives
    assert manager.saved_count == 0
    assert statuses == ["Stage is busy; objective not added."]


def test_delete_objective_rechecks_busy_after_confirmation_returns(monkeypatch) -> None:
    window, _stage, manager, statuses = _window()

    def question(*_args, **_kwargs):
        window._microscope_scan_thread = types.SimpleNamespace(is_alive=lambda: True)
        return QMessageBox.Yes

    monkeypatch.setattr(QMessageBox, "question", question)

    Main._delete_objective_profile(window, "X20")

    assert "X20" in manager.settings.objectives.objectives
    assert manager.saved_count == 0
    assert statuses == ["Stage is busy; objective not deleted."]


def test_objective_mismatch_cannot_bypass_alive_scan_guard() -> None:
    window, stage, manager, statuses = _window()
    task_token = StageTaskToken(1, "_run_move")
    stage.is_calibration_task_token_current = lambda token: token is task_token
    restored: list[str] = []
    window._microscope_scan_thread = types.SimpleNamespace(is_alive=lambda: True)
    window._sync_objective_combo = lambda name: restored.append(name)

    Main._on_objective_mismatch_detected(
        window,
        "X20",
        "Objective mismatch.",
        task_token,
    )

    assert manager.settings.objectives.active_name == "X5"
    assert manager.saved_count == 0
    assert stage.applied_objectives == []
    assert restored == ["X5"]
    assert statuses == [
        "Stage is busy; objective not changed.",
        "Objective mismatch.",
    ]


def test_stale_objective_mismatch_a_cannot_switch_objective_during_task_b() -> None:
    window, stage, manager, statuses = _window()
    token_a = StageTaskToken(1, "_run_move")
    token_b = StageTaskToken(2, "_run_move")
    stage.busy = True
    stage.is_calibration_task_token_current = lambda token: token is token_b

    Main._on_objective_mismatch_detected(
        window,
        "X20",
        "Objective mismatch from calibration A.",
        token_a,
    )

    assert manager.settings.objectives.active_name == "X5"
    assert manager.saved_count == 0
    assert stage.applied_objectives == []
    assert statuses == [
        "Objective mismatch ignored because its calibration task is no longer current."
    ]


def test_set_active_objective_reports_selected_after_offset_motion_status() -> None:
    window, stage, manager, statuses = _window()

    Main._set_active_objective(window, "X20", apply_motion=True)

    assert manager.settings.objectives.active_name == "X20"
    assert stage.absolute_moves
    assert statuses == [
        "Applying X20 objective offset on X, Y, Z.",
        "Objective selected: X20.",
    ]


def test_set_objective_offset_reference_base_saves_zero_offset_and_refreshes() -> None:
    window, _stage, manager, statuses = _window()
    window._resolve_alignment_capture_stage_position = lambda: (10.0, 20.0)
    apply_calls: list[str] = []
    window._apply_objective_settings = lambda: apply_calls.append("apply")

    Main._set_objective_offset_reference(window)

    profile = manager.settings.objectives.objectives["X5"]
    assert profile.xy_offset_configured is True
    assert (profile.xy_offset_x_mm, profile.xy_offset_y_mm) == (0.0, 0.0)
    assert manager.saved_count == 1
    assert apply_calls == ["apply"]
    assert window._objective_offset_reference.objective_name == "X5"
    assert window._objective_offset_reference.stage_xy == (10.0, 20.0)
    assert window._objective_offset_reference.offset_xy == (0.0, 0.0)
    assert getattr(window, "_calibration_refreshes", 0) == 1
    assert statuses[-1].startswith("Objective offset reference set with X5.")


def test_save_active_objective_offset_non_base_saves_delta_and_refreshes() -> None:
    window, _stage, manager, statuses = _window()
    manager.settings.objectives.active_name = "X20"
    window._objective_offset_reference = ObjectiveOffsetReference(
        "X5",
        (10.0, 20.0),
        (0.0, 0.0),
    )
    window._resolve_alignment_capture_stage_position = lambda: (10.125, 19.75)
    apply_calls: list[str] = []
    window._apply_objective_settings = lambda: apply_calls.append("apply")

    Main._save_active_objective_offset(window)

    profile = manager.settings.objectives.objectives["X20"]
    assert (profile.xy_offset_x_mm, profile.xy_offset_y_mm) == (0.125, -0.25)
    assert profile.xy_offset_configured is True
    assert manager.saved_count == 1
    assert apply_calls == ["apply"]
    assert getattr(window, "_design_refreshes", 0) == 1
    assert statuses == ["Saved X20 objective offset: X=+0.1250, Y=-0.2500 mm."]


def test_capture_manual_alignment_point_requests_b_rotation_for_quick_alignment() -> None:
    window, stage, _manager, statuses = _window()
    window._manual_alignment_pick_slot = 1
    window._manual_alignment_points = [(1.0, 1.0), None]
    window._pending_quick_alignment_rotation = False
    window._design_backed_alignment_active = lambda: False
    window._refresh_manual_alignment_ui = lambda: None
    window._update_stage_coordinate_apply_state = lambda: None
    window._set_alignment_panel_expanded = lambda: None
    window._invalidate_design_registration = lambda reason: setattr(
        window,
        "_invalidated_reason",
        reason,
    )

    Main._capture_manual_alignment_point(window, 1, (2.0, 2.0), source="center")

    assert window._manual_alignment_pick_slot is None
    assert window._pending_quick_alignment_rotation is True
    assert stage.rotations == [-45.0]
    assert window._invalidated_reason == "Design registration cleared after B-axis rotation."
    assert statuses == ["Chip alignment: rotating B by -45.000 deg."]


def test_capture_manual_alignment_point_design_near_zero_applies_without_b_rotation() -> None:
    window, stage, _manager, statuses = _window()
    session_calls: list[object] = []
    preparation = _preparation(0.0005)
    session = types.SimpleNamespace(
        set_source_stage_mark=lambda slot, xy: session_calls.append(("mark", slot, xy)),
        source_pair_count=lambda: 2,
        prepare_source_alignment=lambda: preparation,
        apply_prepared_alignment=lambda prep: session_calls.append(("apply", prep)),
    )
    window._design_session = session
    window._manual_alignment_pick_slot = 1
    window._pending_alignment_preparation = None
    window._design_backed_alignment_active = lambda: True
    window._camera_stage_xy_from_raw_stage_xy = lambda xy: (xy[0] - 0.12, xy[1] + 0.08)
    window._refresh_manual_alignment_ui = lambda: None
    window._update_stage_coordinate_apply_state = lambda: None
    window._refresh_design_panel = lambda: session_calls.append("panel")
    window._refresh_design_position = lambda: session_calls.append("position")
    window._design_spacing_ratio_is_reasonable = lambda _ratio: True
    window._set_design_snap_enabled = lambda value: session_calls.append(("snap", value))
    window._collapse_alignment_panel_if_ready = lambda: session_calls.append("collapse")

    Main._capture_manual_alignment_point(window, 1, (11.0, 20.0), source="center")

    assert ("mark", 1, (10.88, 20.08)) in session_calls
    assert ("apply", preparation) in session_calls
    assert ("snap", False) in session_calls
    assert "collapse" in session_calls
    assert stage.rotations == []
    assert statuses == ["Design calibration complete. Spacing ratio 1.000."]
