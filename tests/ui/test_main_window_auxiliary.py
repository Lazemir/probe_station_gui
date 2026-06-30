from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from typing import Any

from probe_station_gui.views import main_window_auxiliary as auxiliary_ui
from probe_station_gui.views.main_window_auxiliary import (
    create_design_layout_window,
    show_connection_dialog,
    show_microscope_scan_dialog,
    show_surface_map_window,
    sync_contact_calibration_window_action,
    sync_design_layout_window_action,
    toggle_contact_calibration_window,
    toggle_design_layout_window,
)


class _Signal:
    def __init__(self) -> None:
        self.connections: list[Any] = []

    def connect(self, slot: Any) -> None:
        self.connections.append(slot)

    def emit(self, *args: object) -> None:
        for slot in list(self.connections):
            slot(*args)


class _FakeSettingsManager:
    def __init__(self, path: Path) -> None:
        self._path = path

    def config_dir(self) -> Path:
        return self._path

    def design_last_directory(self) -> Path:
        return self._path / "designs"


class _FakeSurfaceMapWindow:
    instances: list["_FakeSurfaceMapWindow"] = []

    def __init__(
        self,
        *,
        stage_status_provider: Any,
        stage_move_requester: Any,
        settings_path: Path,
        parent: object | None,
    ) -> None:
        self.stage_status_provider = stage_status_provider
        self.stage_move_requester = stage_move_requester
        self.settings_path = Path(settings_path)
        self.parent = parent
        self.capture_running_changed = _Signal()
        self.show_count = 0
        self.raise_count = 0
        self.__class__.instances.append(self)

    def showNormal(self) -> None:  # noqa: N802 - Qt naming
        self.show_count += 1

    def raise_(self) -> None:
        self.raise_count += 1


class _SurfaceMapOwner:
    def __init__(self, config_dir: Path) -> None:
        self.surface_map_window = None
        self.settings_manager = _FakeSettingsManager(config_dir)
        self.coordinate_apply_updates = 0

    def _surface_map_stage_status(self) -> str:
        return "idle"

    def _surface_map_move_to_xy(self, x_value: float, y_value: float) -> tuple[float, float]:
        return (float(x_value), float(y_value))

    def _update_stage_coordinate_apply_state(self) -> None:
        self.coordinate_apply_updates += 1


def test_show_surface_map_window_creates_once_and_keeps_lazy_wiring(tmp_path: Path) -> None:
    _FakeSurfaceMapWindow.instances.clear()
    owner = _SurfaceMapOwner(tmp_path)

    show_surface_map_window(owner, window_class=_FakeSurfaceMapWindow)
    show_surface_map_window(owner, window_class=_FakeSurfaceMapWindow)

    assert len(_FakeSurfaceMapWindow.instances) == 1
    window = _FakeSurfaceMapWindow.instances[0]
    assert owner.surface_map_window is window
    assert window.settings_path == tmp_path / "surface-map-settings.json"
    assert window.parent is None
    assert window.show_count == 2
    assert window.raise_count == 2

    window.capture_running_changed.emit(True)

    assert owner.coordinate_apply_updates == 1


class _FakeMicroscopeScanDialog:
    instances: list["_FakeMicroscopeScanDialog"] = []

    def __init__(self, *, default_output_dir: str, parent: object | None) -> None:
        self.default_output_dir = default_output_dir
        self.parent = parent
        self.scan_requested = _Signal()
        self.stop_requested = _Signal()
        self.finished = _Signal()
        self.show_count = 0
        self.raise_count = 0
        self.activate_count = 0
        self.__class__.instances.append(self)

    def show(self) -> None:
        self.show_count += 1

    def raise_(self) -> None:
        self.raise_count += 1

    def activateWindow(self) -> None:  # noqa: N802 - Qt naming
        self.activate_count += 1


class _MicroscopeScanOwner:
    def __init__(self, default_dir: str) -> None:
        self.microscope_scan_dialog = None
        self.default_dir = default_dir
        self.default_dir_calls = 0
        self.calls: list[tuple[str, object]] = []
        self._design_session = SimpleNamespace(document=object())

    def _start_microscope_scan(self, configuration: object) -> None:
        self.calls.append(("scan", configuration))

    def _request_stop_microscope_scan(self) -> None:
        self.calls.append(("stop", None))

    def _clear_microscope_scan_dialog(self) -> None:
        self.calls.append(("clear", None))
        self.microscope_scan_dialog = None


