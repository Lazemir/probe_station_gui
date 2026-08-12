from __future__ import annotations

import queue
import logging
import time

from PySide6.QtGui import QColor, QImage

from probe_station_gui.camera.worker import Grabber
from tests.camera.camera_settings_test_support import (
    FakeCamera,
    FakeNode,
    FakeNodeMap,
)


def make_grabber(nodes: list[FakeNode]) -> Grabber:
    grabber = Grabber()
    grabber._camera = FakeCamera(FakeNodeMap(nodes))
    grabber._lifecycle_state = type(grabber._lifecycle_state).ACCEPTING
    grabber._running = True
    return grabber


def close_grabber(grabber: Grabber) -> None:
    grabber._camera_settings_executor.shutdown(wait=True, cancel_futures=True)


def test_latest_frame_cache_does_not_depend_on_gui_signal_delivery() -> None:
    grabber = Grabber()
    frame = QImage(3, 2, QImage.Format_RGB888)
    frame.fill(QColor("red"))
    grabber._frame_index = 7
    try:
        grabber._cache_latest_frame(frame)

        cached, counter = grabber.wait_for_frame(after_counter=None, timeout_s=0.0)
        stale, stale_counter = grabber.wait_for_frame(
            after_counter=counter,
            timeout_s=0.0,
        )

        assert grabber.latest_frame_counter() == 7
        assert counter == 7
        assert cached is not None
        assert cached.pixelColor(0, 0) == QColor("red")
        assert stale is None
        assert stale_counter == 7
    finally:
        close_grabber(grabber)


def test_stream_buffer_uses_newest_frame_instead_of_camera_backlog() -> None:
    handling_mode = FakeNode(
        "StreamBufferHandlingMode",
        "enum",
        "OldestFirst",
        entries=("OldestFirst", "NewestOnly"),
    )
    camera = FakeCamera(
        FakeNodeMap([]),
        FakeNodeMap([handling_mode]),
    )
    grabber = Grabber()
    try:
        grabber._set_stream_buffer_handling_mode(camera)

        assert handling_mode.value == "NewestOnly"
        assert handling_mode.set_values == ["NewestOnly"]
    finally:
        close_grabber(grabber)


def test_public_temporary_camera_settings_request_queues_ordered_command() -> None:
    grabber = make_grabber(
        [
            FakeNode("TriggerSource", "enum", "Software"),
            FakeNode("TriggerMode", "enum", "Off"),
        ]
    )
    try:
        grabber.request_temporary_camera_settings(
            [
                ("TriggerSource", "Line0"),
                ("TriggerMode", "On"),
            ],
            restore_key="laser",
        )
        command = grabber._camera_commands.get_nowait()
        assert command.action == "temporary_set"
        assert command.payload["restore_key"] == "laser"
        assert command.payload["settings"] == [
            {"node_name": "TriggerSource", "value": "Line0"},
            {"node_name": "TriggerMode", "value": "On"},
        ]

        grabber.request_restore_camera_settings(restore_key="laser")
        command = grabber._camera_commands.get_nowait()
        assert command.action == "temporary_restore"
        assert command.payload["restore_key"] == "laser"
        try:
            grabber._camera_commands.get_nowait()
        except queue.Empty:
            pass
        else:
            raise AssertionError("unexpected camera command")
    finally:
        close_grabber(grabber)


def test_snapshot_request_preserves_request_id() -> None:
    grabber = make_grabber([FakeNode("Gain", "float", 0.0)])
    try:
        grabber.request_camera_settings_snapshot(
            "camera",
            ["Gain"],
            request_id="snapshot-1",
        )

        command = grabber._camera_commands.get_nowait()
        assert command.action == "snapshot"
        assert command.payload["request_id"] == "snapshot-1"
    finally:
        close_grabber(grabber)


