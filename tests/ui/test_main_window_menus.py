from __future__ import annotations

import os

import pytest


os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
pytest.importorskip("PySide6")

from PySide6.QtCore import Qt
from PySide6.QtGui import QKeySequence
from PySide6.QtWidgets import QApplication, QDockWidget, QMainWindow

from probe_station_gui.views import main_window_menus
from probe_station_gui.views.main_window_menus import setup_main_window_menus


@pytest.fixture(scope="module")
def qt_app() -> QApplication:
    app = QApplication.instance()
    if app is None:
        app = QApplication([])
    return app


class _MenuOwner(QMainWindow):
    ALIGNMENT_CAPTURE_SHORTCUT = "Space"

    def __init__(self) -> None:
        super().__init__()
        self.calls: list[tuple[str, object]] = []
        self.resistance_dock = QDockWidget("old resistance", self)
        self.oscillation_dock = QDockWidget("old oscillation", self)
        self.joystick_dock = QDockWidget("old joystick", self)
        self.alignment_dock = QDockWidget("old alignment", self)

        self._sample_load_action = None
        self._sample_unload_action = None
        self._design_layout_window_action = None
        self._contact_calibration_window_action = None
        self._surface_map_window_action = None
        self._microscope_scan_action = None
        self._click_calibration_action = None
        self._lens_distortion_calibration_action = None
        self._ruler_action = None
        self._rect_action = None
        self._alignment_capture_action = None
        self._alignment_exit_action = None

    def _record(self, name: str, value: object = None) -> None:
        self.calls.append((name, value))

    def _open_settings_dialog(self) -> None:
        self._record("settings")

    def _open_status_log(self) -> None:
        self._record("status_log")

    def _show_connection_dialog(self) -> None:
        self._record("connection")

    def _request_sample_load(self) -> None:
        self._record("load")

    def _request_sample_unload(self) -> None:
        self._record("unload")

    def _toggle_design_layout_window(self, visible: bool) -> None:
        self._record("design", bool(visible))

    def _toggle_contact_calibration_window(self, visible: bool) -> None:
        self._record("contact", bool(visible))

    def _show_surface_map_window(self) -> None:
        self._record("surface")

    def _show_microscope_scan_dialog(self) -> None:
        self._record("microscope")

    def _show_click_calibration_dialog(self) -> None:
        self._record("click_calibration")

    def _show_lens_distortion_dialog(self) -> None:
        self._record("lens_distortion")

    def _on_measure_action_toggled(self, checked: bool) -> None:
        self._record("measure", bool(checked))

    def _capture_manual_alignment_center_shortcut(self) -> None:
        self._record("capture")

    def _cancel_manual_alignment_pick(self) -> None:
        self._record("cancel_pick")

    def _on_measure_mode_exited(self) -> None:
        self._record("measure_exit")


def _visible_menu_action_labels(window: QMainWindow) -> list[str]:
    labels: list[str] = []
    for menu_action in window.menuBar().actions():
        menu = menu_action.menu()
        if menu is None:
            labels.append(menu_action.text())
            continue
        labels.append(menu.title())
        labels.extend(
            action.text()
            for action in menu.actions()
            if not action.isSeparator() and action.isVisible()
        )
    return labels


def test_setup_main_window_menus_preserves_labels_and_shortcuts(
    monkeypatch: pytest.MonkeyPatch,
    qt_app: QApplication,
) -> None:
    monkeypatch.setattr(
        main_window_menus,
        "toggle_design_layout_window",
        lambda owner, visible: owner._record("design", bool(visible)),
    )
    monkeypatch.setattr(
        main_window_menus,
        "toggle_contact_calibration_window",
        lambda owner, visible: owner._record("contact", bool(visible)),
    )
    monkeypatch.setattr(
        main_window_menus,
        "show_surface_map_window",
        lambda owner: owner._record("surface"),
    )
    monkeypatch.setattr(
        main_window_menus,
        "show_microscope_scan_dialog",
        lambda owner: owner._record("microscope"),
    )
    window = _MenuOwner()

    setup_main_window_menus(window)

    labels = _visible_menu_action_labels(window)
    assert labels == [
        "Application",
        "Settings",
        "Open Status Log",
        "Connection",
        "Navigation",
        "Load Sample",
        "Unload Sample",
        "Design Window",
        "Tools",
        "Resistance",
        "Oscillation",
        "Joystick",
        "Ruler",
        "Rectangle",
        "Calibration",
        "Contact / Stone Calibration",
        "Surface Map",
        "Microscope Scan",
        "Click-to-Move Calibration",
        "Lens Distortion Calibration",
        "Alignment",
    ]
    assert not any("..." in label or "\N{HORIZONTAL ELLIPSIS}" in label for label in labels)

    assert window._design_layout_window_action.isCheckable()
    assert window._contact_calibration_window_action.isCheckable()
    assert window._ruler_action.isCheckable()
    assert window._rect_action.isCheckable()
    assert window._ruler_action.shortcut() == QKeySequence("R")
    assert window._rect_action.shortcut() == QKeySequence("T")
    assert window._ruler_action.shortcutContext() == Qt.ApplicationShortcut
    assert window._rect_action.shortcutContext() == Qt.ApplicationShortcut
    assert window._alignment_capture_action.shortcut() == QKeySequence("Space")
    assert window._alignment_capture_action.shortcutContext() == Qt.ApplicationShortcut
    assert window._alignment_exit_action.shortcut() == QKeySequence(Qt.Key_Escape)
    assert window._alignment_exit_action.shortcutContext() == Qt.ApplicationShortcut

    window._design_layout_window_action.trigger()
    window._contact_calibration_window_action.trigger()
    window._surface_map_window_action.trigger()
    window._microscope_scan_action.trigger()
    window._lens_distortion_calibration_action.trigger()

    assert ("design", True) in window.calls
    assert ("contact", True) in window.calls
    assert ("surface", None) in window.calls
    assert ("microscope", None) in window.calls
    assert ("lens_distortion", None) in window.calls

    window.close()
