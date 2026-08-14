from __future__ import annotations

import json
import gc
import os
import time
import types
from collections import Counter
from pathlib import Path

import pytest
from tests.app.route_run_execution_support import activate_route_run


os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
pytest.importorskip("PySide6")

from PySide6.QtCore import QCoreApplication, QEvent, QLocale  # noqa: E402
from PySide6.QtGui import QCloseEvent  # noqa: E402
from PySide6.QtWidgets import (  # noqa: E402
    QApplication,
    QCheckBox,
    QComboBox,
    QLabel,
    QLineEdit,
    QProgressBar,
    QPushButton,
)

from probe_station_gui.dialogs.route_measurement_dialog import (  # noqa: E402
    RouteMeasurementDialog,
)
from probe_station_gui.route.dialog_adapter import (  # noqa: E402
    RouteDialogHandlers,
    _create_route_measurement_dialog,
    route_dialog_handlers,
)
from probe_station_gui.route.runtime_presenter import (  # noqa: E402
    RouteRuntimePresentationSink,
)
from probe_station_gui.route.contact_quality import (  # noqa: E402
    RouteContactQuality,
    RouteMeasurementSample,
)
from probe_station_gui.route.measurement_records import (  # noqa: E402
    RouteMeasurementRecord,
)


@pytest.fixture(scope="module")
def qt_app() -> QApplication:
    QLocale.setDefault(QLocale.c())
    app = QApplication.instance()
    if app is None:
        app = QApplication([])
    return app


def _dialog(tmp_path: Path, *, route_point_count: int = 4) -> RouteMeasurementDialog:
    return RouteMeasurementDialog(
        route_name="route-a",
        route_point_count=route_point_count,
        default_csv_path=str(tmp_path / "default.csv"),
        default_photo_dir=str(tmp_path / "default-photos"),
        settings_path=tmp_path / "route-measurement-settings.json",
    )


def _button(dialog: RouteMeasurementDialog, text: str) -> QPushButton:
    matches = [
        button
        for button in dialog.findChildren(QPushButton)
        if button.text() == text and not button.isHidden()
    ]
    assert len(matches) == 1, (text, [button.text() for button in matches])
    return matches[0]


def _line_edit(dialog: RouteMeasurementDialog, placeholder: str) -> QLineEdit:
    matches = [
        line_edit
        for line_edit in dialog.findChildren(QLineEdit)
        if line_edit.placeholderText() == placeholder
    ]
    assert len(matches) == 1, placeholder
    return matches[0]


def _combo_with_text(dialog: RouteMeasurementDialog, text: str) -> QComboBox:
    matches = [
        combo for combo in dialog.findChildren(QComboBox) if combo.findText(text) >= 0
    ]
    assert len(matches) == 1, text
    return matches[0]


def _checkbox(dialog: RouteMeasurementDialog, text: str) -> QCheckBox:
    matches = [
        checkbox
        for checkbox in dialog.findChildren(QCheckBox)
        if checkbox.text() == text
    ]
    assert len(matches) == 1, text
    return matches[0]


def _dispose_dialog(dialog: RouteMeasurementDialog, app: QApplication) -> None:
    dialog.deleteLater()
    QCoreApplication.sendPostedEvents(None, QEvent.DeferredDelete)
    app.processEvents()
    del dialog
    gc.collect()


def test_pause_click_updates_to_interrupt_before_emitting(
    qt_app: QApplication,
    tmp_path: Path,
) -> None:
    _ = qt_app
    dialog = _dialog(tmp_path)
    dialog.set_running(True)
    pause_button = _button(dialog, "Pause")
    emitted_state: list[tuple[str, bool]] = []
    dialog.pause_requested.connect(
        lambda: emitted_state.append((pause_button.text(), pause_button.isEnabled()))
    )

    pause_button.click()

    assert emitted_state == [("Interrupt", True)]
    assert pause_button.text() == "Interrupt"
    assert pause_button.isEnabled()
    dialog.deleteLater()


