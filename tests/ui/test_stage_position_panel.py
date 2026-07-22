from __future__ import annotations

import os
from types import SimpleNamespace

import pytest


os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
pytest.importorskip("PySide6")

from PySide6.QtCore import Qt
from PySide6.QtGui import QDoubleValidator
from PySide6.QtWidgets import QApplication, QLabel

from probe_station_gui.coordinates.presentation import (
    CoordinateAxisDisplay,
    CoordinateDisplayPlan,
    CoordinateSelectorEntry,
)
from probe_station_gui.coordinates.model import (
    AxisReadiness,
    CoordinateFrameRecord,
    FrameKind,
    PhysicalMachinePose,
    ReadinessStatus,
)
from probe_station_gui.coordinates.registry import CoordinateFrameRegistry
from probe_station_gui.coordinates.transforms import BFrameTransform

from probe_station_gui.stage.position_presenter import (
    AxisFieldPresentation,
    StagePositionDisplayPlan,
)
from probe_station_gui.settings.axis_calibration_config import (
    default_axis_calibrations,
)
from probe_station_gui.settings.manager import SoftwareCoordinateSelectionSnapshot
from probe_station_gui.stage.controller import StageController
from probe_station_gui.stage.position_update import physical_machine_pose_from_controller
from probe_station_gui.stage.types import _Status
from probe_station_gui.views.stage_position_panel import (
    StagePositionPanel,
    format_stage_axis_value,
)
from probe_station_gui.views import main_window_stage_position_panel as panel_adapter


@pytest.fixture(scope="module")
def qt_app() -> QApplication:
    app = QApplication.instance()
    if app is None:
        app = QApplication([])
    return app


def _display_plan(
    *axis_updates: AxisFieldPresentation,
    missing_axes: tuple[str, ...] = (),
    homed_axes: frozenset[str] = frozenset(),
    fields_available: bool = True,
) -> StagePositionDisplayPlan:
    return StagePositionDisplayPlan(
        valid=True,
        homed_axes=homed_axes,
        axis_updates=axis_updates,
        missing_axes=missing_axes,
        reset_all=False,
        fields_available=fields_available,
    )


def test_widget_construction_matches_existing_stage_position_controls(
    qt_app: QApplication,
) -> None:
    panel = StagePositionPanel(("X", "Y", "Z", "A", "B", "C"))

    assert tuple(panel.axis_fields) == ("X", "Y", "Z", "A", "B", "C")
    assert not panel.apply_button.isEnabled()
    assert not panel.cancel_button.isEnabled()
    assert panel.apply_button.toolTip() == "Apply changed coordinate fields as one move."
    assert (
        panel.cancel_button.toolTip()
        == "Clear edited fields or cancel the active stage workflow."
    )
    assert panel.input_mode_combo.toolTip() == (
        "Coordinate input mode. Idle fields always show absolute coordinates."
    )
    assert panel.input_mode_combo.itemText(0) == "Absolute"
    assert panel.input_mode_combo.itemData(0) == "G90"
    assert panel.input_mode_combo.itemText(1) == "Relative"
    assert panel.input_mode_combo.itemData(1) == "G91"

    for axis_name, field in panel.axis_fields.items():
        assert field.placeholderText() == "---"
        assert field.toolTip() == (
            f"Current {axis_name} coordinate. Enter target and press Enter."
        )
        validator = field.validator()
        assert isinstance(validator, QDoubleValidator)
        assert validator.bottom() == -1000000.0
        assert validator.top() == 1000000.0
        assert validator.decimals() == 6
        assert validator.notation() == QDoubleValidator.StandardNotation
        assert validator.locale().name() == "C"
        assert not field.isEnabled()

    panel.deleteLater()


def _coordinate_plan(
    *,
    selected_frame_id: str = "machine",
    entries: tuple[CoordinateSelectorEntry, ...] | None = None,
    updates: tuple[CoordinateAxisDisplay, ...] = (),
) -> CoordinateDisplayPlan:
    return CoordinateDisplayPlan(
        selected_frame_id=selected_frame_id,
        selector_entries=entries
        or (
            CoordinateSelectorEntry("machine", "Machine", "Machine", True),
            CoordinateSelectorEntry(
                "11111111-1111-4111-8111-111111111111",
                "chip-a",
                "Designs",
                True,
            ),
            CoordinateSelectorEntry(
                "22222222-2222-4222-8222-222222222222",
                "fixture",
                "Custom",
                False,
                "Home X and Y to use this coordinate system.",
            ),
        ),
        axis_updates=updates,
    )


