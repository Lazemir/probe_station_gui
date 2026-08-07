"""Software coordinate-frame settings widgets."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import replace
from uuid import uuid4

from PySide6.QtCore import Signal
from PySide6.QtWidgets import (
    QFormLayout,
    QFrame,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QPushButton,
    QVBoxLayout,
    QWidget,
)

from probe_station_gui.coordinates import PhysicalMachinePose
from probe_station_gui.settings.manager import Settings
from probe_station_gui.settings.software_coordinates import (
    CustomFrameSettings,
    SoftwareCoordinateSettings,
)
from probe_station_gui.shared.wheel_guard import GuardedComboBox as QComboBox


class CoordinateSystemSettingsWidget(QWidget):
    """Edit user-managed software frames without touching controller WCS state."""

    availability_changed = Signal()

    def __init__(
        self,
        settings: SoftwareCoordinateSettings,
        parent: QWidget | None = None,
        *,
        physical_pose_source: Callable[[], PhysicalMachinePose | None] | None = None,
        stage_idle_source: Callable[[], bool] | None = None,
    ) -> None:
        super().__init__(parent)
        self._settings = settings.clone()
        self._physical_pose_source = physical_pose_source
        self._stage_idle_source = stage_idle_source
        self._current_frame_id: str | None = (
            self._settings.custom_frames[0].frame_id
            if self._settings.custom_frames
            else None
        )
        self._updating = False

        root = QVBoxLayout(self)
        root.setContentsMargins(0, 0, 0, 0)
        actions = QHBoxLayout()
        self._frame_combo = QComboBox(self)
        self._frame_combo.currentIndexChanged.connect(self._on_frame_selected)
        actions.addWidget(self._frame_combo, 1)
        self._add_button = QPushButton("Add", self)
        self._duplicate_button = QPushButton("Duplicate", self)
        self._rename_button = QPushButton("Rename", self)
        self._delete_button = QPushButton("Delete", self)
        for button, callback in (
            (self._add_button, self.add_custom_frame),
            (self._duplicate_button, self.duplicate_current_frame),
            (self._rename_button, self._rename_from_field),
            (self._delete_button, self.delete_current_frame),
        ):
            button.clicked.connect(callback)
            actions.addWidget(button)
        root.addLayout(actions)

        form = QFormLayout()
        self._name_edit = QLineEdit(self)
        self._name_edit.editingFinished.connect(self._rename_from_field)
        self._name_edit.textChanged.connect(self._on_editor_changed)
        form.addRow("Name", self._name_edit)
        self._fields: dict[str, QLineEdit] = {}
        for key, label, unit in (
            ("origin_x_mm", "Machine X origin", " mm"),
            ("origin_y_mm", "Machine Y origin", " mm"),
            ("reference_b_deg", "Reference B", " deg"),
            ("xy_angle_deg", "XY angle", " deg"),
            ("b_zero_deg", "B zero", " deg"),
            ("z_zero_mm", "Z zero", " mm"),
            ("a_zero_mm", "A zero", " mm"),
        ):
            field = QLineEdit(self)
            field.setPlaceholderText("Unset" if key in {"z_zero_mm", "a_zero_mm"} else "0")
            field.setToolTip(f"{label}{unit}")
            field.editingFinished.connect(self._apply_fields_from_editor)
            field.textChanged.connect(self._on_editor_changed)
            self._fields[key] = field
            form.addRow(f"{label} ({unit.strip()})", field)
        root.addLayout(form)

        self._use_current_button = QPushButton("Use current position", self)
        self._use_current_button.clicked.connect(self.use_current_position)
        root.addWidget(self._use_current_button)
        separator = QFrame(self)
        separator.setFrameShape(QFrame.HLine)
        root.addWidget(separator)
        self._status_label = QLabel(self)
        self._status_label.setWordWrap(True)
        root.addWidget(self._status_label)
        root.addStretch(1)
        self._refresh_editor()

    def settings(self) -> SoftwareCoordinateSettings:
        """Return an independent copy of the edited coordinate settings."""

        return self._settings_from_editor()

    def to_settings(self, settings: Settings) -> None:
        """Persist only software-frame configuration into application settings."""

        available, message = self.apply_availability()
        if not available:
            raise ValueError(message)
        settings.software_coordinates = self.settings()

    def apply_availability(self) -> tuple[bool, str]:
        """Return whether the current visible coordinate draft can be saved."""

        blocked_message = self._materialization_block_message()
        if blocked_message:
            return False, blocked_message
        if not self._stage_is_idle(show_status=False):
            return False, "Stage is busy."
        try:
            self._settings_from_editor()
        except (TypeError, ValueError) as exc:
            return False, str(exc)
        return True, ""

    def current_frame(self) -> CustomFrameSettings:
        """Return the selected frame, failing clearly when the list is empty."""

        if self._current_frame_id is None:
            raise ValueError("Select a custom frame first.")
        for frame in self._settings.custom_frames:
            if frame.frame_id == self._current_frame_id:
                return frame
        raise ValueError("Selected custom frame is unavailable.")

    def status_message(self) -> str:
        return self._status_label.text()

    def show_validation_message(self, message: str) -> None:
        self._set_status(message)

    def add_custom_frame(self) -> bool:
        blocked_message = self._materialization_block_message()
        if blocked_message:
            self._set_status(blocked_message)
            return False
        frame = CustomFrameSettings(
            frame_id=str(uuid4()),
            name=self._next_default_name(),
            origin_x_mm=0.0,
            origin_y_mm=0.0,
            reference_b_deg=0.0,
            xy_angle_deg=0.0,
            b_zero_deg=0.0,
        )
        self._settings.custom_frames = (*self._settings.custom_frames, frame)
        self._current_frame_id = frame.frame_id
        self._set_status("")
        self._refresh_editor()
        return True

    def duplicate_current_frame(self) -> bool:
        blocked_message = self._materialization_block_message()
        if blocked_message:
            self._set_status(blocked_message)
            return False
        try:
            source = self.current_frame()
        except ValueError as exc:
            self._set_status(str(exc))
            return False
        duplicate = replace(
            source,
            frame_id=str(uuid4()),
            name=self._duplicate_name(source.name),
        )
        self._settings.custom_frames = (*self._settings.custom_frames, duplicate)
        self._current_frame_id = duplicate.frame_id
        self._set_status("")
        self._refresh_editor()
        return True

    def rename_current_frame(self, name: object) -> bool:
        blocked_message = self._materialization_block_message()
        if blocked_message:
            self._set_status(blocked_message)
            return False
        try:
            current = self.current_frame()
            replacement = replace(current, name=str(name))
            if any(
                frame.frame_id != replacement.frame_id
                and frame.name.casefold() == replacement.name.casefold()
                for frame in self._settings.custom_frames
            ):
                raise ValueError("Custom frame names must be unique.")
        except (TypeError, ValueError) as exc:
            self._set_status(str(exc))
            return False
        self._replace_current(replacement)
        self._set_status("")
        self._refresh_editor()
        return True

    def delete_current_frame(self) -> bool:
        blocked_message = self._materialization_block_message()
        if blocked_message:
            self._set_status(blocked_message)
            return False
        if self._current_frame_id is None:
            self._set_status("Select a custom frame first.")
            return False
        frame_ids = [frame.frame_id for frame in self._settings.custom_frames]
        index = frame_ids.index(self._current_frame_id)
        self._settings = self._settings.without_custom_frame(self._current_frame_id)
        remaining = self._settings.custom_frames
        self._current_frame_id = (
            remaining[min(index, len(remaining) - 1)].frame_id if remaining else None
        )
        self._set_status("")
        self._refresh_editor()
        return True

    def set_current_name(self, name: object) -> bool:
        return self.rename_current_frame(name)

    def set_current_origin(self, *, x_mm: object, y_mm: object) -> bool:
        return self._apply_geometry_edit(origin_x_mm=x_mm, origin_y_mm=y_mm)

    def set_current_geometry(self, **values: object) -> bool:
        """Apply one validated geometry edit through the immutable settings model."""

        return self._apply_geometry_edit(**values)

    def use_current_position(self) -> bool:
        """Copy cached machine X/Y/B values without polling stage hardware."""

        if not self._stage_is_idle():
            return False
        if self._physical_pose_source is None:
            self._set_status("Current position is unavailable.")
            return False
        try:
            pose = self._physical_pose_source()
            if not isinstance(pose, PhysicalMachinePose):
                raise ValueError("Current position is unavailable.")
            return self._apply_geometry_edit(
                origin_x_mm=pose.require("X"),
                origin_y_mm=pose.require("Y"),
                reference_b_deg=pose.require("B"),
            )
        except (TypeError, ValueError):
            self._set_status("Current position is unavailable.")
            return False

    def _apply_geometry_edit(self, **values: object) -> bool:
        blocked_message = self._materialization_block_message()
        if blocked_message:
            self._set_status(blocked_message)
            return False
        if not self._stage_is_idle():
            return False
        try:
            replacement = self.current_frame().apply_geometry_edit(**values)
        except (TypeError, ValueError) as exc:
            self._set_status(str(exc))
            return False
        self._replace_current(replacement)
        self._set_status("")
        self._refresh_editor()
        return True

    def _stage_is_idle(self, *, show_status: bool = True) -> bool:
        if self._stage_idle_source is None:
            return True
        try:
            idle = bool(self._stage_idle_source())
        except Exception:
            idle = False
        if not idle and show_status:
            self._set_status("Stage is busy.")
        return idle

    def _settings_from_editor(self) -> SoftwareCoordinateSettings:
        blocked_message = self._materialization_block_message()
        if blocked_message:
            raise ValueError(blocked_message)
        settings = self._settings.clone()
        if self._current_frame_id is None:
            return settings
        current = self.current_frame()
        values: dict[str, object] = {}
        for key, field in self._fields.items():
            raw = field.text().strip()
            values[key] = None if key in {"z_zero_mm", "a_zero_mm"} and not raw else raw
        replacement = replace(current, name=self._name_edit.text())
        replacement = replacement.apply_geometry_edit(**values)
        frames = tuple(
            replacement if frame.frame_id == replacement.frame_id else frame
            for frame in settings.custom_frames
        )
        names = [frame.name.casefold() for frame in frames]
        if len(names) != len(set(names)):
            raise ValueError("Custom frame names must be unique.")
        return replace(settings, custom_frames=frames)

    def _replace_current(self, replacement: CustomFrameSettings) -> None:
        self._settings.custom_frames = tuple(
            replacement if frame.frame_id == replacement.frame_id else frame
            for frame in self._settings.custom_frames
        )

    def _next_default_name(self) -> str:
        existing = {frame.name.casefold() for frame in self._settings.custom_frames}
        index = 1
        while f"Custom frame {index}".casefold() in existing:
            index += 1
        return f"Custom frame {index}"

    def _duplicate_name(self, source_name: str) -> str:
        candidate = f"{source_name} copy"
        existing = {frame.name.casefold() for frame in self._settings.custom_frames}
        if candidate.casefold() not in existing:
            return candidate
        index = 2
        while f"{candidate} {index}".casefold() in existing:
            index += 1
        return f"{candidate} {index}"

    def _on_frame_selected(self, _index: int) -> None:
        if self._updating:
            return
        frame_id = self._frame_combo.currentData()
        self._current_frame_id = str(frame_id) if isinstance(frame_id, str) else None
        self._refresh_editor()

    def _on_editor_changed(self, _text: str) -> None:
        if not self._updating:
            self.availability_changed.emit()

    def _rename_from_field(self) -> None:
        if not self._updating:
            self.rename_current_frame(self._name_edit.text())

    def _apply_fields_from_editor(self) -> None:
        if self._updating or self._current_frame_id is None:
            return
        values: dict[str, object] = {}
        for key, field in self._fields.items():
            raw = field.text().strip()
            values[key] = None if key in {"z_zero_mm", "a_zero_mm"} and not raw else raw
        self._apply_geometry_edit(**values)

    def _refresh_editor(self) -> None:
        self._updating = True
        try:
            self._frame_combo.clear()
            for frame in self._settings.custom_frames:
                self._frame_combo.addItem(frame.name, frame.frame_id)
            if self._current_frame_id is not None:
                index = self._frame_combo.findData(self._current_frame_id)
                if index >= 0:
                    self._frame_combo.setCurrentIndex(index)
            has_frame = self._current_frame_id is not None
            editing_blocked = bool(self._materialization_block_message())
            self._add_button.setEnabled(not editing_blocked)
            for widget in (
                self._frame_combo,
                self._duplicate_button,
                self._rename_button,
                self._delete_button,
                self._use_current_button,
                self._name_edit,
                *self._fields.values(),
            ):
                widget.setEnabled(has_frame and not editing_blocked)
            if not has_frame:
                self._name_edit.clear()
                for field in self._fields.values():
                    field.clear()
                return
            frame = self.current_frame()
            self._name_edit.setText(frame.name)
            for key, field in self._fields.items():
                value = getattr(frame, key)
                field.setText("" if value is None else str(value))
        finally:
            self._updating = False
        self.availability_changed.emit()

    def _materialization_block_message(self) -> str:
        if not self._settings.materialization_blocked:
            return ""
        if self._settings.diagnostics:
            return self._settings.diagnostics[0]
        return "Software coordinate settings are unavailable."

    def _set_status(self, message: str) -> None:
        self._status_label.setText(message)


__all__ = ["CoordinateSystemSettingsWidget"]
