from __future__ import annotations

from collections.abc import Callable


class FakeEnumEntry:
    def __init__(
        self,
        name: str,
        *,
        implemented: bool = True,
        available: bool = True,
    ) -> None:
        self._name = name
        self._implemented = implemented
        self._available = available

    def is_implemented(self) -> bool:
        return self._implemented

    def is_available(self) -> bool:
        return self._available

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
        available: bool = True,
        implemented: bool = True,
        entries: tuple[str, ...] = (),
        set_error: Callable[[object], Exception | None] | None = None,
    ) -> None:
        self.name = name
        self.node_type = node_type
        self.value = value
        self.writable = writable
        self.readable = readable
        self.available = available
        self.implemented = implemented
        self.entries = entries
        self.set_error = set_error
        self.set_values: list[object] = []
        self.execute_calls: list[bool] = []

    def get_name(self) -> str:
        return self.name

    def get_display_name(self) -> str:
        return self.name

    def is_implemented(self) -> bool:
        return self.implemented

    def is_available(self) -> bool:
        return self.available

    def is_readable(self) -> bool:
        return self.readable

    def is_writable(self) -> bool:
        return self.writable

    def get_node_type(self) -> str:
        return self.node_type

    def get_node_value_as_str(self) -> str:
        return str(self.value)

    def set_node_value(self, value: object) -> None:
        if self.set_error is not None:
            error = self.set_error(value)
            if error is not None:
                raise error
        self.set_values.append(value)
        self.value = value

    def set_node_value_from_str(self, value: str, verify: bool = True) -> None:
        del verify
        if self.entries and value not in self.entries:
            raise ValueError(value)
        self.set_node_value(value)

    def execute_node(self, verify: bool = True) -> None:
        self.execute_calls.append(verify)

    def get_entries(self) -> list[FakeEnumEntry]:
        return [FakeEnumEntry(entry) for entry in self.entries]


class FakeNodeMap:
    def __init__(self, nodes: list[FakeNode]) -> None:
        self.nodes = {node.name: node for node in nodes}

    def get_node_by_name(self, name: str) -> FakeNode | None:
        return self.nodes.get(name)

    def get_nodes(self) -> list[FakeNode]:
        return list(self.nodes.values())


class FakeCamera:
    def __init__(
        self,
        node_map: FakeNodeMap,
        stream_node_map: FakeNodeMap | None = None,
        device_node_map: FakeNodeMap | None = None,
    ) -> None:
        self._node_map = node_map
        self._stream_node_map = stream_node_map or FakeNodeMap([])
        self._device_node_map = device_node_map or FakeNodeMap([])
        self.begin_calls = 0
        self.end_calls = 0

    def get_node_map(self) -> FakeNodeMap:
        return self._node_map

    def get_tl_stream_node_map(self) -> FakeNodeMap:
        return self._stream_node_map

    def get_tl_dev_node_map(self) -> FakeNodeMap:
        return self._device_node_map

    def begin_acquisition(self) -> None:
        self.begin_calls += 1

    def end_acquisition(self) -> None:
        self.end_calls += 1
