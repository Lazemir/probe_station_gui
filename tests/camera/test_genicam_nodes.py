from __future__ import annotations

from probe_station_gui.camera.genicam_nodes import GenICamNodes
from tests.camera.camera_settings_test_support import (
    FakeCamera,
    FakeNode,
    FakeNodeMap,
)


def make_nodes(
    camera_nodes: list[FakeNode],
    *,
    device_nodes: list[FakeNode] | None = None,
    stream_nodes: list[FakeNode] | None = None,
    streaming: bool = True,
) -> GenICamNodes:
    camera = FakeCamera(
        FakeNodeMap(camera_nodes),
        FakeNodeMap(stream_nodes or []),
        FakeNodeMap(device_nodes or []),
    )
    return GenICamNodes(camera, is_streaming=lambda: streaming)


def test_partial_snapshot_keeps_unavailable_requested_node_visible() -> None:
    nodes = make_nodes([FakeNode("Gain", "float", 0.0)])

    result = nodes.snapshot(
        {
            "map_key": "camera",
            "node_names": ["Gain", "ExposureTime"],
        }
    )

    result_nodes = result["maps"][0]["nodes"]
    assert [node["name"] for node in result_nodes] == ["Gain", "ExposureTime"]
    assert result_nodes[1]["available"] is False
    assert result_nodes[1]["readable"] is False
    assert result_nodes[1]["writable"] is False
    assert "ExposureTime" in result_nodes[1]["error"]


def test_full_snapshot_preserves_map_order_schema_and_streaming() -> None:
    nodes = make_nodes(
        [
            FakeNode(
                "ExposureAuto",
                "enum",
                "Continuous",
                entries=("Off", "Continuous"),
            ),
            FakeNode("Gain", "float", 1.25),
        ],
        device_nodes=[FakeNode("DeviceVendorName", "string", "FLIR")],
        stream_nodes=[FakeNode("StreamBufferCount", "integer", 8)],
    )

    result = nodes.snapshot({"request_id": "snapshot-id"})

    assert result["ok"] is True
    assert result["request_id"] == "snapshot-id"
    assert result["streaming"] is True
    assert [node_map["key"] for node_map in result["maps"]] == [
        "camera",
        "transport_device",
        "transport_stream",
    ]
    assert [node["name"] for node in result["maps"][0]["nodes"]] == [
        "ExposureAuto",
        "Gain",
    ]
    exposure = result["maps"][0]["nodes"][0]
    assert exposure["type"] == "enum"
    assert exposure["value"] == "Continuous"
    assert exposure["entries"] == ["Off", "Continuous"]


def test_setting_updates_coerce_supported_node_types() -> None:
    boolean = FakeNode("ReverseX", "boolean", False)
    integer = FakeNode("PacketSize", "integer", 512)
    floating = FakeNode("Gain", "float", 0.0)
    enumeration = FakeNode("Mode", "enum", "Off", entries=("Off", "On"))
    string = FakeNode("Label", "string", "old")
    nodes = make_nodes([boolean, integer, floating, enumeration, string])

    for node_name, value in (
        ("ReverseX", "enabled"),
        ("PacketSize", "0x400"),
        ("Gain", "1.5"),
        ("Mode", "On"),
        ("Label", 42),
    ):
        result = nodes.apply_setting(
            {"map_key": "camera", "node_name": node_name, "value": value}
        )
        assert result["ok"] is True

    assert boolean.value is True
    assert integer.value == 1024
    assert floating.value == 1.5
    assert enumeration.value == "On"
    assert string.value == "42"


def test_command_execution_returns_refreshed_node() -> None:
    command = FakeNode("UserSetLoad", "command", "ready")
    nodes = make_nodes([command])

    result = nodes.execute_command({"map_key": "camera", "node_name": "UserSetLoad"})

    assert result["ok"] is True
    assert result["node_name"] == "UserSetLoad"
    assert result["node"]["name"] == "UserSetLoad"
    assert command.execute_calls == [True]
