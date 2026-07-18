from __future__ import annotations

import threading
from types import SimpleNamespace

from PySide6.QtCore import QByteArray
from PySide6.QtGui import QColor, QImage

from tests.app.import_reset import restore_real_imports_for_main


restore_real_imports_for_main()

from main import Main
from probe_station_gui.camera.auto_exposure import AutoExposureBusyError


def test_api_server_setup_does_not_pass_removed_auto_exposure_callback(
    monkeypatch,
) -> None:
    import main as main_module

    created: list[dict[str, object]] = []

    class FakeServer:
        def __init__(self, **kwargs) -> None:
            created.append(kwargs)

    window = Main.__new__(Main)
    window.settings_manager = SimpleNamespace(
        api_configuration=lambda: SimpleNamespace(
            enabled=True,
            host="127.0.0.1",
            port=8765,
        )
    )
    window._api_settings_signature = None
    window._api_server = None
    window._submit_api_move_request = lambda _request: {}
    window._submit_api_status_request = lambda: {}
    window._submit_api_command_request_from_api_thread = lambda _request: {}
    window._authorize_api_request = lambda _key, _permission: {}
    window._camera_api_broker = SimpleNamespace(
        read_settings=lambda _names=None: {},
        write_settings=lambda _settings: {},
    )
    window._api_camera_frame = lambda _space, _counter, _timeout: {}
    monkeypatch.setattr(main_module, "ProbeStationApiServer", FakeServer)

    Main._configure_api_server_from_settings(window, start_if_enabled=False)

    assert len(created) == 1
    assert "camera_auto_exposure_callback" not in created[0]


def test_camera_api_frame_selects_raw_and_corrected_storage() -> None:
    window = Main.__new__(Main)
    window._latest_camera_frame_condition = threading.Condition()
    window._latest_raw_camera_frame = _solid_image("red")
    window._latest_raw_camera_frame_counter = 11
    window._latest_camera_frame = _solid_image("blue")
    window._latest_camera_frame_counter = 13

    raw = Main._api_camera_frame(window, "raw", None, 0.0)
    corrected = Main._api_camera_frame(window, "corrected", None, 0.0)

    raw_image = QImage.fromData(QByteArray(raw["data"]), "PNG")
    corrected_image = QImage.fromData(QByteArray(corrected["data"]), "PNG")
    assert raw["counter"] == 11
    assert corrected["counter"] == 13
    assert raw_image.pixelColor(0, 0) == QColor("red")
    assert corrected_image.pixelColor(0, 0) == QColor("blue")


def test_camera_api_submitters_forward_request_ids_and_order() -> None:
    class FakeGrabber:
        def __init__(self) -> None:
            self.calls = []

        def request_camera_settings_snapshot(self, *args, **kwargs) -> None:
            self.calls.append(("snapshot", args, kwargs))

        def request_camera_settings_batch(self, *args, **kwargs) -> None:
            self.calls.append(("batch", args, kwargs))

    window = Main.__new__(Main)
    window.grabber = FakeGrabber()

    Main._submit_camera_settings_snapshot(window, "read-1", ["Gain"])
    Main._submit_camera_settings_batch(
        window,
        "write-1",
        [("ExposureAuto", "Off"), ("ExposureTime", 1800.0)],
    )

    assert window.grabber.calls == [
        (
            "snapshot",
            ("camera", ["Gain"]),
            {"request_id": "read-1"},
        ),
        (
            "batch",
            ([("ExposureAuto", "Off"), ("ExposureTime", 1800.0)],),
            {"request_id": "write-1", "map_key": "camera"},
        ),
    ]


def test_camera_auto_exposure_api_parses_config_and_runs_controller() -> None:
    window = Main.__new__(Main)
    window._microscope_scan_running = lambda: False
    calls = []
    window._run_camera_auto_exposure = (
        lambda config: calls.append(config)
        or {
            "accepted": True,
            "converged": True,
            "final_exposure_us": 2300.0,
        }
    )

    result = Main._api_camera_auto_exposure(
        window,
        {"target_level": 230.0, "settling_frames": 1},
    )

    assert result["accepted"] is True
    assert calls[0].target_level == 230.0
    assert calls[0].settling_frames == 1


def test_camera_auto_exposure_api_rejects_scan_and_busy_controller() -> None:
    window = Main.__new__(Main)
    window._microscope_scan_running = lambda: True

    scan_busy = Main._api_camera_auto_exposure(window, None)

    assert scan_busy["status_code"] == 409
    window._microscope_scan_running = lambda: False
    window._run_camera_auto_exposure = lambda _config: (_ for _ in ()).throw(
        AutoExposureBusyError("Camera auto exposure is already running.")
    )

    camera_busy = Main._api_camera_auto_exposure(window, None)

    assert camera_busy["status_code"] == 409
    assert "already running" in camera_busy["message"]


def _solid_image(color: str) -> QImage:
    image = QImage(8, 6, QImage.Format_RGB32)
    image.fill(QColor(color))
    return image