def test_coordinate_selector_is_immediately_after_position_and_groups_disabled_headings(
    qt_app: QApplication,
) -> None:
    panel = StagePositionPanel(("X", "Y"))
    panel.set_coordinate_display_plan(_coordinate_plan())

    row = panel.layout().itemAt(0).layout()
    assert isinstance(row.itemAt(0).widget(), QLabel)
    assert row.itemAt(0).widget().text() == "Position:"
    assert row.itemAt(1).widget() is panel.coordinate_system_combo
    assert [
        panel.coordinate_system_combo.itemText(index)
        for index in range(panel.coordinate_system_combo.count())
    ] == ["Machine", "Machine", "Designs", "chip-a", "Custom", "fixture"]
    for index in (0, 2, 4):
        item = panel.coordinate_system_combo.model().item(index)
        assert not bool(item.flags() & Qt.ItemIsEnabled)
        assert not bool(item.flags() & Qt.ItemIsSelectable)
        assert panel.coordinate_system_combo.itemData(index) is None
    assert panel.coordinate_system_combo.itemData(1) == "machine"

    panel.deleteLater()


def test_coordinate_selector_emits_stable_id_only_for_user_activated_frame(
    qt_app: QApplication,
) -> None:
    panel = StagePositionPanel(("X",))
    panel.set_coordinate_display_plan(_coordinate_plan())
    selected: list[str] = []
    panel.coordinate_system_changed.connect(selected.append)

    panel.coordinate_system_combo.setCurrentIndex(3)
    panel.coordinate_system_combo.activated.emit(3)

    assert selected == ["11111111-1111-4111-8111-111111111111"]
    panel.deleteLater()


def test_coordinate_display_updates_existing_fields_with_blue_and_yellow_reasons(
    qt_app: QApplication,
) -> None:
    panel = StagePositionPanel(("X", "Y"))
    x_field = panel.axis_fields["X"]
    y_field = panel.axis_fields["Y"]

    panel.set_coordinate_display_plan(
        _coordinate_plan(
            updates=(
                CoordinateAxisDisplay("X", 1.25, "available", "X is registered."),
                CoordinateAxisDisplay("Y", None, "unavailable", "Register Y."),
            )
        )
    )

    assert panel.axis_fields["X"] is x_field
    assert panel.axis_fields["Y"] is y_field
    assert x_field.text() == "1.25"
    assert "background-color: #1565c0" in x_field.styleSheet()
    assert x_field.toolTip() == "X is registered."
    assert y_field.text() == ""
    assert y_field.placeholderText() == "---"
    assert "background-color: #f0b429" in y_field.styleSheet()
    assert y_field.toolTip() == "Register Y."

    panel.deleteLater()


def test_non_machine_coordinate_fields_are_display_only_until_motion_resolution_exists(
    qt_app: QApplication,
) -> None:
    frame_id = "11111111-1111-4111-8111-111111111111"
    panel = StagePositionPanel(("X", "Y"))
    panel.set_coordinate_display_plan(_coordinate_plan())
    panel.set_pending_target("X", 99.0, 1.25)
    panel.set_action_buttons_enabled(True, True)

    panel.set_coordinate_display_plan(
        _coordinate_plan(
            selected_frame_id=frame_id,
            updates=(
                CoordinateAxisDisplay("X", 1.25, "available", "X is registered."),
                CoordinateAxisDisplay("Y", 2.5, "available", "Y is registered."),
            ),
        )
    )

    assert all(field.isEnabled() for field in panel.axis_fields.values())
    assert all(field.isReadOnly() for field in panel.axis_fields.values())
    assert panel.pending_targets == {}
    assert not panel.input_mode_combo.isEnabled()
    assert not panel.apply_button.isEnabled()

    panel.set_coordinate_display_plan(_coordinate_plan())

    assert all(not field.isReadOnly() for field in panel.axis_fields.values())
    assert panel.input_mode_combo.isEnabled()
    panel.deleteLater()


def test_selector_refresh_preserves_stable_selection_across_version_or_name_change(
    qt_app: QApplication,
) -> None:
    frame_id = "11111111-1111-4111-8111-111111111111"
    panel = StagePositionPanel(("X",))
    panel.set_coordinate_display_plan(
        _coordinate_plan(selected_frame_id=frame_id)
    )
    panel.set_coordinate_display_plan(
        _coordinate_plan(
            selected_frame_id=frame_id,
            entries=(
                CoordinateSelectorEntry("machine", "Machine", "Machine", True),
                CoordinateSelectorEntry(frame_id, "chip-renamed", "Designs", True),
            ),
        )
    )

    assert panel.coordinate_system_combo.currentData() == frame_id
    assert panel.coordinate_system_combo.currentText() == "chip-renamed"
    panel.deleteLater()


