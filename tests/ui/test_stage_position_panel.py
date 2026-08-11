from __future__ import annotations

import inspect
import os
from types import SimpleNamespace

import pytest


os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
pytest.importorskip("PySide6")

from PySide6.QtCore import Qt
from PySide6.QtGui import QDoubleValidator
from PySide6.QtWidgets import QApplication, QLabel

from probe_station_gui.coordinates.coordinator_model import (
    CoordinateSystemSelection,
    CoordinateSystemSnapshot,
    CoordinateTransition,
)
from probe_station_gui.coordinates.presentation import (
    CoordinateAxisDisplay,
    CoordinateDisplayPlan,
    CoordinateSelectorEntry,
)
from probe_station_gui.stage.position_presenter import (
    AxisFieldPresentation,
    StagePositionDisplayPlan,
)
from probe_station_gui.views import main_window_coordinate_flow as coordinate_flow
from probe_station_gui.views import main_window_stage_position_panel as panel_adapter
from probe_station_gui.views.stage_position_panel import (
    StagePositionPanel,
    format_stage_axis_value,
)


@pytest.fixture(scope="module")
def qt_app() -> QApplication:
    app = QApplication.instance()
    return app if app is not None else QApplication([])


def _coordinate_plan(
    *,
    selected_frame_id: str = "machine",
    updates: tuple[CoordinateAxisDisplay, ...] = (),
    selection_available: bool = True,
    selection_reason: str | None = None,
) -> CoordinateDisplayPlan:
    return CoordinateDisplayPlan(
        selected_frame_id=selected_frame_id,
        selector_entries=(
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
        selection_available=selection_available,
        selection_reason=selection_reason,
    )


def _stage_plan(
    *updates: AxisFieldPresentation,
    homed_axes: frozenset[str] = frozenset(),
) -> StagePositionDisplayPlan:
    return StagePositionDisplayPlan(
        valid=True,
        homed_axes=homed_axes,
        axis_updates=updates,
        missing_axes=(),
        reset_all=False,
        fields_available=True,
    )


def test_widget_construction_keeps_primary_controls_laconic(
    qt_app: QApplication,
) -> None:
    panel = StagePositionPanel(("X", "Y", "Z", "A", "B"))
    assert tuple(panel.axis_fields) == ("X", "Y", "Z", "A", "B")
    assert not panel.apply_button.isEnabled()
    assert not panel.cancel_button.isEnabled()
    assert panel.input_mode_combo.itemData(0) == "G90"
    assert panel.input_mode_combo.itemData(1) == "G91"
    for axis, field in panel.axis_fields.items():
        assert field.placeholderText() == "---"
        assert field.toolTip() == (
            f"Current {axis} coordinate. Enter target and press Enter."
        )
        validator = field.validator()
        assert isinstance(validator, QDoubleValidator)
        assert not field.isEnabled()
    panel.deleteLater()


def test_coordinate_selector_groups_entries_and_emits_stable_id(
    qt_app: QApplication,
) -> None:
    panel = StagePositionPanel(("X",))
    panel.set_coordinate_display_plan(_coordinate_plan())
    row = panel.layout().itemAt(0).layout()
    assert isinstance(row.itemAt(0).widget(), QLabel)
    assert [
        panel.coordinate_system_combo.itemText(index)
        for index in range(panel.coordinate_system_combo.count())
    ] == ["Machine", "Machine", "Designs", "chip-a", "Custom", "fixture"]
    for index in (0, 2, 4):
        item = panel.coordinate_system_combo.model().item(index)
        assert not bool(item.flags() & Qt.ItemIsEnabled)
        assert panel.coordinate_system_combo.itemData(index) is None
    selected: list[str] = []
    panel.coordinate_system_changed.connect(selected.append)
    panel.coordinate_system_combo.activated.emit(3)
    assert selected == ["11111111-1111-4111-8111-111111111111"]
    panel.deleteLater()


def test_coordinate_display_reuses_fields_and_exposes_readiness_reason(
    qt_app: QApplication,
) -> None:
    panel = StagePositionPanel(("X", "Y"))
    x_field = panel.axis_fields["X"]
    panel.set_coordinate_display_plan(
        _coordinate_plan(
            updates=(
                CoordinateAxisDisplay("X", 1.25, "available", "X is registered."),
                CoordinateAxisDisplay("Y", None, "unavailable", "Register Y."),
            )
        )
    )
    assert panel.axis_fields["X"] is x_field
    assert x_field.text() == "1.25"
    assert "background-color: #1565c0" in x_field.styleSheet()
    assert panel.axis_fields["Y"].text() == ""
    assert "background-color: #f0b429" in panel.axis_fields["Y"].styleSheet()
    assert panel.axis_fields["Y"].toolTip() == "Register Y."
    panel.deleteLater()


def test_available_non_machine_coordinate_fields_are_editable(
    qt_app: QApplication,
) -> None:
    panel = StagePositionPanel(("X", "Y"))
    panel.set_coordinate_display_plan(
        _coordinate_plan(
            selected_frame_id="11111111-1111-4111-8111-111111111111",
            updates=(
                CoordinateAxisDisplay("X", 1.0, "available", "X"),
                CoordinateAxisDisplay("Y", 2.0, "available", "Y"),
            ),
        )
    )
    assert all(not field.isReadOnly() for field in panel.axis_fields.values())
    assert panel.input_mode_combo.isEnabled()
    assert not panel.apply_button.isEnabled()
    panel.deleteLater()


def test_hard_limit_style_overlays_selected_coordinate_readiness(
    qt_app: QApplication,
) -> None:
    panel = StagePositionPanel(("X",))
    plan = _coordinate_plan(
        selected_frame_id="11111111-1111-4111-8111-111111111111",
        updates=(CoordinateAxisDisplay("X", 1.0, "available", "X"),),
    )

    panel.set_limit_axes({"X"})
    panel.set_coordinate_display_plan(plan)

    assert "background-color: #c62828" in panel.axis_fields["X"].styleSheet()
    panel.set_limit_axes(set())
    assert "background-color: #1565c0" in panel.axis_fields["X"].styleSheet()
    panel.deleteLater()


def test_unavailable_non_machine_coordinate_fields_are_read_only(
    qt_app: QApplication,
) -> None:
    panel = StagePositionPanel(("X", "Y"))
    panel.set_coordinate_display_plan(
        _coordinate_plan(
            selected_frame_id="22222222-2222-4222-8222-222222222222",
            updates=(
                CoordinateAxisDisplay("X", None, "unavailable", "Home X and Y."),
                CoordinateAxisDisplay("Y", None, "unavailable", "Home X and Y."),
            ),
            selection_available=False,
            selection_reason="Home X and Y.",
        )
    )
    assert all(field.isReadOnly() for field in panel.axis_fields.values())
    assert not panel.input_mode_combo.isEnabled()
    assert not panel.apply_button.isEnabled()
    panel.deleteLater()


def test_adapter_renders_finished_snapshot_without_policy_or_shadow_owner() -> None:
    plan = _coordinate_plan(
        updates=(
            CoordinateAxisDisplay("X", 1.25, "available", "X"),
            CoordinateAxisDisplay("Y", None, "unavailable", "Y"),
        )
    )
    rendered: list[CoordinateDisplayPlan] = []
    owner = SimpleNamespace(
        _stage_position_panel=SimpleNamespace(
            set_coordinate_display_plan=rendered.append
        ),
        _stage_axis_display_values={"stale": 9.0},
    )
    snapshot = CoordinateSystemSnapshot(False, (), None, display_plan=plan)
    panel_adapter.render_coordinate_system_snapshot(owner, snapshot)
    assert rendered == [plan]
    assert owner._stage_axis_display_values == {"X": 1.25}
    source = inspect.getsource(panel_adapter)
    for forbidden in (
        "_coordinate_frame_registry",
        "_coordinate_frame_lifecycle",
        "_coordinate_frames_loaded",
        "FrameSelectionDecision",
        "build_coordinate_display_plan",
    ):
        assert forbidden not in source


def test_widget_creation_renders_current_snapshot_and_keeps_c_hidden(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class _Signal:
        def connect(self, _callback: object) -> None:
            pass

    plan = _coordinate_plan()
    rendered: list[CoordinateDisplayPlan] = []
    observed_axes: list[tuple[str, ...]] = []
    panel = SimpleNamespace(
        axis_escape_pressed=_Signal(),
        axis_editing_finished=_Signal(),
        axis_text_edited=_Signal(),
        input_mode_changed=_Signal(),
        apply_requested=_Signal(),
        cancel_requested=_Signal(),
        coordinate_system_changed=_Signal(),
        axis_fields={},
        base_styles={},
        pending_targets={},
        set_coordinate_display_plan=rendered.append,
    )

    def make_panel(axes: tuple[str, ...], _owner: object) -> object:
        observed_axes.append(tuple(axes))
        return panel

    monkeypatch.setattr(panel_adapter, "StagePositionPanel", make_panel)
    owner = SimpleNamespace(
        _on_stage_axis_escape_pressed=lambda _axis: None,
        _on_stage_axis_editing_finished=lambda _axis: None,
        _update_stage_coordinate_apply_state=lambda: None,
        _on_stage_coordinate_mode_changed=lambda: None,
        _apply_pending_stage_coordinate_targets=lambda: None,
        _coordinate_system_coordinator=SimpleNamespace(
            snapshot=lambda: CoordinateSystemSnapshot(
                False,
                (),
                None,
                display_plan=plan,
            )
        ),
        _stage_axis_display_values={},
    )

    assert panel_adapter.create_stage_position_widget(owner) is panel
    assert observed_axes == [("X", "Y", "Z", "A", "B")]
    assert rendered == [plan]


def test_adapter_selection_calls_coordinator_and_leaves_api_choice_untouched(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[object] = []
    transition = CoordinateTransition(CoordinateSystemSnapshot(False, (), None))

    class _Coordinator:
        def select_system(self, request: CoordinateSystemSelection):
            calls.append(request)
            return transition

    owner = SimpleNamespace(
        _coordinate_system_coordinator=_Coordinator(),
        _api_coordinate_frame_id="api-frame",
    )
    monkeypatch.setattr(
        coordinate_flow,
        "apply_coordinate_transition",
        lambda actual_owner, actual_transition: calls.append(
            (actual_owner, actual_transition)
        ),
    )
    panel_adapter.select_gui_coordinate_frame(
        owner,
        "11111111-1111-4111-8111-111111111111",
    )
    assert calls == [
        CoordinateSystemSelection("11111111-1111-4111-8111-111111111111"),
        (owner, transition),
    ]
    assert owner._api_coordinate_frame_id == "api-frame"


@pytest.mark.parametrize(
    ("value", "expected"),
    [(0.0, "0"), (0.00049, "0"), (-0.00049, "0"), (1.23456, "1.235")],
)
def test_format_stage_axis_value(value: float, expected: str) -> None:
    assert format_stage_axis_value(value) == expected


def test_stage_plan_preserves_focused_modified_pending_text(
    qt_app: QApplication,
) -> None:
    panel = StagePositionPanel(("X", "Y"))
    panel.show()
    qt_app.processEvents()
    panel.set_pending_target("X", 7.5, 7.777)
    field = panel.axis_fields["X"]
    field.setEnabled(True)
    field.setFocus()
    field.setText("7.777")
    field.setModified(True)
    panel.apply_display_plan(
        _stage_plan(
            AxisFieldPresentation("X", 1.0, 1.0, 7.777, "#1565c0", "#f5f5f5", "X"),
            AxisFieldPresentation("Y", 2.0, 2.0, 2.0, "#1565c0", "#f5f5f5", "Y"),
            homed_axes=frozenset({"X", "Y"}),
        )
    )
    assert field.text() == "7.777"
    assert field.isModified()
    panel.deleteLater()


def test_motion_blink_dimming_maps_semantic_base_colors() -> None:
    panel = StagePositionPanel(("X", "Y", "Z", "A"))
    panel.apply_display_plan(
        _stage_plan(
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


def test_selected_input_mode_normalizes_invalid_data() -> None:
    panel = StagePositionPanel(("X",))
    assert panel.selected_input_mode() == "G90"
    panel.input_mode_combo.setCurrentIndex(1)
    assert panel.selected_input_mode() == "G91"
    panel.input_mode_combo.setItemData(1, "bogus")
    assert panel.selected_input_mode() == "G90"
    panel.deleteLater()
