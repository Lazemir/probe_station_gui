"""Lazily materialized GenICam feature page."""

from __future__ import annotations

from collections.abc import Callable

from PySide6.QtWidgets import QFrame, QScrollArea, QVBoxLayout, QWidget

from probe_station_gui.dialogs.camera_feature_editors import (
    CameraFeatureEditors,
    NodePayload,
)


class CameraFeaturePage(QWidget):
    """Keep hidden feature pages cheap while retaining their latest node state."""

    def __init__(
        self,
        map_key: str,
        apply_callback: Callable[[str, str, object], None],
        execute_callback: Callable[[str, str], None],
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self._map_key = map_key
        self._nodes: list[NodePayload] = []
        self._layout_dirty = False

        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        self._scroll_area = QScrollArea(self)
        self._scroll_area.setFrameShape(QFrame.NoFrame)
        self._scroll_area.setWidgetResizable(True)
        layout.addWidget(self._scroll_area, 1)

        self._editors = CameraFeatureEditors(
            map_key,
            apply_callback,
            execute_callback,
            self,
        )
        self._scroll_area.setWidget(self._editors)

    def map_key(self) -> str:
        return self._map_key

    def showEvent(self, event: object) -> None:  # type: ignore[override]
        super().showEvent(event)
        self._sync_layout_if_visible()

    def node_names(self) -> list[str]:
        return [
            str(node.get("name") or "")
            for node in self._nodes
            if str(node.get("name") or "")
        ]

    def set_nodes(self, nodes: list[NodePayload]) -> None:
        self._nodes = [
            node for node in nodes if str(node.get("type") or "") != "category"
        ]
        if not self.isVisible():
            self._layout_dirty = True
            return
        self._editors.set_nodes(self._nodes)
        self._layout_dirty = False

    def update_node(self, node: NodePayload) -> bool:
        node_name = str(node.get("name") or "")
        if not node_name:
            return False
        for index, existing in enumerate(self._nodes):
            if str(existing.get("name") or "") != node_name:
                continue
            merged = dict(existing)
            merged.update(node)
            self._nodes[index] = merged
            if not self.isVisible():
                self._layout_dirty = True
                return True
            return self._editors.update_node(merged)
        return False

    def queue_current_editor_value(self) -> None:
        self._editors.queue_current_editor_value()

    def has_edit_focus(self) -> bool:
        return self._editors.has_edit_focus()

    def _sync_layout_if_visible(self) -> None:
        if not self.isVisible():
            return
        if self._layout_dirty:
            self._editors.set_nodes(self._nodes)
            self._layout_dirty = False


__all__ = ["CameraFeaturePage"]