def test_pending_pause_click_marks_interrupt_before_emitting_once(
    qt_app: QApplication,
    tmp_path: Path,
) -> None:
    _ = qt_app
    dialog = _dialog(tmp_path)
    dialog.set_running(True)
    pause_button = _button(dialog, "Pause")
    dialog.pause_requested.connect(lambda: None)
    pause_button.click()
    emitted_state: list[tuple[str, bool]] = []
    dialog.interrupt_requested.connect(
        lambda: emitted_state.append((pause_button.text(), pause_button.isEnabled()))
    )

    pause_button.click()

    assert emitted_state == [("Interrupt", False)]
    assert not pause_button.isEnabled()
    dialog.deleteLater()


def test_only_confirmable_wait_ack_changes_pause_to_resume(
    qt_app: QApplication,
    tmp_path: Path,
) -> None:
    _ = qt_app
    dialog = _dialog(tmp_path)
    dialog.set_running(True)
    pause_button = _button(dialog, "Pause")
    calls: list[str] = []
    dialog.pause_requested.connect(lambda: calls.append("pause"))
    dialog.interrupt_requested.connect(lambda: calls.append("interrupt"))
    dialog.next_requested.connect(lambda: calls.append("next"))

    pause_button.click()
    assert pause_button.text() == "Interrupt"

    dialog.set_waiting(True, "paused")
    assert pause_button.text() == "Resume"
    assert pause_button.isEnabled()
    pause_button.click()

    assert calls == ["pause", "next"]
    dialog.deleteLater()


def test_external_measurement_wait_keeps_interrupt_and_disables_confirmation(
    qt_app: QApplication,
    tmp_path: Path,
) -> None:
    _ = qt_app
    dialog = _dialog(tmp_path)
    dialog.set_running(True)
    dialog.set_waiting(True, "external_measurement")

    pause_button = _button(dialog, "Interrupt")
    assert pause_button.isEnabled()
    assert not _button(dialog, "Measure").isEnabled()
    assert not _button(dialog, "Save Shift").isEnabled()
    assert not _button(dialog, "Skip").isEnabled()
    dialog.deleteLater()


def test_confirmable_wait_measure_button_requests_current_contact(
    qt_app: QApplication,
    tmp_path: Path,
) -> None:
    _ = qt_app
    dialog = _dialog(tmp_path)
    calls: list[str] = []
    dialog.measure_current_requested.connect(lambda: calls.append("measure_current"))
    dialog.measure_requested.connect(lambda _configuration: calls.append("start"))
    dialog.set_running(True)
    dialog.set_waiting(True, "contact_confirmation")

    _button(dialog, "Measure").click()

    assert calls == ["measure_current"]
    dialog.deleteLater()


def test_confirmable_wait_reenables_runtime_setup_fields(
    qt_app: QApplication,
    tmp_path: Path,
) -> None:
    _ = qt_app
    dialog = _dialog(tmp_path)
    csv_path = _line_edit(dialog, "measurement_results.csv")
    previous_csv = _line_edit(dialog, "previous measurement CSV")
    photo_dir = _line_edit(dialog, "route photo output directory")
    operation = _combo_with_text(dialog, "Photo then measure")
    meter = _combo_with_text(dialog, "Keithley 2400 + 2182A")
    autofocus = _checkbox(dialog, "Autofocus before each point")
    operation.setCurrentIndex(operation.findText("Photo then measure"))
    _checkbox(dialog, "Only previous OK").setChecked(True)
    autofocus.setChecked(True)
    dialog.set_running(True)
    assert not csv_path.isEnabled()
    assert not meter.isEnabled()

    dialog.set_waiting(True, "contact_confirmation")

    assert csv_path.isEnabled()
    assert previous_csv.isEnabled()
    assert photo_dir.isEnabled()
    assert operation.isEnabled()
    assert meter.isEnabled()
    assert autofocus.isEnabled()
    dialog.deleteLater()


