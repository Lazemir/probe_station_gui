"""Application entry point for the probe station GUI."""

from __future__ import annotations

import sys

from PySide6.QtCore import QEvent, QThread, QTimer, Qt
from PySide6.QtGui import QAction
from PySide6.QtWidgets import (
    QApplication,
    QHBoxLayout,
    QMainWindow,
    QToolButton,
    QVBoxLayout,
    QWidget,
)

from probe_station_gui import (
    Grabber,
    JoystickWindow,
    MicroscopeView,
    StageController,
    SerialTerminalWindow,
)
from probe_station_gui.views.dock_widgets import CollapsibleDockWidget
from probe_station_gui.views.serial_connection_panel import SerialConnectionPanel


class Main(QMainWindow):
    """Main application window wiring the camera view and serial dialog."""

    def __init__(self) -> None:
        super().__init__()
        self.setWindowTitle("Microscope control")
        self.menuBar().setNativeMenuBar(False)

        self.view = MicroscopeView()
        self._minimize_button: QToolButton | None = None
        self._full_screen_button: QToolButton | None = None
        self._close_button: QToolButton | None = None
        central_container = QWidget(self)
        central_layout = QVBoxLayout(central_container)
        central_layout.setContentsMargins(0, 0, 0, 0)
        central_layout.setSpacing(0)
        self._window_controls_bar = self._create_window_controls_bar()
        central_layout.addWidget(self._window_controls_bar)
        central_layout.addWidget(self.view, 1)
        self.setCentralWidget(central_container)
        self.serial_connection = None
        self.serial_port_name: str | None = None
        self.serial_baud_rate: int | None = None
        self.joystick_panel: JoystickWindow | None = None
        self.serial_terminal_panel: SerialTerminalWindow | None = None
        self.serial_connection_panel: SerialConnectionPanel | None = None
        self.joystick_dock: CollapsibleDockWidget | None = None
        self.serial_terminal_dock: CollapsibleDockWidget | None = None
        self.serial_connection_dock: CollapsibleDockWidget | None = None
        self._minimize_action: QAction | None = None
        self._restore_action: QAction | None = None
        self._full_screen_action: QAction | None = None
        self._close_action: QAction | None = None
        self.statusBar()

        self.grabber = Grabber()
        self.thread = QThread()
        self.grabber.moveToThread(self.thread)
        self.thread.started.connect(self.grabber.start)
        self.view.clicked.connect(self.on_click)
        self.grabber.frame_ready.connect(self.view.set_frame)
        self.grabber.error.connect(self.on_error)
        self.thread.start()

        self.stage_controller = StageController()
        self.stage_controller.status_message.connect(self.statusBar().showMessage)
        self.stage_controller.movement_finished.connect(self.on_move_finished)
        self.stage_controller.calibration_changed.connect(self.on_calibration_changed)
        self.stage_controller.movement_started.connect(
            lambda: self.statusBar().showMessage("Moving stage…")
        )
        self.grabber.frame_ready.connect(self.stage_controller.on_frame_ready)

        self._create_dock_widgets()

        self._setup_menus()
        self._connect_window_control_buttons()

        window_menu = self.menuBar().addMenu("Panels")
        if self.joystick_dock is not None:
            joystick_action = self.joystick_dock.toggleViewAction()
            joystick_action.setText("Joystick")
            window_menu.addAction(joystick_action)
        if self.serial_terminal_dock is not None:
            terminal_action = self.serial_terminal_dock.toggleViewAction()
            terminal_action.setText("Serial Terminal")
            window_menu.addAction(terminal_action)
        if self.serial_connection_dock is not None:
            connection_action = self.serial_connection_dock.toggleViewAction()
            connection_action.setText("Connection")
            window_menu.addAction(connection_action)

        QTimer.singleShot(0, self._auto_connect_if_possible)

        self.setStyleSheet(
            """
            QMainWindow::separator { width: 8px; height: 8px; background: palette(window); }
            #WindowControlsBar {
                background-color: #2b2b2b;
            }
            #WindowControlsBar QToolButton {
                color: #f0f0f0;
                font-size: 16px;
                padding: 4px 12px;
                border: none;
            }
            #WindowControlsBar QToolButton:hover {
                background-color: rgba(255, 255, 255, 0.12);
            }
            #WindowControlsBar QToolButton:pressed {
                background-color: rgba(255, 255, 255, 0.24);
            }
            """
        )

    def on_click(self, dx: float, dy: float, _rel_x: float, _rel_y: float) -> None:
        self.stage_controller.request_move(dx, dy)

    def on_error(self, message: str) -> None:
        print("Camera error:", message)

    def on_serial_connected(self, serial_port) -> None:
        if self.serial_connection and self.serial_connection.is_open:
            self.serial_connection.close()
        self.serial_connection = serial_port
        self.serial_port_name = serial_port.port
        try:
            baud_rate = int(serial_port.baudrate)
        except TypeError:
            baud_rate = int(float(serial_port.baudrate))
        self.serial_baud_rate = baud_rate
        print(
            f"Serial connected: {self.serial_connection.port} @ {self.serial_connection.baudrate} baud"
        )
        self.stage_controller.set_serial(self.serial_connection)
        if self.joystick_panel and self.joystick_dock:
            self.joystick_panel.set_serial(self.serial_connection)
            self.joystick_dock.setVisible(True)
            self.joystick_dock.raise_()
            if self.joystick_dock.isFloating():
                self.joystick_dock.activateWindow()
        if self.serial_terminal_panel and self.serial_terminal_dock:
            self.serial_terminal_panel.set_serial(self.serial_connection)
            self.serial_terminal_dock.setVisible(True)
            self.serial_terminal_dock.raise_()
            if self.serial_terminal_dock.isFloating():
                self.serial_terminal_dock.activateWindow()

    def on_serial_disconnected(self) -> None:
        if self.serial_connection and self.serial_connection.is_open:
            self.serial_connection.close()
        self.serial_connection = None
        print("Serial disconnected")
        self.stage_controller.set_serial(None)
        auto_retry = self.sender() is not self.serial_connection_panel
        if self.serial_connection_panel:
            self.serial_connection_panel.handle_external_disconnect(auto_retry=auto_retry)
        if self.joystick_panel:
            self.joystick_panel.set_serial(None)
        if self.serial_terminal_panel:
            self.serial_terminal_panel.set_serial(None)

    def _auto_connect_if_possible(self) -> None:
        if self.serial_connection_panel and not self.serial_connection:
            self.serial_connection_panel.auto_connect()

    def _setup_menus(self) -> None:
        app_menu = self.menuBar().addMenu("Application")

        self._minimize_action = QAction("Minimize", self)
        self._minimize_action.triggered.connect(self.showMinimized)
        app_menu.addAction(self._minimize_action)

        self._restore_action = QAction("Restore", self)
        self._restore_action.triggered.connect(self.showNormal)
        app_menu.addAction(self._restore_action)

        self._full_screen_action = QAction("Toggle Full Screen", self)
        self._full_screen_action.setCheckable(True)
        self._full_screen_action.setChecked(True)
        self._full_screen_action.triggered.connect(self._toggle_full_screen)
        app_menu.addAction(self._full_screen_action)

        if self._minimize_action is not None:
            self._minimize_action.triggered.connect(
                lambda: self._set_full_screen_checked(False)
            )
        if self._restore_action is not None:
            self._restore_action.triggered.connect(
                lambda: self._set_full_screen_checked(False)
            )

        self._close_action = QAction("Close", self)
        self._close_action.triggered.connect(self.close)
        app_menu.addAction(self._close_action)

        self._update_full_screen_action_state()

    def _connect_window_control_buttons(self) -> None:
        if self._minimize_button is not None:
            self._minimize_button.clicked.connect(self._on_minimize_requested)
        if self._close_button is not None:
            self._close_button.clicked.connect(self.close)
        if self._full_screen_button is not None:
            self._full_screen_button.clicked.connect(
                self._toggle_full_screen_from_button
            )

    def show_joystick_window(self) -> None:
        if not self.joystick_panel or not self.joystick_dock:
            return
        self.joystick_dock.setVisible(True)
        self.joystick_dock.raise_()
        if self.joystick_dock.isFloating():
            self.joystick_dock.activateWindow()
        else:
            self.joystick_panel.setFocus(Qt.ActiveWindowFocusReason)

    def show_serial_terminal_window(self) -> None:
        if not self.serial_terminal_panel or not self.serial_terminal_dock:
            return
        self.serial_terminal_dock.setVisible(True)
        self.serial_terminal_dock.raise_()
        if self.serial_terminal_dock.isFloating():
            self.serial_terminal_dock.activateWindow()
        else:
            self.serial_terminal_panel.setFocus(Qt.ActiveWindowFocusReason)

    def on_move_finished(self, success: bool, message: str) -> None:
        if success:
            self.view.clear_target_cross()
        if message:
            self.statusBar().showMessage(message, 5000)

    def on_calibration_changed(self, mm_per_pixel_x: float, mm_per_pixel_y: float) -> None:
        self.statusBar().showMessage(
            f"Calibration: ΔX {mm_per_pixel_x:.6f} mm/px, ΔY {mm_per_pixel_y:.6f} mm/px",
            5000,
        )

    def closeEvent(self, event) -> None:  # type: ignore[override]
        self.grabber.stop()
        self.thread.quit()
        self.thread.wait()
        if self.serial_connection and self.serial_connection.is_open:
            self.serial_connection.close()
        if self.joystick_panel:
            self.joystick_panel.set_serial(None)
        if self.serial_terminal_panel:
            self.serial_terminal_panel.set_serial(None)
        self.stage_controller.shutdown()
        if self.serial_connection_panel:
            self.serial_connection_panel.shutdown()
        event.accept()

    def changeEvent(self, event) -> None:  # type: ignore[override]
        super().changeEvent(event)
        if event.type() == QEvent.WindowStateChange:
            QTimer.singleShot(0, self._update_full_screen_action_state)

    def _create_dock_widgets(self) -> None:
        self.serial_connection_panel = SerialConnectionPanel(self)
        self.serial_connection_panel.connected.connect(self.on_serial_connected)
        self.serial_connection_panel.disconnected.connect(self.on_serial_disconnected)
        self.serial_connection_dock = CollapsibleDockWidget("Connection", self)
        self.serial_connection_dock.setObjectName("SerialConnectionDock")
        self.serial_connection_dock.setWidget(self.serial_connection_panel)
        self.serial_connection_dock.setAllowedAreas(
            Qt.LeftDockWidgetArea | Qt.RightDockWidgetArea
        )
        self.addDockWidget(Qt.LeftDockWidgetArea, self.serial_connection_dock)

        self.joystick_panel = JoystickWindow(self)
        self.joystick_panel.set_serial(self.serial_connection)
        self.joystick_dock = CollapsibleDockWidget("Joystick", self)
        self.joystick_dock.setObjectName("JoystickDock")
        self.joystick_dock.setWidget(self.joystick_panel)
        self.joystick_dock.setAllowedAreas(
            Qt.LeftDockWidgetArea | Qt.RightDockWidgetArea
        )
        self.addDockWidget(Qt.LeftDockWidgetArea, self.joystick_dock)
        self.splitDockWidget(
            self.serial_connection_dock, self.joystick_dock, Qt.Vertical
        )

        self.serial_terminal_panel = SerialTerminalWindow(self)
        self.serial_terminal_panel.set_stage_controller(self.stage_controller)
        self.serial_terminal_panel.set_serial(self.serial_connection)
        self.serial_terminal_dock = CollapsibleDockWidget("Serial Terminal", self)
        self.serial_terminal_dock.setObjectName("SerialTerminalDock")
        self.serial_terminal_dock.setWidget(self.serial_terminal_panel)
        self.serial_terminal_dock.setAllowedAreas(
            Qt.LeftDockWidgetArea | Qt.RightDockWidgetArea
        )
        self.addDockWidget(Qt.LeftDockWidgetArea, self.serial_terminal_dock)
        self.splitDockWidget(self.joystick_dock, self.serial_terminal_dock, Qt.Vertical)

    def _set_full_screen_checked(self, checked: bool) -> None:
        if self._full_screen_action:
            block = self._full_screen_action.blockSignals(True)
            self._full_screen_action.setChecked(checked)
            self._full_screen_action.blockSignals(block)
        if self._full_screen_button:
            block = self._full_screen_button.blockSignals(True)
            self._full_screen_button.setChecked(checked)
            self._full_screen_button.blockSignals(block)
        self._update_full_screen_button_icon()

    def _update_full_screen_action_state(self) -> None:
        self._set_full_screen_checked(self.isFullScreen())

    def _toggle_full_screen(self, checked: bool) -> None:
        if checked:
            self.showFullScreen()
        else:
            self.showNormal()
        self._update_full_screen_action_state()

    def _toggle_full_screen_from_button(self, checked: bool) -> None:
        if self._full_screen_action:
            self._set_full_screen_checked(checked)
        self._toggle_full_screen(checked)

    def _on_minimize_requested(self) -> None:
        self._set_full_screen_checked(False)
        self.showMinimized()

    def _update_full_screen_button_icon(self) -> None:
        if not self._full_screen_button:
            return
        if self.isFullScreen():
            self._full_screen_button.setText("❐")
            self._full_screen_button.setToolTip("Restore")
        else:
            self._full_screen_button.setText("□")
            self._full_screen_button.setToolTip("Full Screen")

    def _create_window_controls_bar(self) -> QWidget:
        bar = QWidget(self)
        bar.setObjectName("WindowControlsBar")
        layout = QHBoxLayout(bar)
        layout.setContentsMargins(12, 6, 12, 6)
        layout.setSpacing(6)

        layout.addStretch()

        self._minimize_button = QToolButton(bar)
        self._minimize_button.setText("–")
        self._minimize_button.setToolTip("Minimize")
        self._minimize_button.setAutoRaise(True)
        layout.addWidget(self._minimize_button)

        self._full_screen_button = QToolButton(bar)
        self._full_screen_button.setCheckable(True)
        self._full_screen_button.setChecked(True)
        self._full_screen_button.setToolTip("Full Screen")
        self._full_screen_button.setAutoRaise(True)
        layout.addWidget(self._full_screen_button)

        self._close_button = QToolButton(bar)
        self._close_button.setText("✕")
        self._close_button.setToolTip("Close")
        self._close_button.setAutoRaise(True)
        layout.addWidget(self._close_button)

        return bar


def main() -> int:
    app = QApplication(sys.argv)
    window = Main()
    window.showFullScreen()
    return app.exec()


if __name__ == "__main__":
    sys.exit(main())