def test_show_microscope_scan_dialog_reuses_until_finished(monkeypatch) -> None:
    _FakeMicroscopeScanDialog.instances.clear()
    owner = _MicroscopeScanOwner("C:/scan-output")

    def default_output_dir(_document: object) -> str:
        owner.default_dir_calls += 1
        return owner.default_dir

    monkeypatch.setattr(
        auxiliary_ui.microscope_scan,
        "default_output_dir",
        default_output_dir,
    )

    show_microscope_scan_dialog(owner, dialog_class=_FakeMicroscopeScanDialog)
    show_microscope_scan_dialog(owner, dialog_class=_FakeMicroscopeScanDialog)

    assert len(_FakeMicroscopeScanDialog.instances) == 1
    dialog = _FakeMicroscopeScanDialog.instances[0]
    assert owner.microscope_scan_dialog is dialog
    assert dialog.default_output_dir == "C:/scan-output"
    assert dialog.parent is None
    assert dialog.show_count == 2
    assert dialog.raise_count == 2
    assert dialog.activate_count == 2
    assert owner.default_dir_calls == 2

    configuration = object()
    dialog.scan_requested.emit(configuration)
    dialog.stop_requested.emit()
    dialog.finished.emit(0)

    assert owner.calls == [("scan", configuration), ("stop", None), ("clear", None)]
    assert owner.microscope_scan_dialog is None


class _FakeCheckAction:
    def __init__(self) -> None:
        self.checked_values: list[bool] = []
        self.blocked_values: list[bool] = []

    def blockSignals(self, blocked: bool) -> None:  # noqa: N802 - Qt naming
        self.blocked_values.append(bool(blocked))

    def setChecked(self, checked: bool) -> None:  # noqa: N802 - Qt naming
        self.checked_values.append(bool(checked))


class _FakeDesignWindow:
    def __init__(self) -> None:
        self.show_count = 0
        self.hide_count = 0

    def show_and_raise(self) -> None:
        self.show_count += 1

    def hide(self) -> None:
        self.hide_count += 1


class _ToggleDesignOwner:
    def __init__(self) -> None:
        self.design_layout_window = None
        self._design_layout_window_class = None
        self._design_layout_window_requested = False
        self._design_layout_window_action = _FakeCheckAction()
        self.calls: list[str] = []

    def _create_design_layout_window(self, window_class: object) -> None:
        self.calls.append(f"create:{window_class!r}")

    def _show_status(self, message: str) -> None:
        self.calls.append(message)

    def _preload_design_layout_window(self) -> None:
        self.calls.append("preload")

    def _collapse_alignment_panel_if_ready(self) -> None:
        self.calls.append("collapse")


def test_toggle_design_layout_window_preserves_lazy_preload_and_action_sync() -> None:
    owner = _ToggleDesignOwner()

    toggle_design_layout_window(owner, True)

    assert owner._design_layout_window_requested is True
    assert owner.calls == ["Preparing design window...", "preload"]

    window = _FakeDesignWindow()
    owner.design_layout_window = window
    toggle_design_layout_window(owner, True)
    toggle_design_layout_window(owner, False)
    sync_design_layout_window_action(owner, True)

    assert window.show_count == 1
    assert window.hide_count == 1
    assert owner.calls[-1] == "collapse"
    assert owner._design_layout_window_action.blocked_values == [True, False]
    assert owner._design_layout_window_action.checked_values == [True]


class _FakeContactWindow:
    def __init__(self) -> None:
        self.show_count = 0
        self.hide_count = 0
        self.lowering_values: list[float] = []

    def show_and_raise(self) -> None:
        self.show_count += 1

    def hide(self) -> None:
        self.hide_count += 1

    def set_current_needle_lowering(self, value: float) -> None:
        self.lowering_values.append(float(value))


class _FakeStageForContact:
    def __init__(self, lowering: float | None) -> None:
        self.lowering = lowering
        self.status_refreshes = 0

    def latest_axis_a_lowering(self) -> float | None:
        return self.lowering

    def request_status_refresh(self) -> None:
        self.status_refreshes += 1


class _ToggleContactOwner:
    def __init__(self, lowering: float | None = 1.25) -> None:
        self.contact_calibration_window = _FakeContactWindow()
        self._contact_calibration_window_action = _FakeCheckAction()
        self.stage_controller = _FakeStageForContact(lowering)
        self.serial_connection = None