def _ready_frame_without_a(frame_id: str) -> CoordinateFrameRecord:
    return CoordinateFrameRecord(
        frame_id=frame_id,
        kind=FrameKind.DESIGN,
        name="chip-a",
        version=0,
        transform=BFrameTransform(
            origin_xy_at_reference_b=(0.0, 0.0),
            reference_b_deg=0.0,
            xy_angle_at_reference_b_deg=0.0,
            b_zero_machine_deg=0.0,
            z_zero_machine_mm=1.0,
            a_zero_machine_mm=None,
        ),
        readiness={
            axis: AxisReadiness(
                ReadinessStatus.READY
                if axis in {"X", "Y", "Z", "B"}
                else ReadinessStatus.MISSING,
                "" if axis in {"X", "Y", "Z", "B"} else "Find contact.",
            )
            for axis in ("X", "Y", "Z", "A", "B")
        },
        metadata={},
    )


class _SelectionSettingsManager:
    def __init__(self, selected: str) -> None:
        self.settings = SimpleNamespace(
            software_coordinates=SimpleNamespace(
                last_selected_frame_id=selected,
                pivot=SimpleNamespace(x_mm=0.0, y_mm=0.0),
            )
        )
        self.in_memory_updates: list[str] = []

    def set_software_coordinate_selection(
        self,
        frame_id: str,
    ) -> SoftwareCoordinateSelectionSnapshot:
        self.settings.software_coordinates.last_selected_frame_id = frame_id
        self.in_memory_updates.append(frame_id)
        return SoftwareCoordinateSelectionSnapshot(frame_id, len(self.in_memory_updates))

    def update_and_save(self, *_args, **_kwargs) -> None:
        raise AssertionError("selector callback must not synchronously save settings")


def test_gui_restore_selects_ready_through_z_even_when_a_is_missing(
    qt_app: QApplication,
) -> None:
    frame_id = "11111111-1111-4111-8111-111111111111"
    registry = CoordinateFrameRegistry()
    registry.add(_ready_frame_without_a(frame_id))
    panel = StagePositionPanel(("X", "Y", "Z", "A", "B"))
    owner = SimpleNamespace(
        _stage_position_panel=panel,
        _coordinate_frame_registry=registry,
        _coordinate_frames_loaded=True,
        _selected_coordinate_frame_id="machine",
        _pending_coordinate_frame_restore_id=frame_id,
        settings_manager=_SelectionSettingsManager(frame_id),
        stage_controller=SimpleNamespace(homed_axes=lambda: {"X", "Y"}),
    )
    pose = PhysicalMachinePose({"X": 0.0, "Y": 0.0, "Z": 1.0, "A": 2.0, "B": 0.0})

    panel_adapter.update_software_coordinate_display(owner, pose)

    assert owner._selected_coordinate_frame_id == frame_id
    assert owner._pending_coordinate_frame_restore_id is None
    assert panel.coordinate_system_combo.currentData() == frame_id
    assert "background-color: #1565c0" in panel.axis_fields["Z"].styleSheet()
    assert "background-color: #f0b429" in panel.axis_fields["A"].styleSheet()
    panel.deleteLater()


def test_explicit_gui_selection_cancels_pending_restore_and_persists_locally() -> None:
    frame_id = "11111111-1111-4111-8111-111111111111"
    registry = CoordinateFrameRegistry()
    registry.add(_ready_frame_without_a(frame_id))
    manager = _SelectionSettingsManager(frame_id)
    published: list[SoftwareCoordinateSelectionSnapshot] = []
    owner = SimpleNamespace(
        _coordinate_frame_registry=registry,
        _coordinate_frames_loaded=True,
        _selected_coordinate_frame_id="machine",
        _pending_coordinate_frame_restore_id=frame_id,
        _api_coordinate_frame_id="api-frame-must-not-change",
        _latest_physical_machine_pose=PhysicalMachinePose(
            {"X": 0.0, "Y": 0.0, "Z": 1.0, "A": 2.0, "B": 0.0}
        ),
        _stage_position_panel=None,
        _software_coordinate_selection_store=SimpleNamespace(publish=published.append),
        settings_manager=manager,
        stage_controller=SimpleNamespace(homed_axes=lambda: {"X", "Y"}),
    )

    panel_adapter.select_gui_coordinate_frame(owner, "machine")

    assert owner._selected_coordinate_frame_id == "machine"
    assert owner._pending_coordinate_frame_restore_id is None
    assert owner.settings_manager.in_memory_updates == ["machine"]
    assert manager.in_memory_updates == ["machine"]
    assert published == [SoftwareCoordinateSelectionSnapshot("machine", 1)]
    assert owner._api_coordinate_frame_id == "api-frame-must-not-change"


