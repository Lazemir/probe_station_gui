from __future__ import annotations

import queue

from probe_station_gui.camera.worker import Grabber


class FakeEnumEntry:
    def __init__(self, name: str) -> None:
        self._name = name

    def is_implemented(self) -> bool:
        return True

    def is_available(self) -> bool:
        return True

    def get_enum_name(self) -> str:
        return self._name


class FakeNode:
    def __init__(
        self,
        name: str,
        node_type: str,
        value: object,
        *,
        writable: bool = True,
        readable: bool = True,
        entries: tuple[str, ...] = (),
    ) -> None:
        self.name = name
        self.node_type = node_type
        self.value = value
        self.writable = writable
        self.readable = readable
        self.entries = entries
        self.set_values: list[object] = []

    def get_name(self) -> str:
        return self.name

    def get_display_name(self) -> str:
        return self.name

    def is_implemented(self) -> bool:
        return True

    def is_available(self) -> bool:
        return True

    def is_readable(self) -> bool:
        return self.readable

    def is_writable(self) -> bool:
        return self.writable

    def get_node_type(self) -> str:
        return self.node_type

    def get_node_value_as_str(self) -> str:
        return str(self.value)

    def set_node_value(self, value: object) -> None:
        self.set_values.append(value)
        self.value = value

    def set_node_value_from_str(self, value: str, verify: bool = True) -> None:
        del verify
        if self.entries and value not in self.entries:
            raise ValueError(value)
        self.set_values.append(value)
        self.value = value

    def get_entries(self) -> list[FakeEnumEntry]:
        return [FakeEnumEntry(entry) for entry in self.entries]


class FakeNodeMap:
    def __init__(self, nodes: list[FakeNode]) -> None:
        self.nodes = {node.name: node for node in nodes}

    def get_node_by_name(self, name: str) -> FakeNode | None:
        return self.nodes.get(name)


class FakeCamera:
    def __init__(self, node_map: FakeNodeMap) -> None:
        self._node_map = node_map

    def get_node_map(self) -> FakeNodeMap:
        return self._node_map


def make_grabber(nodes: list[FakeNode]) -> Grabber:
    grabber = Grabber()
    grabber._camera = FakeCamera(FakeNodeMap(nodes))
    return grabber


def close_grabber(grabber: Grabber) -> None:
    grabber._camera_settings_executor.shutdown(wait=True, cancel_futures=True)


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
    grabber = make_grabber([selector, source, mode])
    try:
        result = grabber._apply_temporary_camera_settings(
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

        result = grabber._restore_temporary_camera_settings({"restore_key": "laser"})
        assert result["ok"] is True
        assert mode.value == "Off"
        assert source.value == "Software"
        assert selector.value == "FrameStart"
        assert mode.set_values == ["On", "Off"]
        assert source.set_values == ["Line0", "Software"]
    finally:
        close_grabber(grabber)


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
    grabber = make_grabber([source, mode])
    try:
        result = grabber._apply_temporary_camera_settings(
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
        assert "laser" not in grabber._temporary_camera_settings
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
        result = grabber._camera_settings_snapshot(command.payload)
        assert result["request_id"] == "snapshot-1"
    finally:
        close_grabber(grabber)


def test_partial_snapshot_keeps_unavailable_requested_node_visible() -> None:
    grabber = make_grabber([FakeNode("Gain", "float", 0.0)])
    try:
        result = grabber._camera_settings_partial_snapshot(
            "camera",
            ["Gain", "ExposureTime"],
        )

        nodes = result["maps"][0]["nodes"]
        assert [node["name"] for node in nodes] == ["Gain", "ExposureTime"]
        assert nodes[1]["available"] is False
        assert nodes[1]["readable"] is False
        assert nodes[1]["writable"] is False
        assert "ExposureTime" in nodes[1]["error"]
    finally:
        close_grabber(grabber)


def test_public_batch_request_queues_ordered_settings() -> None:
    grabber = make_grabber(
        [
            FakeNode("ExposureAuto", "enum", "Continuous", entries=("Off", "Continuous")),
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
    grabber = make_grabber([exposure_auto, exposure_time])
    try:
        result = grabber._apply_camera_settings_batch(
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
    finally:
        close_grabber(grabber)


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
    grabber = make_grabber([gain, exposure_auto, exposure_mode])
    try:
        result = grabber._apply_camera_settings_batch(
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
    finally:
        close_grabber(grabber)


def test_batch_rejects_duplicate_nodes_without_writing() -> None:
    gain = FakeNode("Gain", "float", 0.0)
    grabber = make_grabber([gain])
    try:
        result = grabber._apply_camera_settings_batch(
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
    finally:
        close_grabber(grabber)