def test_toggle_contact_calibration_window_preserves_raise_lowering_and_sync() -> None:
    owner = _ToggleContactOwner(1.25)

    toggle_contact_calibration_window(owner, True)
    toggle_contact_calibration_window(owner, False)
    sync_contact_calibration_window_action(owner, True)

    assert owner.contact_calibration_window.show_count == 1
    assert owner.contact_calibration_window.hide_count == 1
    assert owner.contact_calibration_window.lowering_values == [1.25]
    assert owner._contact_calibration_window_action.blocked_values == [True, False]
    assert owner._contact_calibration_window_action.checked_values == [True]


def test_toggle_contact_calibration_window_requests_status_when_lowering_unknown() -> None:
    owner = _ToggleContactOwner(None)
    owner.serial_connection = type("Serial", (), {"is_open": True})()

    toggle_contact_calibration_window(owner, True)

    assert owner.stage_controller.status_refreshes == 1


class _FakeConnectionPanel:
    def __init__(self) -> None:
        self.resources: list[str] = []

    def set_lcr_resource(self, value: str) -> None:
        self.resources.append(value)


class _FakeTabs:
    def __init__(self) -> None:
        self.indexes: list[int] = []

    def setCurrentIndex(self, value: int) -> None:  # noqa: N802 - Qt naming
        self.indexes.append(int(value))


class _FakeDialog:
    def __init__(self) -> None:
        self.calls: list[str] = []

    def show(self) -> None:
        self.calls.append("show")

    def raise_(self) -> None:
        self.calls.append("raise")

    def activateWindow(self) -> None:  # noqa: N802 - Qt naming
        self.calls.append("activate")


class _FakeLcrForDialog:
    def connection_label(self) -> str:
        return "LCR0"


def test_show_connection_dialog_updates_lcr_resource_and_selected_tab() -> None:
    owner = type("Owner", (), {})()
    owner.serial_connection_dialog = _FakeDialog()
    owner.serial_connection_panel = _FakeConnectionPanel()
    owner.serial_connection_tabs = _FakeTabs()
    owner.lcr_controller = _FakeLcrForDialog()

    show_connection_dialog(owner, "terminal")

    assert owner.serial_connection_panel.resources == ["LCR0"]
    assert owner.serial_connection_tabs.indexes == [1]
    assert owner.serial_connection_dialog.calls == ["show", "raise", "activate"]


class _FakeNavigatorPanel:
    def __init__(self) -> None:
        signal_names = (
            "load_design_requested",
            "unload_design_requested",
            "top_cell_changed",
            "layer_visibility_changed",
            "design_rotate_requested",
            "route_new_requested",
            "route_open_requested",
            "route_save_requested",
            "route_save_as_requested",
            "route_add_current_requested",
            "route_remove_selected_requested",
            "route_clear_requested",
            "route_selected",
            "route_offsets_changed",
            "route_edit_enabled_changed",
            "route_array_requested",
            "route_measurement_run_requested",
            "route_measurement_measure_requested",
            "route_measurement_stop_requested",
            "route_measurement_pause_requested",
            "route_measurement_interrupt_requested",
            "route_measurement_save_shift_requested",
            "route_measurement_confirmation_requested",
            "route_measurement_jump_requested",
            "route_measurement_move_requested",
            "move_to_target_requested",
            "next_target_requested",
            "previous_target_requested",
            "target_selected",
            "snap_enabled_changed",
        )
        for name in signal_names:
            setattr(self, name, _Signal())
        self.directories: list[Path] = []
        self.hover_snap_values: list[object] = []

    def set_design_dialog_directory(self, path: Path) -> None:
        self.directories.append(Path(path))

    def set_hover_snap(self, value: object) -> None:
        self.hover_snap_values.append(value)


class _FakeDesignLayoutWindow:
    instances: list["_FakeDesignLayoutWindow"] = []

    def __init__(self) -> None:
        self.navigator_panel = _FakeNavigatorPanel()
        self.calibration_point_selected = _Signal()
        self.move_requested = _Signal()
        self.route_point_requested = _Signal()
        self.hover_snap_changed = _Signal()
        self.visibility_changed = _Signal()
        self.show_count = 0
        self.__class__.instances.append(self)

    def show_and_raise(self) -> None:
        self.show_count += 1


class _FakeRouteDialog:
    def __init__(self) -> None:
        self.current_points: list[int] = []
        self.configuration = object()

    def set_current_point(self, point_number: int) -> None:
        self.current_points.append(int(point_number))

    def current_configuration(self) -> object:
        return self.configuration