def test_deleted_selected_frame_falls_back_safely_without_rearming_restore() -> None:
    owner = SimpleNamespace(
        _stage_position_panel=None,
        _coordinate_frame_registry=CoordinateFrameRegistry(),
        _coordinate_frames_loaded=True,
        _selected_coordinate_frame_id="11111111-1111-4111-8111-111111111111",
        _pending_coordinate_frame_restore_id=None,
        settings_manager=_SelectionSettingsManager("11111111-1111-4111-8111-111111111111"),
        stage_controller=SimpleNamespace(homed_axes=lambda: {"X", "Y"}),
    )

    panel_adapter.update_software_coordinate_display(
        owner,
        PhysicalMachinePose({"X": 0.0, "Y": 0.0, "Z": 1.0, "A": 2.0, "B": 0.0}),
    )

    assert owner._selected_coordinate_frame_id == "machine"
    assert owner._pending_coordinate_frame_restore_id is None
    assert owner.settings_manager.in_memory_updates == ["machine"]


def test_missing_fresh_machine_axis_clears_stale_value_and_shows_exact_yellow_reason(
    qt_app: QApplication,
) -> None:
    panel = StagePositionPanel(("X", "Y", "Z", "A", "B"))
    owner = SimpleNamespace(
        _stage_position_panel=panel,
        _coordinate_frame_registry=CoordinateFrameRegistry(),
        _coordinate_frames_loaded=True,
        _selected_coordinate_frame_id="machine",
        _pending_coordinate_frame_restore_id=None,
        _stage_axis_display_values={},
        settings_manager=_SelectionSettingsManager("machine"),
        stage_controller=SimpleNamespace(homed_axes=lambda: {"X", "Y", "Z", "A"}),
    )
    panel_adapter.update_software_coordinate_display(
        owner,
        PhysicalMachinePose({"X": 12.0, "Y": 2.0, "Z": 3.0, "A": 4.0, "B": 5.0}),
    )
    assert panel.axis_fields["X"].text() == "12"

    panel_adapter.update_software_coordinate_display(
        owner,
        PhysicalMachinePose({"Y": 2.0, "Z": 3.0, "A": 4.0, "B": 5.0}),
    )

    assert panel.axis_fields["X"].text() == ""
    assert "background-color: #f0b429" in panel.axis_fields["X"].styleSheet()
    assert panel.axis_fields["X"].toolTip() == (
        "Physical Machine X coordinate is unavailable."
    )
    assert owner._stage_axis_display_values == {"Y": 2.0, "Z": 3.0, "A": 4.0, "B": 5.0}
    panel.deleteLater()


def _connect_synchronized_snapshot_presentation(
    owner: SimpleNamespace,
    controller: StageController,
) -> None:
    def refresh(_legacy_position: object) -> None:
        pose = physical_machine_pose_from_controller(
            controller,
            ("X", "Y", "Z", "A", "B"),
        ) or PhysicalMachinePose({})
        owner._latest_physical_machine_pose = pose
        panel_adapter.update_software_coordinate_display(owner, pose)

    controller.stage_position_changed = SimpleNamespace(emit=refresh)


def _status_with_synchronized_machine(
    synchronized_machine_position: tuple[float, ...] | None,
) -> _Status:
    display = (5.0, 2.0, 3.0, 4.0, 5.0)
    return _Status(
        state="Idle",
        display_position=display,
        work_position=display,
        synchronized_machine_position=synchronized_machine_position,
    )


def test_same_wpos_missing_wco_clears_previous_blue_machine_values(
    qt_app: QApplication,
) -> None:
    controller = StageController()
    panel = StagePositionPanel(("X", "Y", "Z", "A", "B"))
    owner = SimpleNamespace(
        _stage_position_panel=panel,
        _coordinate_frame_registry=CoordinateFrameRegistry(),
        _coordinate_frames_loaded=True,
        _selected_coordinate_frame_id="machine",
        _pending_coordinate_frame_restore_id=None,
        _stage_axis_display_values={},
        settings_manager=_SelectionSettingsManager("machine"),
        stage_controller=controller,
    )
    controller.apply_axis_calibrations(default_axis_calibrations())
    controller._homed_axes = {"X", "Y", "Z", "A"}
    _connect_synchronized_snapshot_presentation(owner, controller)
    try:
        controller._update_cached_positions(
            _status_with_synchronized_machine((15.0, 22.0, 3.0, 4.0, 5.0))
        )
        assert panel.axis_fields["X"].text() == "15"
        assert "background-color: #1565c0" in panel.axis_fields["X"].styleSheet()

        controller._update_cached_positions(_status_with_synchronized_machine(None))

        assert panel.axis_fields["X"].text() == ""
        assert "background-color: #f0b429" in panel.axis_fields["X"].styleSheet()
    finally:
        panel.deleteLater()
        controller.shutdown()


