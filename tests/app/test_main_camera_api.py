from __future__ import annotations

import logging
import threading
from types import SimpleNamespace

from PySide6.QtCore import QByteArray
from PySide6.QtGui import QColor, QImage

from tests.app.import_reset import restore_real_imports_for_main


restore_real_imports_for_main()

from main import Main
from probe_station_gui.settings.sections import ExposurePolicySettings
from probe_station_gui.settings.manager import Settings, SettingsManager


class _CameraBroker:
    def __init__(self) -> None:
        self.manual_exposure_write = None
        self.trusted_writes: list[list[tuple[str, object]]] = []

    def read_settings(self, _names=None) -> dict[str, object]:
        return {"accepted": True, "nodes": []}

    def write_settings(self, _settings) -> dict[str, object]:
        raise AssertionError("controller-owned writes must not use the public path")

    def write_settings_trusted(self, settings) -> dict[str, object]:
        self.trusted_writes.append(list(settings))
        return {"accepted": True, "nodes": []}

    def set_manual_exposure_write(self, callback) -> None:
        self.manual_exposure_write = callback


def _compose_policy_window(
    *,
    auto_enabled: bool = True,
    engine: str = "software",
):
    saved: list[object] = []
    window = Main.__new__(Main)
    window.settings_manager = SimpleNamespace(
        exposure_policy_configuration=lambda: ExposurePolicySettings(
            auto_enabled=auto_enabled,
            engine=engine,
        ),
        set_exposure_policy_configuration=saved.append,
    )
    window._camera_api_broker = _CameraBroker()
    window._camera_auto_exposure_controller = SimpleNamespace(
        run=lambda _config=None: {"accepted": True, "converged": True}
    )
    window._read_camera_auto_exposure_frame = lambda _counter, _timeout: None
    Main._compose_camera_exposure_policy(window)
    return window, saved


def _shutdown_policy_window(window) -> None:
    window._exposure_policy_adapter.shutdown()
    window._exposure_policy_controller.shutdown()


def test_main_composes_policy_from_persisted_settings() -> None:
    window, _saved = _compose_policy_window()
    try:
        state = window._exposure_policy_controller.snapshot()

        assert state["engine"] == "software"
        assert state["auto_enabled"] is True
        assert window._optical_session_manager._controller is (
            window._exposure_policy_controller
        )
        assert window._exposure_policy_adapter._controller is (
            window._exposure_policy_controller
        )
    finally:
        _shutdown_policy_window(window)


def test_main_injects_optical_session_manager_into_stage_controller(
    monkeypatch,
) -> None:
    class FakeStageController:
        def __init__(self) -> None:
            self.session_manager = None

        def set_optical_session_manager(self, manager) -> None:
            self.session_manager = manager

    manager = SimpleNamespace(open=lambda _operation: None)
    window = Main.__new__(Main)
    window._optical_session_manager = manager
    monkeypatch.setitem(
        Main._create_stage_controller.__globals__,
        "StageController",
        FakeStageController,
    )

    controller = Main._create_stage_controller(window)

    assert controller.session_manager is manager


def test_main_policy_persists_and_public_exposure_write_uses_controller_guard() -> None:
    window, saved = _compose_policy_window(auto_enabled=False)
    try:
        window._exposure_policy_controller.set_policy(
            auto_enabled=False,
            engine="camera",
        )
        result = window._camera_api_broker.manual_exposure_write(
            lambda: {"accepted": True, "nodes": []}
        )

        assert saved[-1].auto_enabled is False
        assert saved[-1].engine.value == "camera"
        assert result["accepted"] is True
    finally:
        _shutdown_policy_window(window)


def test_settings_dialog_transaction_preserves_concurrent_exposure_policy(
    tmp_path,
) -> None:
    manager = SettingsManager.__new__(SettingsManager)
    manager._settings = Settings(
        exposure_policy=ExposurePolicySettings(
            auto_enabled=False,
            engine="camera",
        )
    )
    manager._config_dir = tmp_path
    manager._config_path = tmp_path / "settings.json"
    manager._logger = logging.getLogger(__name__)
    manager._settings_lock = threading.RLock()
    manager.apply = lambda: None
    stale_dialog_settings = Settings()
    stale_dialog_settings.design_last_directory = "C:/dialog-selection"
    window = Main.__new__(Main)
    window.settings_manager = manager
    window._apply_settings = lambda: None
    window._optical_calibration_runtime = SimpleNamespace(
        state=lambda: SimpleNamespace(
            active_run_id=None,
            active_kind=None,
            parent_session_token=None,
        )
    )

    Main._apply_settings_from_dialog(window, stale_dialog_settings)

    assert manager.settings.design_last_directory == "C:/dialog-selection"
    assert manager.settings.exposure_policy.to_dict() == {
        "auto_enabled": False,
        "engine": "camera",
    }


def test_controller_owned_camera_writes_use_trusted_broker_path() -> None:
    window = Main.__new__(Main)
    broker = _CameraBroker()
    window._camera_api_broker = broker

    result = Main._write_camera_auto_exposure_settings(
        window,
        [("ExposureAuto", "Off"), ("ExposureTime", 1800.0)],
    )

    assert result["accepted"] is True
    assert broker.trusted_writes == [
        [("ExposureAuto", "Off"), ("ExposureTime", 1800.0)]
    ]


