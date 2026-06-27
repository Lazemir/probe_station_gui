from __future__ import annotations

import os

import pytest


os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
pytest.importorskip("PySide6")

from PySide6.QtGui import QDoubleValidator
from PySide6.QtWidgets import QApplication

from probe_station_gui.stage.position_presenter import (
    AxisFieldPresentation,
    StagePositionDisplayPlan,
)
from probe_station_gui.views.stage_position_panel import (
    StagePositionPanel,
    format_stage_axis_value,
)


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