def test_same_wpos_new_synchronized_snapshot_completes_pending_restore() -> None:
    frame_id = "11111111-1111-4111-8111-111111111111"
    registry = CoordinateFrameRegistry()
    registry.add(_ready_frame_without_a(frame_id))
    controller = StageController()
    plans: list[CoordinateDisplayPlan] = []
    owner = SimpleNamespace(
        _stage_position_panel=SimpleNamespace(set_coordinate_display_plan=plans.append),
        _coordinate_frame_registry=registry,
        _coordinate_frames_loaded=True,
        _selected_coordinate_frame_id="machine",
        _pending_coordinate_frame_restore_id=frame_id,
        _stage_axis_display_values={},
        settings_manager=_SelectionSettingsManager(frame_id),
        stage_controller=controller,
    )
    controller.apply_axis_calibrations(default_axis_calibrations())
    controller._homed_axes = {"X", "Y", "Z", "A"}
    _connect_synchronized_snapshot_presentation(owner, controller)
    try:
        controller._update_cached_positions(_status_with_synchronized_machine(None))
        assert owner._pending_coordinate_frame_restore_id == frame_id
        assert owner._selected_coordinate_frame_id == "machine"

        controller._update_cached_positions(
            _status_with_synchronized_machine((0.0, 0.0, 1.0, 2.0, 0.0))
        )

        assert owner._pending_coordinate_frame_restore_id is None
        assert owner._selected_coordinate_frame_id == frame_id
        assert plans[-1].selected_frame_id == frame_id
    finally:
        controller.shutdown()


def test_same_wpos_changed_machine_snapshot_refreshes_displayed_value(
    qt_app: QApplication,
) -> None:
    controller = StageController()
    panel = StagePositionPanel(("X", "Y", "Z", "A", "B"))
    owner = SimpleNamespace(
        _stage_position_panel=panel,
        _coordinate_frame_registry=CoordinateFrameRegistry(),
        _coordinate_frames_loaded=True,
        _selected_coordinate_frame_id="machine",
        _pending_coordinate_frame_restore_id=None,
        _stage_axis_display_values={},
        settings_manager=_SelectionSettingsManager("machine"),
        stage_controller=controller,
    )
    controller.apply_axis_calibrations(default_axis_calibrations())
    controller._homed_axes = {"X", "Y", "Z", "A"}
    _connect_synchronized_snapshot_presentation(owner, controller)
    try:
        controller._update_cached_positions(
            _status_with_synchronized_machine((15.0, 22.0, 3.0, 4.0, 5.0))
        )
        controller._update_cached_positions(
            _status_with_synchronized_machine((16.0, 22.0, 3.0, 4.0, 5.0))
        )

        assert panel.axis_fields["X"].text() == "16"
    finally:
        panel.deleteLater()
        controller.shutdown()


def test_refresh_accepts_valid_pose_value_across_module_reload_boundary(
    qt_app: QApplication,
) -> None:
    plans: list[object] = []
    reloaded_pose_type = type(
        "PhysicalMachinePose",
        (),
        {"__module__": "probe_station_gui.coordinates.model"},
    )
    reloaded_pose = reloaded_pose_type()
    reloaded_pose.values = {"X": 1.0, "Y": 2.0, "Z": 3.0, "A": 4.0, "B": 5.0}
    owner = SimpleNamespace(
        _stage_position_panel=SimpleNamespace(set_coordinate_display_plan=plans.append),
        _coordinate_frame_registry=CoordinateFrameRegistry(),
        _coordinate_frames_loaded=True,
        _selected_coordinate_frame_id="machine",
        _pending_coordinate_frame_restore_id=None,
        _stage_axis_display_values={},
        _latest_physical_machine_pose=reloaded_pose,
        settings_manager=_SelectionSettingsManager("machine"),
        stage_controller=SimpleNamespace(homed_axes=lambda: {"X", "Y", "Z", "A"}),
    )

    panel_adapter.refresh_coordinate_frame_display(owner)

    assert plans[-1].selected_frame_id == "machine"


def _reloaded_pose(values: object) -> object:
    pose_type = type(
        "PhysicalMachinePose",
        (),
        {"__module__": "probe_station_gui.coordinates.model"},
    )
    pose = pose_type()
    pose.values = values
    return pose


def _reloaded_pose_without_values() -> object:
    pose_type = type(
        "PhysicalMachinePose",
        (),
        {"__module__": "probe_station_gui.coordinates.model"},
    )
    return pose_type()