def test_api_server_setup_passes_exposure_policy_controller_callbacks(
    monkeypatch,
) -> None:
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
    policy = SimpleNamespace(
        snapshot=lambda: {"auto_enabled": True, "engine": "software"},
        set_policy=lambda **_policy: {"accepted": True},
        run_once=lambda: {"accepted": True},
    )
    window._exposure_policy_controller = policy
    window._api_camera_frame = lambda _space, _counter, _timeout: {}
    monkeypatch.setitem(
        Main._configure_api_server_from_settings.__globals__,
        "ProbeStationApiServer",
        FakeServer,
    )

    Main._configure_api_server_from_settings(window, start_if_enabled=False)

    assert len(created) == 1
    assert "camera_auto_exposure_callback" not in created[0]
    assert created[0]["camera_exposure_policy_snapshot_callback"] is policy.snapshot
    assert created[0]["camera_exposure_policy_set_callback"] is policy.set_policy
    assert created[0]["camera_exposure_once_callback"] is policy.run_once


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


def test_raw_camera_reads_use_acquisition_worker_cache_when_available() -> None:
    raw_frame = _solid_image("green")

    class DirectFrameGrabber:
        @staticmethod
        def latest_frame_counter() -> int:
            return 23

        @staticmethod
        def wait_for_frame(*, after_counter, timeout_s):
            assert after_counter == 22
            assert timeout_s == 1.25
            return raw_frame.copy(), 23

    window = Main.__new__(Main)
    window.grabber = DirectFrameGrabber()
    window._latest_camera_frame_condition = threading.Condition()
    window._latest_raw_camera_frame = _solid_image("red")
    window._latest_raw_camera_frame_counter = 11

    frame, counter = Main._wait_for_raw_camera_frame(
        window,
        after_counter=22,
        timeout_s=1.25,
    )

    assert Main._latest_raw_camera_counter(window) == 23
    assert counter == 23
    assert frame is not None
    assert frame.pixelColor(0, 0) == QColor("green")


def test_exposure_policy_successful_start_latches_after_first_real_raw_frame() -> None:
    class StartAdapter:
        def __init__(self) -> None:
            self.start_requests = 0

        def request_start(self) -> None:
            self.start_requests += 1

    window = Main.__new__(Main)
    window._latest_camera_frame_condition = threading.Condition()
    window._latest_raw_camera_frame = None
    window._latest_raw_camera_frame_counter = 0
    window._exposure_policy_start_in_flight = False
    window._exposure_policy_started = False
    window._exposure_policy_start_retry_after = 0.0
    window._exposure_policy_adapter = StartAdapter()
    window.settings_manager = SimpleNamespace(
        active_objective_configuration=lambda: SimpleNamespace(
            name="",
            distortion_correction_configured=False,
            distortion_correction={},
        )
    )
    window._suppress_next_camera_ui_gap = False
    window._live_camera_frame_processor = SimpleNamespace(submit=lambda _request: True)

    Main._on_camera_frame(window, QImage())
    Main._on_camera_frame(window, _solid_image("red"))
    Main._on_camera_frame(window, _solid_image("blue"))
    Main._on_exposure_policy_command_finished(
        window,
        {"operation": "start", "accepted": True},
    )
    Main._on_camera_frame(window, _solid_image("green"))

    assert window._exposure_policy_adapter.start_requests == 1
    assert window._exposure_policy_start_in_flight is False
    assert window._exposure_policy_started is True
    assert window._latest_raw_camera_frame_counter == 4


def test_exposure_policy_failed_start_retries_after_backoff(monkeypatch) -> None:
    class StartAdapter:
        def __init__(self) -> None:
            self.start_requests = 0

        def request_start(self) -> None:
            self.start_requests += 1

    statuses: list[tuple[str, int]] = []
    clock = SimpleNamespace(now=100.0)
    monkeypatch.setattr(
        Main._on_camera_frame.__globals__["time"],
        "monotonic",
        lambda: clock.now,
    )
    window = Main.__new__(Main)
    window._latest_camera_frame_condition = threading.Condition()
    window._latest_raw_camera_frame = None
    window._latest_raw_camera_frame_counter = 0
    window._exposure_policy_start_in_flight = False
    window._exposure_policy_started = False
    window._exposure_policy_start_retry_after = 0.0
    window._exposure_policy_adapter = StartAdapter()
    window.settings_manager = SimpleNamespace(
        active_objective_configuration=lambda: SimpleNamespace(
            name="",
            distortion_correction_configured=False,
            distortion_correction={},
        )
    )
    window._suppress_next_camera_ui_gap = False
    window._live_camera_frame_processor = SimpleNamespace(submit=lambda _request: True)
    window._show_status = lambda message, timeout: statuses.append((message, timeout))

    Main._on_camera_frame(window, _solid_image("red"))
    Main._on_exposure_policy_command_finished(
        window,
        {
            "operation": "start",
            "accepted": False,
            "message": "Camera settings unavailable.",
        },
    )
    retry_after = window._exposure_policy_start_retry_after
    Main._on_camera_frame(window, _solid_image("blue"))
    clock.now = retry_after
    Main._on_camera_frame(window, _solid_image("green"))

    assert retry_after == 101.0
    assert window._exposure_policy_adapter.start_requests == 2
    assert window._exposure_policy_start_in_flight is True
    assert window._exposure_policy_started is False
    assert statuses == [
        (
            "Camera exposure setup failed: Camera settings unavailable.",
            8000,
        )
    ]


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


def _solid_image(color: str) -> QImage:
    image = QImage(8, 6, QImage.Format_RGB32)
    image.fill(QColor(color))
    return image