class _DesignOwner:
    _NOOP_HANDLER_NAMES = (
        "_load_design_document",
        "_unload_design_document",
        "_set_design_top_cell",
        "_set_design_layer_visibility",
        "_rotate_design_document",
        "_create_measurement_route",
        "_load_measurement_route",
        "_save_measurement_route",
        "_save_measurement_route_as",
        "_add_current_design_route_point",
        "_remove_selected_route_point",
        "_clear_measurement_route_points",
        "_select_route_point",
        "_set_route_needle_offsets",
        "_set_route_edit_enabled",
        "_add_route_array_points",
        "_open_route_measurement_dialog",
        "_start_route_measurement",
        "_request_stop_route_measurement",
        "_request_pause_route_measurement",
        "_request_route_measurement_point_correction",
        "_save_route_measurement_shift",
        "_submit_route_measurement_confirmation",
        "_submit_route_measurement_jump",
        "_request_route_contact_move",
        "_move_to_design_target",
        "_select_next_design_target",
        "_select_previous_design_target",
        "_on_design_target_selected",
        "_on_design_snap_enabled_changed",
        "_on_design_layout_point_selected",
        "_add_design_route_point",
        "_on_design_layout_window_visibility_changed",
    )

    def __init__(self, settings_path: Path) -> None:
        self.settings_manager = _FakeSettingsManager(settings_path)
        self.design_layout_window = None
        self.design_navigator_panel = None
        self._design_layout_window_class = None
        self._design_layout_window_requested = True
        self._route_measurement_dialog = _FakeRouteDialog()
        self.calls: list[tuple[str, tuple[object, ...]]] = []
        for name in self._NOOP_HANDLER_NAMES:
            setattr(self, name, self._recording_handler(name))

    def _record(self, name: str, *args: object) -> None:
        self.calls.append((name, args))

    def _recording_handler(self, name: str) -> Any:
        def handler(*args: object, **kwargs: object) -> None:
            if kwargs:
                self._record(name, *args, kwargs)
                return
            self._record(name, *args)

        return handler

    def _refresh_design_panel(self) -> None:
        self._record("refresh_design_panel")

    def _collapse_alignment_panel_if_ready(self) -> None:
        self._record("collapse_alignment")

    def _move_to_design_window_point(self, x_value: float, y_value: float) -> None:
        self._record("move_to_design_window_point", x_value, y_value)


def test_create_design_layout_window_wires_route_controls_and_reuses_class(
    tmp_path: Path,
) -> None:
    _FakeDesignLayoutWindow.instances.clear()
    owner = _DesignOwner(tmp_path)

    create_design_layout_window(owner, _FakeDesignLayoutWindow)

    assert len(_FakeDesignLayoutWindow.instances) == 1
    window = _FakeDesignLayoutWindow.instances[0]
    panel = window.navigator_panel
    assert owner.design_layout_window is window
    assert owner.design_navigator_panel is panel
    assert owner._design_layout_window_class is _FakeDesignLayoutWindow
    assert panel.directories == [tmp_path / "designs"]
    assert window.show_count == 1
    assert ("refresh_design_panel", ()) in owner.calls
    assert ("collapse_alignment", ()) in owner.calls

    panel.route_measurement_pause_requested.emit()
    panel.route_measurement_interrupt_requested.emit()
    panel.route_measurement_confirmation_requested.emit("next")
    panel.route_measurement_move_requested.emit("point-7")
    panel.route_measurement_measure_requested.emit(7)
    window.move_requested.emit(1.25, -3.5)
    window.hover_snap_changed.emit("snap")

    assert ("_request_pause_route_measurement", ()) in owner.calls
    assert ("_request_route_measurement_point_correction", ()) in owner.calls
    assert ("_submit_route_measurement_confirmation", ("next",)) in owner.calls
    assert ("_request_route_contact_move", ("point-7",)) in owner.calls
    assert (
        "_open_route_measurement_dialog",
        ({"start_context": False},),
    ) in owner.calls
    assert owner._route_measurement_dialog.current_points == [7]
    assert (
        "_start_route_measurement",
        (owner._route_measurement_dialog.configuration,),
    ) in owner.calls
    assert ("move_to_design_window_point", (1.25, -3.5)) in owner.calls
    assert panel.hover_snap_values == ["snap"]