def _reloaded_pose_with_raising_values() -> object:
    def raise_values(_self) -> object:
        raise RuntimeError("broken reload value")

    pose_type = type(
        "PhysicalMachinePose",
        (),
        {
            "__module__": "probe_station_gui.coordinates.model",
            "values": property(raise_values),
        },
    )
    return pose_type()


@pytest.mark.parametrize(
    "pose_factory",
    [
        pytest.param(object, id="unexpected-type"),
        pytest.param(_reloaded_pose_without_values, id="missing-values"),
        pytest.param(_reloaded_pose_with_raising_values, id="raising-values"),
        pytest.param(lambda: _reloaded_pose([]), id="non-mapping-values"),
        pytest.param(lambda: _reloaded_pose({"X": 1.0}), id="partial-values"),
        pytest.param(
            lambda: _reloaded_pose(
                {"X": float("nan"), "Y": 2.0, "Z": 3.0, "A": 4.0, "B": 5.0}
            ),
            id="nonfinite-values",
        ),
        pytest.param(
            lambda: _reloaded_pose(
                {"X": 1.0, "Y": 2.0, "Z": 3.0, "A": 4.0, "Q": 5.0}
            ),
            id="unexpected-axis",
        ),
    ],
)
def test_malformed_reloaded_pose_clears_stale_blue_values_without_raising(
    qt_app: QApplication,
    pose_factory,
) -> None:
    panel = StagePositionPanel(("X", "Y", "Z", "A", "B"))
    owner = SimpleNamespace(
        _stage_position_panel=panel,
        _coordinate_frame_registry=CoordinateFrameRegistry(),
        _coordinate_frames_loaded=True,
        _selected_coordinate_frame_id="machine",
        _pending_coordinate_frame_restore_id=None,
        _stage_axis_display_values={},
        _latest_physical_machine_pose=None,
        settings_manager=_SelectionSettingsManager("machine"),
        stage_controller=SimpleNamespace(homed_axes=lambda: {"X", "Y", "Z", "A"}),
    )
    panel_adapter.update_software_coordinate_display(
        owner,
        PhysicalMachinePose({"X": 99.0, "Y": 2.0, "Z": 3.0, "A": 4.0, "B": 5.0}),
    )
    assert panel.axis_fields["X"].text() == "99"
    assert "background-color: #1565c0" in panel.axis_fields["X"].styleSheet()
    owner._latest_physical_machine_pose = pose_factory()

    panel_adapter.refresh_coordinate_frame_display(owner)

    assert owner._stage_axis_display_values == {}
    for axis, field in panel.axis_fields.items():
        assert field.text() == ""
        assert "background-color: #f0b429" in field.styleSheet()
        assert field.toolTip() == f"Physical Machine {axis} coordinate is unavailable."
    panel.deleteLater()


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        (0.0, "0"),
        (0.00049, "0"),
        (-0.00049, "0"),
        (1.23456, "1.235"),
        (-10.5, "-10.5"),
        (2.30001, "2.3"),
    ],
)
def test_format_stage_axis_value_matches_existing_rounding(
    value: float,
    expected: str,
) -> None:
    assert format_stage_axis_value(value) == expected


def test_set_fields_available_false_clears_disables_and_styles_fields(
    qt_app: QApplication,
) -> None:
    panel = StagePositionPanel(("X", "Y"))
    plan = _display_plan(
        AxisFieldPresentation("X", 1.0, 1.0, 1.0, "#1565c0", "#f5f5f5", "x"),
        AxisFieldPresentation("Y", 2.0, 2.0, 2.0, "#f0b429", "#1f1f1f", "y"),
    )
    panel.apply_display_plan(plan)
    panel.axis_fields["X"].setText("9.9")
    panel.axis_fields["X"].setModified(True)

    panel.set_fields_available(False)

    for field in panel.axis_fields.values():
        assert field.text() == ""
        assert field.placeholderText() == "---"
        assert not field.isEnabled()
        assert not field.isModified()
        style = field.styleSheet()
        assert "background-color: #e6e6e6" in style
        assert "color: #666666" in style
        assert "border: 1px solid #e6e6e6" in style

    panel.deleteLater()


def test_apply_display_plan_preserves_focused_modified_pending_text(
    qt_app: QApplication,
) -> None:
    panel = StagePositionPanel(("X", "Y"))
    panel.show()
    qt_app.processEvents()
    panel.set_pending_target("X", 7.5, 7.777)
    field = panel.axis_fields["X"]
    field.setEnabled(True)
    field.setFocus()
    qt_app.processEvents()
    field.setText("7.777")
    field.setModified(True)

    panel.apply_display_plan(
        _display_plan(
            AxisFieldPresentation(
                "X",
                1.0,
                1.0,
                7.777,
                "#1565c0",
                "#f5f5f5",
                "X axis",
            ),
            AxisFieldPresentation(
                "Y",
                2.0,
                2.0,
                2.0,
                "#1565c0",
                "#f5f5f5",
                "Y axis",
            ),
            homed_axes=frozenset({"X", "Y"}),
        )
    )

    assert field.text() == "7.777"
    assert field.isModified()
    assert field.toolTip() == "X axis"

    panel.deleteLater()


