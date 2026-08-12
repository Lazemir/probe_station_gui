from __future__ import annotations

from probe_station_gui.camera.genicam_nodes import GenICamNodes
from probe_station_gui.camera.settings_transactions import CameraSettingsTransactions
from tests.camera.camera_settings_test_support import (
    FakeCamera,
    FakeNode,
    FakeNodeMap,
)


def make_transactions(
    camera_nodes: list[FakeNode],
) -> tuple[CameraSettingsTransactions, FakeCamera]:
    camera = FakeCamera(FakeNodeMap(camera_nodes))
    nodes = GenICamNodes(camera, is_streaming=lambda: True)
    return (
        CameraSettingsTransactions(nodes, is_streaming=lambda: True),
        camera,
    )


def test_temporary_camera_settings_restore_saved_values_in_reverse_order() -> None:
    selector = FakeNode(
        "TriggerSelector",
        "enum",
        "FrameStart",
        entries=("FrameStart",),
    )
    source = FakeNode(
        "TriggerSource",
        "enum",
        "Software",
        entries=("Software", "Line0"),
    )
    mode = FakeNode("TriggerMode", "enum", "Off", entries=("Off", "On"))
    transactions, _camera = make_transactions([selector, source, mode])

    result = transactions.apply_temporary(
        {
            "restore_key": "laser",
            "settings": [
                {"node_name": "TriggerSelector", "value": "FrameStart"},
                {"node_name": "TriggerSource", "value": "Line0"},
                {"node_name": "TriggerMode", "value": "On"},
            ],
        }
    )
    assert result["ok"] is True
    assert selector.value == "FrameStart"
    assert source.value == "Line0"
    assert mode.value == "On"

    result = transactions.restore_temporary({"restore_key": "laser"})
    assert result["ok"] is True
    assert mode.value == "Off"
    assert source.value == "Software"
    assert selector.value == "FrameStart"
    assert mode.set_values == ["On", "Off"]
    assert source.set_values == ["Line0", "Software"]


def test_temporary_camera_settings_roll_back_when_later_setting_fails() -> None:
    source = FakeNode(
        "TriggerSource",
        "enum",
        "Software",
        entries=("Software", "Line0"),
    )
    mode = FakeNode(
        "TriggerMode",
        "enum",
        "Off",
        writable=False,
        entries=("Off", "On"),
    )
    transactions, _camera = make_transactions([source, mode])

    result = transactions.apply_temporary(
        {
            "restore_key": "laser",
            "settings": [
                {"node_name": "TriggerSource", "value": "Line0"},
                {"node_name": "TriggerMode", "value": "On"},
            ],
        }
    )

    assert result["ok"] is False
    assert source.value == "Software"
    missing = transactions.restore_temporary({"restore_key": "laser"})
    assert missing["ok"] is True
    assert missing["nodes"] == []


def test_batch_validates_every_node_before_first_write() -> None:
    exposure_auto = FakeNode(
        "ExposureAuto",
        "enum",
        "Continuous",
        entries=("Off", "Continuous"),
    )
    exposure_time = FakeNode(
        "ExposureTime",
        "float",
        3076.14,
        writable=False,
    )
    transactions, _camera = make_transactions([exposure_auto, exposure_time])

    result = transactions.apply_batch(
        {
            "request_id": "batch-2",
            "settings": [
                {"node_name": "ExposureAuto", "value": "Off"},
                {"node_name": "ExposureTime", "value": 1800.0},
            ],
        }
    )

    assert result["ok"] is False
    assert result["request_id"] == "batch-2"
    assert exposure_auto.set_values == []
    assert exposure_auto.value == "Continuous"


def test_batch_rolls_back_changed_nodes_in_reverse_order() -> None:
    gain = FakeNode("Gain", "float", 0.0)
    exposure_auto = FakeNode(
        "ExposureAuto",
        "enum",
        "Continuous",
        entries=("Off", "Continuous"),
    )
    exposure_mode = FakeNode(
        "ExposureMode",
        "enum",
        "Timed",
        entries=("Timed",),
    )
    transactions, _camera = make_transactions([gain, exposure_auto, exposure_mode])

    result = transactions.apply_batch(
        {
            "request_id": "batch-3",
            "settings": [
                {"node_name": "Gain", "value": 2.0},
                {"node_name": "ExposureAuto", "value": "Off"},
                {"node_name": "ExposureMode", "value": "Invalid"},
            ],
        }
    )

    assert result["ok"] is False
    assert gain.value == 0.0
    assert exposure_auto.value == "Continuous"
    assert exposure_auto.set_values == ["Off", "Continuous"]
    assert gain.set_values == [2.0, 0.0]
    assert result["rollback_errors"] == []


def test_batch_rejects_duplicate_nodes_without_writing() -> None:
    gain = FakeNode("Gain", "float", 0.0)
    transactions, _camera = make_transactions([gain])

    result = transactions.apply_batch(
        {
            "request_id": "batch-4",
            "settings": [
                {"node_name": "Gain", "value": 1.0},
                {"node_name": "Gain", "value": 2.0},
            ],
        }
    )

    assert result["ok"] is False
    assert "Duplicate" in result["message"]
    assert gain.set_values == []


def test_settings_transactions_never_pause_camera_acquisition() -> None:
    gain = FakeNode("Gain", "float", 0.0)
    transactions, camera = make_transactions([gain])

    batch = transactions.apply_batch(
        {
            "request_id": "batch-streaming",
            "settings": [{"node_name": "Gain", "value": 1.0}],
        }
    )
    temporary = transactions.apply_temporary(
        {
            "restore_key": "streaming",
            "settings": [{"node_name": "Gain", "value": 2.0}],
        }
    )
    restored = transactions.restore_temporary({"restore_key": "streaming"})

    assert batch["ok"] is True
    assert batch["streaming"] is True
    assert temporary["ok"] is True
    assert restored["ok"] is True
    assert camera.begin_calls == 0
    assert camera.end_calls == 0


def test_failed_restore_keeps_saved_values_for_retry() -> None:
    source = FakeNode(
        "TriggerSource",
        "enum",
        "Software",
        entries=("Software", "Line0"),
        set_error=lambda value: (
            RuntimeError("restore failed") if value == "Software" else None
        ),
    )
    transactions, _camera = make_transactions([source])
    applied = transactions.apply_temporary(
        {
            "restore_key": "laser",
            "settings": [{"node_name": "TriggerSource", "value": "Line0"}],
        }
    )

    first = transactions.restore_temporary({"restore_key": "laser"})
    second = transactions.restore_temporary({"restore_key": "laser"})

    assert applied["ok"] is True
    assert first["ok"] is False
    assert second["ok"] is False
    assert first["errors"] == ["TriggerSource: restore failed"]
    assert second["errors"] == first["errors"]


def test_temporary_restore_key_cannot_be_reused_while_active() -> None:
    gain = FakeNode("Gain", "float", 0.0)
    transactions, _camera = make_transactions([gain])
    first = transactions.apply_temporary(
        {
            "restore_key": "calibration",
            "settings": [{"node_name": "Gain", "value": 1.0}],
        }
    )

    repeated = transactions.apply_temporary(
        {
            "restore_key": "calibration",
            "settings": [{"node_name": "Gain", "value": 2.0}],
        }
    )

    assert first["ok"] is True
    assert repeated["ok"] is False
    assert "already active" in repeated["message"]
    assert gain.value == 1.0
