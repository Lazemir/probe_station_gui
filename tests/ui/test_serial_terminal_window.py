from __future__ import annotations

import os

import pytest


os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
pytest.importorskip("PySide6")

from PySide6.QtWidgets import QApplication

from probe_station_gui.views.serial_terminal_window import SerialTerminalWindow


class _FakeSerialConnection:
    def __init__(self) -> None:
        self.is_open = True
        self.port = "COM7"
        self.baudrate = 115200
        self.writes: list[bytes] = []
        self.flush_count = 0

    def write(self, payload: bytes) -> None:
        self.writes.append(payload)

    def flush(self) -> None:
        self.flush_count += 1


class _FakeStageController:
    def __init__(self, *, busy: bool = False, handled: bool | None = True) -> None:
        self.busy = busy
        self.handled = handled
        self.calls: list[tuple[object, str]] = []

    def is_busy(self) -> bool:
        return self.busy

    def queue_outbound_command(
        self,
        command: str | bytes,
        *,
        source: str = "unknown",
    ) -> bool | None:
        self.calls.append((command, source))
        return self.handled

    def read_pending_serial_output(self, max_bytes: int | None = None) -> bytes:
        return b""


@pytest.fixture(scope="module")
def qt_app() -> QApplication:
    app = QApplication.instance()
    if app is None:
        app = QApplication([])
    return app


def test_send_control_x_delegates_to_stage_controller(qt_app: QApplication) -> None:
    widget = SerialTerminalWindow()
    serial_connection = _FakeSerialConnection()
    controller = _FakeStageController()
    emitted: list[str] = []
    widget.manual_command_sent.connect(emitted.append)
    widget.set_serial(serial_connection)
    widget.set_stage_controller(controller)

    widget.send_control_x()

    assert controller.calls == [(b"\x18", "serial_terminal_ctrl_x")]
    assert emitted == ["CTRL-X"]
    assert widget._capture_manual_output is True
    assert "\u2418" in widget.output_edit.toPlainText()
    assert serial_connection.writes == []
    widget.deleteLater()


def test_send_current_line_delegates_to_stage_controller_and_updates_history(
    qt_app: QApplication,
) -> None:
    widget = SerialTerminalWindow()
    serial_connection = _FakeSerialConnection()
    controller = _FakeStageController()
    emitted: list[str] = []
    widget.manual_command_sent.connect(emitted.append)
    widget.set_serial(serial_connection)
    widget.set_stage_controller(controller)
    widget.input_edit.setText("M114")

    widget.send_current_line()

    assert controller.calls == [("M114", "unknown")]
    assert emitted == ["M114"]
    assert widget._command_history == ["M114"]
    assert widget._history_position == 1
    assert widget.input_edit.text() == ""
    assert widget._capture_manual_output is True
    assert "M114" in widget.output_edit.toPlainText()
    assert serial_connection.writes == []
    widget.deleteLater()


def test_send_current_line_preserves_busy_message(qt_app: QApplication) -> None:
    widget = SerialTerminalWindow()
    serial_connection = _FakeSerialConnection()
    controller = _FakeStageController(busy=True)
    widget.set_serial(serial_connection)
    widget.set_stage_controller(controller)
    widget.input_edit.setText("M114")

    widget.send_current_line()

    assert controller.calls == []
    assert widget.output_edit.toPlainText().endswith(
        "[ Cannot send while automated move is running. ]"
    )
    widget.deleteLater()