def test_clear_pending_target_state_preserves_uncommitted_focused_text(
    qt_app: QApplication,
) -> None:
    panel = StagePositionPanel(("X", "Y"))
    panel.apply_display_plan(
        _display_plan(
            AxisFieldPresentation("X", 1.0, 1.0, 1.0, "#1565c0", "#f5f5f5", "X axis"),
            AxisFieldPresentation("Y", 2.0, 2.0, 2.0, "#1565c0", "#f5f5f5", "Y axis"),
        )
    )
    panel.set_pending_target("X", 8.0, 8.0)
    y_field = panel.axis_fields["Y"]
    y_field.setFocus()
    qt_app.processEvents()
    y_field.setText("7.777")
    y_field.setModified(True)

    had_changes = panel.clear_pending_target_state()

    assert had_changes
    assert panel.pending_targets == {}
    assert y_field.text() == "7.777"
    assert y_field.isModified()

    panel.deleteLater()


def test_limit_base_style_overrides_homed_and_unhomed_styles(
    qt_app: QApplication,
) -> None:
    panel = StagePositionPanel(("X",))

    panel.apply_display_plan(
        _display_plan(
            AxisFieldPresentation(
                "X",
                1.0,
                1.0,
                1.0,
                "#c62828",
                "#ffffff",
                "X axis",
            ),
            homed_axes=frozenset({"X"}),
        )
    )

    style = panel.axis_fields["X"].styleSheet()
    assert "background-color: #c62828" in style
    assert "color: #ffffff" in style

    panel.deleteLater()


def test_pending_target_style_overrides_base_style(
    qt_app: QApplication,
) -> None:
    panel = StagePositionPanel(("X",))
    panel.apply_display_plan(
        _display_plan(
            AxisFieldPresentation(
                "X",
                1.0,
                1.0,
                1.0,
                "#1565c0",
                "#f5f5f5",
                "X axis",
            )
        )
    )

    panel.set_pending_target("X", 4.0, 4.25)

    style = panel.axis_fields["X"].styleSheet()
    assert "background-color: #d7b8ff" in style
    assert "color: #1f1233" in style

    panel.deleteLater()


def test_confidence_stripe_coexists_with_edited_target_fill(
    qt_app: QApplication,
) -> None:
    panel = StagePositionPanel(("X", "Y", "Z"))
    panel.apply_display_plan(
        _display_plan(
            AxisFieldPresentation(
                "X", 1.0, 1.0, 1.0, "#1565c0", "#f5f5f5", "X axis", "exact"
            ),
            AxisFieldPresentation(
                "Y",
                2.0,
                2.0,
                2.0,
                "#1565c0",
                "#f5f5f5",
                "Y axis",
                "approximate",
            ),
            AxisFieldPresentation(
                "Z", 3.0, 3.0, 3.0, "#c62828", "#ffffff", "Z axis", None
            ),
        )
    )
    panel.set_pending_target("X", 4.0, 4.25)

    x_style = panel.axis_fields["X"].styleSheet()
    y_style = panel.axis_fields["Y"].styleSheet()
    z_style = panel.axis_fields["Z"].styleSheet()
    assert "background-color: #d7b8ff" in x_style
    assert "border-bottom: 4px solid #2e7d32" in x_style
    assert "border-bottom: 4px solid #d32f2f" in y_style
    assert "border-bottom: 4px solid" not in z_style

    panel.deleteLater()


def test_confidence_update_restyles_only_the_changed_axis(
    qt_app: QApplication,
) -> None:
    panel = StagePositionPanel(("X", "Y"))
    panel.apply_display_plan(
        _display_plan(
            AxisFieldPresentation(
                "X", 1.0, 1.0, 1.0, "#1565c0", "#f5f5f5", "X axis", "approximate"
            ),
            AxisFieldPresentation(
                "Y", 2.0, 2.0, 2.0, "#1565c0", "#f5f5f5", "Y axis", "exact"
            ),
        )
    )
    original_y_style = panel.axis_fields["Y"].styleSheet()

    panel.update_confidence_roles({"X": "exact"})

    assert "border-bottom: 4px solid #2e7d32" in panel.axis_fields["X"].styleSheet()
    assert panel.axis_fields["Y"].styleSheet() == original_y_style
    panel.deleteLater()