def test_api_control_presenter_shows_resume_only_after_pause_ack(
    qt_app: QApplication,
    tmp_path: Path,
) -> None:
    _ = qt_app
    dialog = _dialog(tmp_path)
    sink = RouteRuntimePresentationSink(
        current_dialog=lambda: dialog,
        current_navigator=lambda: None,
    )

    sink.apply_api_control_update(
        types.SimpleNamespace(
            active=True,
            pause_pending=True,
            waiting=False,
            control_waiting_reason="paused",
        ),
        "API route control pause requested.",
    )
    assert _button(dialog, "Interrupt").isEnabled()

    sink.apply_api_control_update(
        types.SimpleNamespace(
            active=True,
            pause_pending=False,
            waiting=True,
            control_waiting_reason="paused",
        ),
        "API route control paused.",
    )
    assert _button(dialog, "Resume").isEnabled()
    dialog.deleteLater()


def test_set_route_updates_defaults_clamps_point_and_resets_idle_progress(
    qt_app: QApplication,
    tmp_path: Path,
) -> None:
    _ = qt_app
    dialog = _dialog(tmp_path)
    dialog.set_current_point(4, save=False)
    dialog.set_progress(3, 4, 4)

    dialog.set_route(
        route_name="route-b",
        route_point_count=2,
        default_csv_path=str(tmp_path / "second.csv"),
        default_photo_dir=str(tmp_path / "second-photos"),
    )

    configuration = dialog.current_configuration()
    progress = dialog.findChild(QProgressBar)
    assert configuration.current_point == 2
    assert configuration.csv_path == str(tmp_path / "second.csv")
    assert configuration.previous_csv_path == str(tmp_path / "second.csv")
    assert configuration.photo_output_dir == str(tmp_path / "second-photos")
    assert progress is not None
    assert (progress.minimum(), progress.maximum(), progress.value()) == (0, 2, 0)
    dialog.deleteLater()


def test_set_route_publishes_actual_point_clamp_exactly_once(
    qt_app: QApplication,
    tmp_path: Path,
) -> None:
    _ = qt_app
    dialog = _dialog(tmp_path)
    dialog.set_current_point(4, save=False)
    published: list[int] = []
    dialog.current_point_changed.connect(published.append)

    dialog.set_route(
        route_name="route-b",
        route_point_count=2,
        default_csv_path=str(tmp_path / "second.csv"),
        default_photo_dir=str(tmp_path / "second-photos"),
    )

    assert published == [2]
    assert dialog.current_configuration().current_point == 2
    dialog.deleteLater()


def test_programmatic_current_point_synchronization_remains_silent(
    qt_app: QApplication,
    tmp_path: Path,
) -> None:
    _ = qt_app
    dialog = _dialog(tmp_path)
    published: list[int] = []
    dialog.current_point_changed.connect(published.append)

    dialog.set_current_point(3, save=False)

    assert dialog.current_configuration().current_point == 3
    assert published == []
    dialog.deleteLater()


