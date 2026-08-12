"""Dialog composing route setup, runtime presentation, and run controls."""

from __future__ import annotations

from pathlib import Path

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import (
    QApplication,
    QDialog,
    QScrollArea,
    QVBoxLayout,
    QWidget,
)

from probe_station_gui.dialogs.route_measurement_profile import (
    RouteMeasurementProfileController,
)
from probe_station_gui.dialogs.route_measurement_runtime import (
    RouteMeasurementRuntimeView,
)
from probe_station_gui.dialogs.route_measurement_run_controls import (
    RouteMeasurementRunControls,
)
from probe_station_gui.dialogs.route_measurement_setup import (
    RouteMeasurementSetupEditor,
)
from probe_station_gui.route.measurement_config import RouteMeasurementRunConfiguration
from probe_station_gui.route.meter_config import ROUTE_METER_KEITHLEY


class RouteMeasurementDialog(QDialog):
    """Non-modal route measurement setup and control window."""

    measure_requested = Signal(object)
    start_session_requested = Signal()
    cancel_session_requested = Signal()
    next_requested = Signal()
    remeasure_requested = Signal()
    measure_current_requested = Signal()
    skip_requested = Signal()
    save_shift_requested = Signal()
    interrupt_requested = Signal()
    pause_requested = Signal()
    stop_requested = Signal()
    jump_requested = Signal(int)
    move_requested = Signal(int)
    current_point_changed = Signal(int)

    def __init__(
        self,
        *,
        route_name: str,
        route_point_count: int,
        default_csv_path: str,
        default_photo_dir: str | None = None,
        default_meter_type: str = ROUTE_METER_KEITHLEY,
        settings_path: str | Path | None = None,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self.setWindowTitle("Route Measurement")
        self.setWindowFlags(
            Qt.Window
            | Qt.WindowMinimizeButtonHint
            | Qt.WindowMaximizeButtonHint
            | Qt.WindowCloseButtonHint
        )
        self.setModal(False)
        resolved_photo_dir = (
            default_photo_dir
            if default_photo_dir is not None
            else str(Path(default_csv_path).with_suffix("")) + "-photos"
        )
        self._settings_path = (
            Path(settings_path).expanduser() if settings_path else None
        )
        outer_layout = QVBoxLayout(self)
        scroll_area = QScrollArea(self)
        scroll_area.setWidgetResizable(True)
        scroll_area.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        scroll_content = QWidget(scroll_area)
        content_layout = QVBoxLayout(scroll_content)
        scroll_area.setWidget(scroll_content)
        outer_layout.addWidget(scroll_area, 1)

        self._setup_editor = RouteMeasurementSetupEditor(
            route_name=route_name,
            route_point_count=route_point_count,
            default_csv_path=default_csv_path,
            default_photo_dir=resolved_photo_dir,
            default_meter_type=default_meter_type,
            parent=scroll_content,
        )
        content_layout.addWidget(self._setup_editor)
        self._runtime_view = RouteMeasurementRuntimeView(
            route_point_count=route_point_count,
            parent=scroll_content,
        )
        content_layout.addWidget(self._runtime_view)
        self._run_controls = RouteMeasurementRunControls(
            route_point_count=route_point_count,
            parent=self,
        )
        outer_layout.addWidget(self._run_controls)
        self._profile_controller = RouteMeasurementProfileController(
            setup_editor=self._setup_editor,
            run_controls=self._run_controls,
            settings_path=self._settings_path,
            dialog_parent=self,
        )

        self._setup_editor.measure_requested.connect(self.measure_requested.emit)
        self._setup_editor.status_changed.connect(self.set_status)
        self._setup_editor.settings_changed.connect(
            self._profile_controller.save_settings
        )
        self._setup_editor.load_profile_requested.connect(
            self._profile_controller.load_profile
        )
        self._setup_editor.save_profile_requested.connect(
            self._profile_controller.save_profile
        )
        self._profile_controller.status_changed.connect(self.set_status)
        self._run_controls.measure_requested.connect(self._request_measure)
        self._forward_run_control_signals()
        self._run_controls.current_point_changed.connect(self._on_current_point_changed)
        self._run_controls.close_requested.connect(self.close)
        self._profile_controller.load_settings()
        self.set_running(False)
        self._resize_to_available_screen()

    def _forward_run_control_signals(self) -> None:
        pairs = (
            (self._run_controls.start_session_requested, self.start_session_requested),
            (
                self._run_controls.cancel_session_requested,
                self.cancel_session_requested,
            ),
            (self._run_controls.next_requested, self.next_requested),
            (self._run_controls.remeasure_requested, self.remeasure_requested),
            (
                self._run_controls.measure_current_requested,
                self.measure_current_requested,
            ),
            (self._run_controls.skip_requested, self.skip_requested),
            (self._run_controls.save_shift_requested, self.save_shift_requested),
            (self._run_controls.interrupt_requested, self.interrupt_requested),
            (self._run_controls.pause_requested, self.pause_requested),
            (self._run_controls.stop_requested, self.stop_requested),
            (self._run_controls.jump_requested, self.jump_requested),
            (self._run_controls.move_requested, self.move_requested),
        )
        for source, target in pairs:
            source.connect(target.emit)

    def closeEvent(self, event) -> None:  # type: ignore[override]
        if self._run_controls.running():
            self.set_status("Stop route measurement before closing.")
            event.ignore()
            return
        self._profile_controller.save_settings()
        super().closeEvent(event)

    def _resize_to_available_screen(self) -> None:
        screen = self.screen() or QApplication.primaryScreen()
        if screen is None:
            self.resize(820, 720)
            return
        available = screen.availableGeometry()
        self.resize(
            max(640, min(820, available.width() - 80)),
            max(420, min(760, available.height() - 120)),
        )

    def set_status(self, message: str) -> None:
        self._runtime_view.set_status(message)

    def reset_progress(self, total: int | None = None) -> None:
        self._runtime_view.reset_progress(total)

    def set_progress(self, position: int, total: int, point_number: int) -> None:
        self._runtime_view.set_progress(position, total, point_number)

    def finish_progress(self, success: bool) -> None:
        self._runtime_view.finish_progress(success)

    def set_result(
        self,
        record: object,
        position: int,
        total: int,
        saved: bool,
    ) -> None:
        self._runtime_view.set_result(record, position, total, saved)

    def set_current_point(self, point_number: int, *, save: bool = True) -> None:
        self._run_controls.set_current_point(point_number)
        if save:
            self._profile_controller.save_settings()

    def set_measurement_session_active(
        self,
        active: bool,
        *,
        save: bool = True,
    ) -> None:
        self._run_controls.set_measurement_session_active(active)
        if save:
            self._profile_controller.save_settings()

    def set_route(
        self,
        *,
        route_name: str,
        route_point_count: int,
        default_csv_path: str,
        default_photo_dir: str | None = None,
    ) -> None:
        point_count = max(1, int(route_point_count))
        self._run_controls.set_route_point_count(point_count)
        if not self._run_controls.running():
            self.reset_progress(point_count)
        self._setup_editor.set_route(
            route_name=route_name,
            route_point_count=route_point_count,
            default_csv_path=default_csv_path,
            default_photo_dir=default_photo_dir,
        )

    def set_running(self, running: bool) -> None:
        self._run_controls.set_running(running)
        self._sync_setup_state()

    def set_waiting(self, waiting: bool, reason: str = "") -> None:
        self._run_controls.set_waiting(waiting, reason)
        self._sync_setup_state()

    def set_pause_request_pending(self, pending: bool) -> None:
        self._run_controls.set_pause_request_pending(pending)

    def set_interrupt_request_pending(self, pending: bool) -> None:
        self._run_controls.set_interrupt_request_pending(pending)

    def current_configuration(self) -> RouteMeasurementRunConfiguration:
        """Return and persist the current route measurement configuration."""

        self._profile_controller.save_settings()
        return self._setup_editor.configuration(self._run_controls.current_point())

    def _request_measure(self) -> None:
        self._setup_editor.request_measure(self._run_controls.current_point())

    def _sync_setup_state(self) -> None:
        self._setup_editor.set_run_state(
            running=self._run_controls.running(),
            waiting=self._run_controls.can_confirm_waiting(),
        )

    def _on_current_point_changed(self, value: int) -> None:
        point_number = int(value)
        self._profile_controller.save_settings()
        self.current_point_changed.emit(point_number)


__all__ = ["RouteMeasurementDialog"]