def test_position_legend_uses_aligned_semantic_groups(
    qt_app: QApplication,
) -> None:
    panel = StagePositionPanel(("X",))

    assert tuple(panel._legend_titles) == ("Field state:", "Accuracy:")
    assert tuple(item.label.text() for item in panel._legend_groups["Field state:"]) == (
        "Homed",
        "Unhomed",
        "Limit",
        "Edited",
    )
    assert tuple(item.label.text() for item in panel._legend_groups["Accuracy:"]) == (
        "Exact",
        "Approximate",
    )
    state_sizes = {
        (item.swatch.width(), item.swatch.height())
        for item in panel._legend_groups["Field state:"]
    }
    accuracy_sizes = {
        (item.swatch.width(), item.swatch.height())
        for item in panel._legend_groups["Accuracy:"]
    }
    assert state_sizes == {(10, 10)}
    assert accuracy_sizes == {(14, 4)}
    legend_layout = panel.legend_widget.layout()
    assert legend_layout.spacing() == 10
    item_layouts = tuple(
        layout
        for index in range(legend_layout.count())
        if (layout := legend_layout.itemAt(index).layout()) is not None
    )
    assert len(item_layouts) == 6
    assert {layout.spacing() for layout in item_layouts} == {4}
    assert "backlash" in panel.legend_widget.toolTip().lower()
    assert all(
        label.textFormat() != Qt.RichText
        and "font-size: 9px" not in label.styleSheet()
        for label in panel.legend_widget.findChildren(QLabel)
    )

    panel.deleteLater()


def test_motion_blink_dimming_maps_known_base_colors(
    qt_app: QApplication,
) -> None:
    panel = StagePositionPanel(("X", "Y", "Z", "A"))
    panel.apply_display_plan(
        _display_plan(
            AxisFieldPresentation("X", 1.0, 1.0, 1.0, "#1565c0", "#f5f5f5", "X"),
            AxisFieldPresentation("Y", 2.0, 2.0, 2.0, "#f0b429", "#1f1f1f", "Y"),
            AxisFieldPresentation("Z", 3.0, 3.0, 3.0, "#c62828", "#ffffff", "Z"),
            AxisFieldPresentation("A", 4.0, 4.0, 4.0, "#1565c0", "#f5f5f5", "A"),
        )
    )
    panel.set_pending_target("A", 4.0, 4.0)

    panel.refresh_axis_styles({"X", "Y", "Z", "A"}, True)

    assert "background-color: #6f9dd3" in panel.axis_fields["X"].styleSheet()
    assert "background-color: #f7d98a" in panel.axis_fields["Y"].styleSheet()
    assert "background-color: #e57373" in panel.axis_fields["Z"].styleSheet()
    assert "background-color: #d7b8ff" in panel.axis_fields["A"].styleSheet()

    panel.deleteLater()


def test_axis_signals_include_axis_name_and_programmatic_updates_do_not_emit(
    qt_app: QApplication,
) -> None:
    panel = StagePositionPanel(("X",))
    returned: list[str] = []
    escaped: list[str] = []
    finished: list[str] = []
    edited: list[str] = []
    panel.axis_return_pressed.connect(returned.append)
    panel.axis_escape_pressed.connect(escaped.append)
    panel.axis_editing_finished.connect(finished.append)
    panel.axis_text_edited.connect(edited.append)

    field = panel.axis_fields["X"]
    field.setEnabled(True)
    field.returnPressed.emit()
    panel._axis_escape_shortcuts["X"].activated.emit()
    field.textEdited.emit("1.0")
    field.editingFinished.emit()

    assert returned == ["X"]
    assert escaped == ["X"]
    assert edited == ["X"]
    assert finished == ["X"]

    panel.set_fields_available(False)
    panel.apply_display_plan(
        _display_plan(
            AxisFieldPresentation(
                "X",
                1.0,
                1.0,
                1.0,
                "#1565c0",
                "#f5f5f5",
                "X axis",
            )
        )
    )

    assert edited == ["X"]
    assert finished == ["X"]

    panel.deleteLater()


def test_selected_input_mode_normalizes_and_falls_back_to_g90(
    qt_app: QApplication,
) -> None:
    panel = StagePositionPanel(("X",))

    assert panel.selected_input_mode() == "G90"
    panel.input_mode_combo.setCurrentIndex(1)
    assert panel.selected_input_mode() == "G91"
    panel.input_mode_combo.setItemData(1, "relative")
    assert panel.selected_input_mode() == "G91"
    panel.input_mode_combo.setItemData(1, "bogus")
    assert panel.selected_input_mode() == "G90"

    panel.deleteLater()