def test_loaded_profile_point_reaches_main_through_real_dialog_adapter(
    qt_app: QApplication,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from main import Main

    resume_points: list[int] = []
    adjustment_points: list[int] = []
    window = Main.__new__(Main)
    window._pending_route_measure_point = None
    runner = types.SimpleNamespace(
        set_current_adjustment_point=lambda point: adjustment_points.append(int(point))
    )
    activate_route_run(window, runner, waiting=True)
    window._set_route_measurement_resume_point = lambda point: resume_points.append(
        int(point)
    )
    profile_path = tmp_path / "profile.json"
    profile_path.write_text(
        json.dumps({"version": 4, "current_point": 3}),
        encoding="utf-8",
    )
    dialog = _create_route_measurement_dialog(
        route_name="route-a",
        route_point_count=4,
        default_csv_path=str(tmp_path / "measurements.csv"),
        default_photo_dir=str(tmp_path / "photos"),
        default_meter_type="keithley_2400_2182a",
        settings_path=tmp_path / "settings.json",
        parent=None,
        handlers=route_dialog_handlers(window),
    )
    monkeypatch.setattr(
        "probe_station_gui.dialogs.route_measurement_profile."
        "QFileDialog.getOpenFileName",
        lambda *_args: (str(profile_path), "JSON files (*.json)"),
    )

    _button(dialog, "Load Profile").click()

    assert resume_points == [3]
    assert adjustment_points == [3]
    assert window._pending_route_measure_point == 3
    _dispose_dialog(dialog, qt_app)


def test_route_point_clamp_reaches_main_through_real_dialog_adapter(
    qt_app: QApplication,
    tmp_path: Path,
) -> None:
    from main import Main

    resume_points: list[int] = []
    adjustment_points: list[int] = []
    window = Main.__new__(Main)
    window._pending_route_measure_point = None
    runner = types.SimpleNamespace(
        set_current_adjustment_point=lambda point: adjustment_points.append(int(point))
    )
    activate_route_run(window, runner, waiting=True)
    window._set_route_measurement_resume_point = lambda point: resume_points.append(
        int(point)
    )
    dialog = _create_route_measurement_dialog(
        route_name="route-a",
        route_point_count=4,
        default_csv_path=str(tmp_path / "measurements.csv"),
        default_photo_dir=str(tmp_path / "photos"),
        default_meter_type="keithley_2400_2182a",
        settings_path=tmp_path / "settings.json",
        parent=None,
        handlers=route_dialog_handlers(window),
    )
    dialog.set_current_point(4, save=False)

    dialog.set_route(
        route_name="route-b",
        route_point_count=2,
        default_csv_path=str(tmp_path / "second.csv"),
        default_photo_dir=str(tmp_path / "second-photos"),
    )

    assert resume_points == [2]
    assert adjustment_points == [2]
    assert window._pending_route_measure_point == 2
    _dispose_dialog(dialog, qt_app)


def test_set_route_preserves_custom_paths_and_running_progress(
    qt_app: QApplication,
    tmp_path: Path,
) -> None:
    _ = qt_app
    dialog = _dialog(tmp_path)
    _line_edit(dialog, "measurement_results.csv").setText(str(tmp_path / "custom.csv"))
    _line_edit(dialog, "previous measurement CSV").setText(
        str(tmp_path / "custom-previous.csv")
    )
    _line_edit(dialog, "route photo output directory").setText(
        str(tmp_path / "custom-photos")
    )
    dialog.set_progress(3, 4, 3)
    progress = dialog.findChild(QProgressBar)
    assert progress is not None
    before = (progress.minimum(), progress.maximum(), progress.value())
    dialog.set_running(True)

    dialog.set_route(
        route_name="route-b",
        route_point_count=6,
        default_csv_path=str(tmp_path / "second.csv"),
        default_photo_dir=str(tmp_path / "second-photos"),
    )

    configuration = dialog.current_configuration()
    assert configuration.csv_path == str(tmp_path / "custom.csv")
    assert configuration.previous_csv_path == str(tmp_path / "custom-previous.csv")
    assert configuration.photo_output_dir == str(tmp_path / "custom-photos")
    assert (progress.minimum(), progress.maximum(), progress.value()) == before
    dialog.deleteLater()


def test_progress_eta_uses_current_run_baseline(
    qt_app: QApplication,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _ = qt_app
    dialog = _dialog(tmp_path, route_point_count=292)
    clock = iter((100.0, 160.0))
    monkeypatch.setattr(time, "monotonic", lambda: next(clock))

    dialog.set_progress(188, 292, 188)
    dialog.set_progress(189, 292, 189)

    eta_labels = [
        label.text()
        for label in dialog.findChildren(QLabel)
        if label.text().startswith("Remaining:")
    ]
    assert len(eta_labels) == 1
    assert "Remaining: 1:44:00 | Finish:" in eta_labels[0]
    assert "Remaining: 00:33" not in eta_labels[0]
    dialog.deleteLater()


def test_running_close_is_rejected_and_idle_close_is_accepted(
    qt_app: QApplication,
    tmp_path: Path,
) -> None:
    _ = qt_app
    dialog = _dialog(tmp_path)
    dialog.set_running(True)
    running_event = QCloseEvent()

    dialog.closeEvent(running_event)

    assert not running_event.isAccepted()
    assert any(
        label.text() == "Stop route measurement before closing."
        for label in dialog.findChildren(QLabel)
    )

    dialog.set_running(False)
    idle_event = QCloseEvent()
    dialog.closeEvent(idle_event)

    assert idle_event.isAccepted()
    assert (tmp_path / "route-measurement-settings.json").is_file()
    dialog.deleteLater()


def test_current_configuration_persists_settings_as_existing_side_effect(
    qt_app: QApplication,
    tmp_path: Path,
) -> None:
    _ = qt_app
    dialog = _dialog(tmp_path)
    settings_path = tmp_path / "route-measurement-settings.json"
    assert not settings_path.exists()

    configuration = dialog.current_configuration()

    assert settings_path.is_file()
    stored = json.loads(settings_path.read_text(encoding="utf-8"))
    assert stored["csv_path"] == configuration.csv_path
    assert stored["current_point"] == configuration.current_point
    dialog.deleteLater()


def test_result_slot_renders_typed_record_and_enables_raw_data(
    qt_app: QApplication,
    tmp_path: Path,
) -> None:
    _ = qt_app
    dialog = _dialog(tmp_path)
    record = RouteMeasurementRecord(
        timestamp="2026-08-12T12:00:00",
        structure_number=1,
        nplc="1",
        measurement_type="test",
        n_measurements=1,
        resistance_ohm=123.0,
        resistance_rms_ohm=2.0,
        relative_rms=0.01,
        status="ok",
        contact_quality=RouteContactQuality(
            assessed=True,
            good=True,
            status="good",
        ),
        raw_samples=(
            RouteMeasurementSample(
                sample_index=1,
                differential_resistance_ohm=123.0,
            ),
        ),
    )

    dialog.set_result(record, 1, 4, True)

    assert any(
        label.text().startswith("Saved point 1/4: R=123 Ohm")
        and "contact=good" in label.text()
        for label in dialog.findChildren(QLabel)
    )
    assert _button(dialog, "Raw Data").isEnabled()
    dialog.deleteLater()


def test_dialog_adapter_connects_every_published_signal_exactly_once(
    qt_app: QApplication,
    tmp_path: Path,
) -> None:
    _ = qt_app
    calls: list[str] = []

    def record(name: str):
        return lambda *_args: calls.append(name)

    handlers = RouteDialogHandlers(
        measure_requested=record("measure"),
        start_session_requested=record("start_session"),
        cancel_session_requested=record("cancel_session"),
        next_requested=record("next"),
        remeasure_requested=record("remeasure"),
        measure_current_requested=record("measure_current"),
        skip_requested=record("skip"),
        save_shift_requested=record("save_shift"),
        interrupt_requested=record("interrupt"),
        pause_requested=record("pause"),
        stop_requested=record("stop"),
        jump_requested=record("jump"),
        move_requested=record("move"),
        current_point_changed=record("current_point"),
        finished=record("finished"),
    )
    dialog = _create_route_measurement_dialog(
        route_name="route-a",
        route_point_count=4,
        default_csv_path=str(tmp_path / "default.csv"),
        default_photo_dir=str(tmp_path / "photos"),
        default_meter_type="keithley_6221_2182a",
        settings_path=tmp_path / "settings.json",
        parent=None,
        handlers=handlers,
    )

    dialog.measure_requested.emit(object())
    dialog.start_session_requested.emit()
    dialog.cancel_session_requested.emit()
    dialog.next_requested.emit()
    dialog.remeasure_requested.emit()
    dialog.measure_current_requested.emit()
    dialog.skip_requested.emit()
    dialog.save_shift_requested.emit()
    dialog.interrupt_requested.emit()
    dialog.pause_requested.emit()
    dialog.stop_requested.emit()
    dialog.jump_requested.emit(2)
    dialog.move_requested.emit(2)
    dialog.current_point_changed.emit(2)
    dialog.finished.emit(0)

    assert Counter(calls) == Counter(
        {
            "measure": 1,
            "start_session": 1,
            "cancel_session": 1,
            "next": 1,
            "remeasure": 1,
            "measure_current": 1,
            "skip": 1,
            "save_shift": 1,
            "interrupt": 1,
            "pause": 1,
            "stop": 1,
            "jump": 1,
            "move": 1,
            "current_point": 1,
            "finished": 1,
        }
    )
    dialog.deleteLater()
