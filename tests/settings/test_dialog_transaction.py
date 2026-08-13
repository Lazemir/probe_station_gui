from __future__ import annotations

import importlib.util
from dataclasses import FrozenInstanceError
from types import SimpleNamespace

import pytest

from probe_station_gui.coordinates.design_calibration import (
    design_calibration_fingerprints,
)
from probe_station_gui.settings.axis_calibration_config import (
    AxisCalibrationSettings,
)
from probe_station_gui.settings.manager import Settings, SettingsManager
from probe_station_gui.settings.sections import ExposurePolicySettings
from probe_station_gui.settings.dialog_transaction import (
    SettingsDialogContext,
    SettingsDialogNotice,
    SettingsDialogOutcome,
    SettingsDialogTransaction,
)
from probe_station_gui.settings.software_coordinates import CustomFrameSettings


_CUSTOM_FRAME_ID = "14c838bd-a9a5-47bd-9d22-6326ca63c469"


def _custom_frame() -> CustomFrameSettings:
    return CustomFrameSettings(
        frame_id=_CUSTOM_FRAME_ID,
        name="fixture",
        origin_x_mm=1.0,
        origin_y_mm=2.0,
        reference_b_deg=3.0,
        xy_angle_deg=4.0,
        b_zero_deg=5.0,
    )


def test_dialog_transaction_owner_module_exists() -> None:
    assert (
        importlib.util.find_spec("probe_station_gui.settings.dialog_transaction")
        is not None
    )


class _Manager:
    def __init__(self, events: list[object]) -> None:
        self.settings = Settings()
        self.events = events
        self.saved: list[tuple[Settings, dict[str, object]]] = []

    def replace_and_save(self, settings: Settings, **kwargs: object) -> None:
        self.events.append(("save", settings, kwargs))
        self.saved.append((settings.clone(), dict(kwargs)))
        self.settings = settings.clone()

    def objectives_configuration(self) -> object:
        return self.settings.objectives


class _Coordinator:
    def __init__(
        self,
        events: list[object],
        *,
        frames_loaded: bool = False,
        active_frame_id: str | None = None,
        custom_error: Exception | None = None,
        calibration_view_changed: bool = False,
    ) -> None:
        self.events = events
        self.frames_loaded = frames_loaded
        self.active_frame_id = active_frame_id
        self.custom_error = custom_error
        self.calibration_view_changed = calibration_view_changed

    def snapshot(self) -> object:
        return SimpleNamespace(
            frames_loaded=self.frames_loaded,
            registration=SimpleNamespace(active_frame_id=self.active_frame_id),
        )

    def synchronize_custom_systems(self, request: object) -> object:
        self.events.append(("synchronize_custom_systems", request))
        if self.custom_error is not None:
            raise self.custom_error
        return SimpleNamespace(view_changed=True)

    def observe_design_calibrations(self, observation: object) -> object:
        self.events.append(("observe_design_calibrations", observation))
        return SimpleNamespace(view_changed=self.calibration_view_changed)


def _transaction(
    *,
    frames_loaded: bool = False,
    active_frame_id: str | None = None,
    custom_error: Exception | None = None,
    calibration_view_changed: bool = False,
) -> tuple[
    SettingsDialogTransaction,
    _Manager,
    list[object],
]:
    events: list[object] = []
    manager = _Manager(events)
    transaction = SettingsDialogTransaction(
        manager,
        _Coordinator(
            events,
            frames_loaded=frames_loaded,
            active_frame_id=active_frame_id,
            custom_error=custom_error,
            calibration_view_changed=calibration_view_changed,
        ),
        publish_notice=lambda message, timeout: events.append(
            ("notice", str(message), int(timeout))
        ),
        publish_transition=lambda transition: events.append(("transition", transition)),
    )
    return transaction, manager, events


def test_interface_is_canonical_and_frozen() -> None:
    assert SettingsDialogTransaction.__module__ == (
        "probe_station_gui.settings.dialog_transaction"
    )
    context = SettingsDialogContext(False, False)
    with pytest.raises(FrozenInstanceError):
        context.stage_busy = True  # type: ignore[misc]


def test_invalid_submission_has_no_side_effects() -> None:
    transaction, manager, events = _transaction()

    outcome = transaction.apply(object(), SettingsDialogContext(False, False))

    assert outcome == SettingsDialogOutcome()
    assert manager.saved == []
    assert events == []


