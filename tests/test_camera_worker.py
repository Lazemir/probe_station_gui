from __future__ import annotations

import queue

from probe_station_gui.camera_worker import Grabber


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
