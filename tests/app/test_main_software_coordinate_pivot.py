from __future__ import annotations

from types import SimpleNamespace

from main import Main
from probe_station_gui.coordinates.coordinator_model import (
    CoordinateSystemSnapshot,
    CoordinateTransition,
)
from probe_station_gui.coordinates.rotation_geometry import (
    rotation_geometry_snapshot,
)
from probe_station_gui.settings.manager import Settings


class _SettingsManager:
    def __init__(self, settings: Settings, events: list[object]) -> None:
        self.settings = settings
        self.saved: list[Settings] = []
        self._events = events

    def replace_and_save(self, settings: Settings, **_kwargs: object) -> None:
        self.settings = settings.clone()
        self.saved.append(self.settings)
        self._events.append("save")

    def objectives_configuration(self):
        return self.settings.objectives.clone()


class _Stage:
    def __init__(self, *, busy: bool) -> None:
        self.busy = busy

    def is_busy(self) -> bool:
        return self.busy

    def latest_machine_coordinate_snapshot(self):
        return None

    def homed_axes(self) -> set[str]:
        return {"X", "Y", "Z", "A"}


class _Coordinator:
    def __init__(self, events: list[object]) -> None:
        self._events = events
        self.observations: list[object] = []
        self._snapshot = CoordinateSystemSnapshot(False, (), None)

    def snapshot(self) -> CoordinateSystemSnapshot:
        return self._snapshot

    def observe_authority(self, observation: object) -> CoordinateTransition:
        self.observations.append(observation)
        self._events.append("authority")
        return CoordinateTransition(self._snapshot)


def _owner(*, busy: bool = False) -> SimpleNamespace:
    events: list[object] = []
    manager = _SettingsManager(Settings(), events)
    coordinator = _Coordinator(events)
    owner = SimpleNamespace(
        settings_manager=manager,
        stage_controller=_Stage(busy=busy),
        _coordinate_system_coordinator=coordinator,
        _stage_position_panel=None,
        _stage_axis_display_values={},
        _objective_mutation_busy=lambda: False,
        _active_objective_xy_offset=lambda: (0.0, 0.0),
        _apply_settings=lambda **_kwargs: events.append("apply"),
        _show_status=lambda message, timeout=0: events.append(
            ("status", str(message), int(timeout))
        ),
        _events=events,
    )
    owner._rotation_geometry_snapshot = lambda: rotation_geometry_snapshot(
        manager.settings.software_coordinates
    )
    return owner


def test_idle_pivot_edit_saves_then_refreshes_coordinate_authority() -> None:
    owner = _owner()
    updated = owner.settings_manager.settings.clone()
    updated.software_coordinates.pivot.x_mm = 1.0

    Main._apply_settings_from_dialog(owner, updated)

    assert owner.settings_manager.settings.software_coordinates.pivot.x_mm == 1.0
    assert len(owner._coordinate_system_coordinator.observations) == 1
    assert owner._events == ["save", "apply", "authority"]


def test_same_pivot_reapply_does_not_refresh_authority() -> None:
    owner = _owner()
    unchanged = owner.settings_manager.settings.clone()

    Main._apply_settings_from_dialog(owner, unchanged)

    assert owner._events == ["save", "apply"]
    assert owner._coordinate_system_coordinator.observations == []


def test_busy_stage_rejects_programmatic_settings_without_side_effects() -> None:
    owner = _owner(busy=True)
    before = owner.settings_manager.settings.clone()
    submitted = before.clone()
    submitted.design_last_directory = "C:/programmatic-bypass"
    submitted.software_coordinates.pivot.x_mm = 2.0

    Main._apply_settings_from_dialog(owner, submitted)

    assert owner.settings_manager.settings == before
    assert owner.settings_manager.saved == []
    assert owner._coordinate_system_coordinator.observations == []
    assert owner._events == [
        ("status", "Stage is busy; settings not changed.", 4000)
    ]