def test_busy_stage_rejects_before_clone_or_save() -> None:
    transaction, manager, events = _transaction()

    outcome = transaction.apply(
        manager.settings,
        SettingsDialogContext(stage_busy=True, objective_mutation_busy=False),
    )

    assert outcome == SettingsDialogOutcome()
    assert manager.saved == []
    assert events == [("notice", "Stage is busy; settings not changed.", 4000)]


def test_accepted_submission_saves_with_exposure_policy_preserved() -> None:
    transaction, manager, events = _transaction()
    submitted = manager.settings.clone()
    submitted.design_last_directory = "C:/updated"

    outcome = transaction.apply(
        submitted,
        SettingsDialogContext(stage_busy=False, objective_mutation_busy=False),
    )

    assert outcome == SettingsDialogOutcome(accepted=True)
    assert manager.settings.design_last_directory == "C:/updated"
    assert manager.saved[0][1] == {"preserve_exposure_policy": True}
    assert events[0][0] == "save"


def test_invalid_custom_system_restores_complete_previous_coordinate_section() -> None:
    transaction, manager, events = _transaction(
        frames_loaded=True,
        custom_error=ValueError("Custom frame is invalid."),
    )
    previous_coordinates = manager.settings.software_coordinates.clone()
    submitted = manager.settings.clone()
    submitted.software_coordinates.custom_frames = (_custom_frame(),)
    submitted.software_coordinates.last_selected_frame_id = _CUSTOM_FRAME_ID
    submitted.software_coordinates.pivot.x_mm = 9.0
    submitted.design_last_directory = "C:/unrelated"

    outcome = transaction.apply(
        submitted,
        SettingsDialogContext(stage_busy=False, objective_mutation_busy=False),
    )

    assert outcome == SettingsDialogOutcome(accepted=True)
    assert manager.settings.software_coordinates == previous_coordinates
    assert manager.settings.design_last_directory == "C:/unrelated"
    assert [event[0] for event in events[:3]] == [
        "synchronize_custom_systems",
        "notice",
        "save",
    ]
    assert events[1] == ("notice", "Custom frame is invalid.", 6000)


def test_valid_custom_system_publishes_transition_after_save_and_refreshes() -> None:
    transaction, manager, events = _transaction(frames_loaded=True)
    submitted = manager.settings.clone()
    submitted.software_coordinates.custom_frames = (_custom_frame(),)

    outcome = transaction.apply(
        submitted,
        SettingsDialogContext(stage_busy=False, objective_mutation_busy=False),
    )

    assert outcome == SettingsDialogOutcome(
        accepted=True,
        refresh_coordinate_frame_display=True,
    )
    assert [event[0] for event in events[:3]] == [
        "synchronize_custom_systems",
        "save",
        "transition",
    ]


def test_missing_cached_machine_snapshot_restores_only_changed_pivot() -> None:
    transaction, manager, events = _transaction(
        frames_loaded=True,
        active_frame_id="design-frame",
    )
    previous_pivot = manager.settings.software_coordinates.pivot.clone()
    submitted = manager.settings.clone()
    submitted.software_coordinates.pivot.x_mm = 2.5
    submitted.design_last_directory = "C:/unrelated"

    outcome = transaction.apply(
        submitted,
        SettingsDialogContext(
            stage_busy=False,
            objective_mutation_busy=False,
            machine_snapshot=None,
        ),
    )

    assert outcome == SettingsDialogOutcome(accepted=True)
    assert manager.settings.software_coordinates.pivot == previous_pivot
    assert manager.settings.design_last_directory == "C:/unrelated"
    assert events[:2] == [
        (
            "notice",
            "A current Machine-coordinate snapshot is required to change "
            "the B-axis pivot.",
            6000,
        ),
        ("save", manager.saved[0][0], manager.saved[0][1]),
    ]
    assert [event[0] for event in events[2:]] == [
        "observe_design_calibrations",
        "transition",
    ]


def test_active_design_frame_validates_changed_pivot_from_cached_b() -> None:
    transaction, manager, events = _transaction(
        frames_loaded=True,
        active_frame_id="design-frame",
    )
    submitted = manager.settings.clone()
    submitted.software_coordinates.pivot.x_mm = 2.5
    required_axes: list[str] = []
    machine_snapshot = SimpleNamespace(
        physical_machine_pose=SimpleNamespace(
            require=lambda axis: required_axes.append(str(axis)) or 12.0
        )
    )

    outcome = transaction.apply(
        submitted,
        SettingsDialogContext(
            stage_busy=False,
            objective_mutation_busy=False,
            machine_snapshot=machine_snapshot,
        ),
    )

    assert outcome == SettingsDialogOutcome(
        accepted=True,
        refresh_coordinate_frame_display=True,
        observe_coordinate_authority=True,
    )
    assert manager.settings.software_coordinates.pivot.x_mm == 2.5
    assert required_axes == ["B"]
    assert [event[0] for event in events] == [
        "save",
        "observe_design_calibrations",
        "transition",
    ]