def test_public_batch_request_queues_ordered_settings() -> None:
    grabber = make_grabber(
        [
            FakeNode(
                "ExposureAuto", "enum", "Continuous", entries=("Off", "Continuous")
            ),
            FakeNode("ExposureTime", "float", 3076.14),
        ]
    )
    try:
        grabber.request_camera_settings_batch(
            [("ExposureAuto", "Off"), ("ExposureTime", 1800.0)],
            request_id="batch-1",
        )

        command = grabber._camera_commands.get_nowait()
        assert command.action == "batch_set"
        assert command.payload["request_id"] == "batch-1"
        assert command.payload["settings"] == [
            {"node_name": "ExposureAuto", "value": "Off"},
            {"node_name": "ExposureTime", "value": 1800.0},
        ]
    finally:
        close_grabber(grabber)


class FakeSpinError(RuntimeError):
    def __init__(self, message: str, code: int) -> None:
        super().__init__(message)
        self.spin_error_code = code


class FakeRgbImage:
    def get_pix_fmt(self) -> str:
        return "RGB8"

    def get_height(self) -> int:
        return 1

    def get_width(self) -> int:
        return 1

    def get_stride(self) -> int:
        return 3

    def get_image_data(self) -> bytes:
        return bytes((10, 20, 30))


def _new_buffer_timeout() -> FakeSpinError:
    return FakeSpinError(
        "Spinnaker: Failed waiting for EventData on NEW_BUFFER_DATA event. [-1011]",
        -1011,
    )


def test_isolated_new_buffer_timeout_emits_no_camera_error() -> None:
    grabber = Grabber()
    errors: list[str] = []
    grabber.error.connect(errors.append)
    try:
        emitted = grabber._handle_acquisition_exception(_new_buffer_timeout())
    finally:
        close_grabber(grabber)

    assert emitted is False
    assert errors == []


def test_repeated_new_buffer_timeouts_escalate_once() -> None:
    grabber = Grabber()
    errors: list[str] = []
    grabber.error.connect(errors.append)
    try:
        emitted = [
            grabber._handle_acquisition_exception(_new_buffer_timeout())
            for _ in range(Grabber.NEW_BUFFER_TIMEOUT_ALERT_COUNT + 2)
        ]
    finally:
        close_grabber(grabber)

    assert emitted.count(True) == 1
    assert len(errors) == 1
    assert "NEW_BUFFER_DATA" in errors[0]


def test_valid_frame_resets_timeout_escalation_and_suppresses_recovery_gap(
    caplog,
) -> None:
    grabber = Grabber()
    errors: list[str] = []
    suppressed_gaps: list[bool] = []
    grabber.error.connect(errors.append)
    grabber.frame_gap_suppressed.connect(lambda: suppressed_gaps.append(True))
    grabber._last_frame_timestamp = time.monotonic() - 1.0
    try:
        for _ in range(Grabber.NEW_BUFFER_TIMEOUT_ALERT_COUNT - 1):
            grabber._handle_acquisition_exception(_new_buffer_timeout())
        with caplog.at_level(logging.WARNING):
            grabber._emit_frame(FakeRgbImage())
        for _ in range(Grabber.NEW_BUFFER_TIMEOUT_ALERT_COUNT - 1):
            grabber._handle_acquisition_exception(_new_buffer_timeout())
    finally:
        close_grabber(grabber)

    assert errors == []
    assert suppressed_gaps == [True]
    assert "Camera frame gap" not in caplog.text


def test_unrelated_acquisition_exception_emits_immediately() -> None:
    grabber = Grabber()
    errors: list[str] = []
    grabber.error.connect(errors.append)
    try:
        wrong_code = grabber._handle_acquisition_exception(
            FakeSpinError("NEW_BUFFER_DATA", -1001)
        )
        wrong_message = grabber._handle_acquisition_exception(
            FakeSpinError("different timeout", -1011)
        )
    finally:
        close_grabber(grabber)

    assert wrong_code is True
    assert wrong_message is True
    assert errors == ["NEW_BUFFER_DATA", "different timeout"]