def test_busy_objective_preserves_active_profile_but_accepts_inactive_edits() -> None:
    transaction, manager, events = _transaction()
    current_active = manager.settings.objectives.objectives["X5"].clone()
    current_active.pixels_to_mm = [[0.01, 0.0], [0.0, 0.01]]
    current_active.xy_calibration_configured = True
    manager.settings.objectives.objectives["X5"] = current_active
    submitted = manager.settings.clone()
    submitted.design_last_directory = "C:/unrelated"
    submitted.objectives.active_name = "X20"
    submitted.objectives.objectives["X5"].magnification = 99.0
    submitted.objectives.objectives["X20"].magnification = 25.0

    outcome = transaction.apply(
        submitted,
        SettingsDialogContext(
            stage_busy=False,
            objective_mutation_busy=True,
        ),
    )

    assert outcome == SettingsDialogOutcome(
        accepted=True,
        apply_objective_runtime=False,
        post_apply_notices=(
            SettingsDialogNotice(
                "Stage is busy; active objective settings not changed.",
                4000,
            ),
        ),
    )
    assert manager.settings.objectives.active_name == "X5"
    assert manager.settings.objectives.objectives["X5"] == current_active
    assert manager.settings.objectives.objectives["X20"].magnification == 25.0
    assert manager.settings.design_last_directory == "C:/unrelated"
    assert [event[0] for event in events] == ["save"]


def test_axis_calibration_reconciliation_uses_saved_fingerprints_in_order() -> None:
    transaction, manager, events = _transaction(
        frames_loaded=True,
        calibration_view_changed=True,
    )
    submitted = manager.settings.clone()
    submitted.axis_calibrations["X"] = AxisCalibrationSettings(
        enabled=True,
        calibration_file="X.npz",
        controller_points=[0.0, 1.0],
        physical_points=[0.0, 1.1],
    )

    outcome = transaction.apply(
        submitted,
        SettingsDialogContext(stage_busy=False, objective_mutation_busy=False),
    )

    assert outcome == SettingsDialogOutcome(
        accepted=True,
        refresh_coordinate_frame_display=True,
        observe_coordinate_authority=True,
    )
    assert [event[0] for event in events] == [
        "save",
        "observe_design_calibrations",
        "transition",
    ]
    assert events[1][1].fingerprints == design_calibration_fingerprints(
        manager.settings.axis_calibrations
    )


def test_real_manager_preserves_concurrent_exposure_policy(
    tmp_path,
    monkeypatch,
) -> None:
    monkeypatch.setenv("APPDATA", str(tmp_path / "appdata"))
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path / "localappdata"))
    monkeypatch.setenv(
        "XDG_CONFIG_HOME",
        str(tmp_path / "xdg-config"),
    )
    monkeypatch.setenv(
        "XDG_STATE_HOME",
        str(tmp_path / "xdg-state"),
    )
    monkeypatch.setattr(
        "probe_station_gui.settings.manager.platform.system",
        lambda: "Windows",
    )
    monkeypatch.setattr(
        "probe_station_gui.settings.manager.configure_logging",
        lambda *_args: None,
    )
    manager = SettingsManager()
    manager.replace(
        Settings(
            exposure_policy=ExposurePolicySettings(
                auto_enabled=False,
                engine="camera",
            )
        )
    )
    transaction = SettingsDialogTransaction(
        manager,
        _Coordinator([]),
        publish_notice=lambda _message, _timeout: None,
        publish_transition=lambda _transition: None,
    )
    stale_dialog_settings = Settings()
    stale_dialog_settings.design_last_directory = "C:/dialog-selection"

    outcome = transaction.apply(
        stale_dialog_settings,
        SettingsDialogContext(False, False),
    )

    assert outcome.accepted
    assert manager.settings.design_last_directory == "C:/dialog-selection"
    assert manager.settings.exposure_policy.to_dict() == {
        "auto_enabled": False,
        "engine": "camera",
    }
