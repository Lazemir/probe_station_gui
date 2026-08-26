from __future__ import annotations

# ruff: noqa: E402 -- Qt mode and import-reset must be configured before importing Main.

import os
import types
from dataclasses import replace
from pathlib import Path

import numpy as np

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from tests.app.import_reset import restore_real_imports_for_main

restore_real_imports_for_main()

import main as main_module
from main import Main
from probe_station_gui.application import design_load
from probe_station_gui.application.design_load import (
    _LoadedDesignDocument,
    _PendingDesignMarkupLoad,
)
from probe_station_gui.application.stage_motion_types import PlannedXYMoveRequest
from probe_station_gui.coordinates.coordinator_model import (
    CoordinateSystemSnapshot,
    CoordinateTransition,
)
from probe_station_gui.design.markup import MarkupDocument
from probe_station_gui.design.session import DesignSession
from probe_station_gui.design.session_state import export_persisted_session_state
from probe_station_gui.design.session_state import prepare_persisted_session_restore
from probe_station_gui.settings.axis_calibration_config import (
    AxisCalibrationSettings,
    default_axis_calibrations,
)
from probe_station_gui.stage.axis_calibration import StageAxisCalibrationMapper
from probe_station_gui.stage.machine_coordinates import MachineCoordinateSnapshot
from probe_station_gui.stage.types import _Status

DesignDocument = design_load.DesignDocument


class _FakeStageController:
    def __init__(self) -> None:
        self.homed_axes = {"X", "Y", "Z"}
        self.unhomed_requests: list[set[str]] = []
        self.move_requests: list[tuple[float, float]] = []
        self.move_accepted = True
        self.busy = False

    def mark_axes_unhomed(self, axes: set[str]) -> set[str]:
        normalized = {str(axis).strip().upper() for axis in axes}
        self.unhomed_requests.append(normalized)
        removed = self.homed_axes.intersection(normalized)
        self.homed_axes -= removed
        return set(removed)

    def is_busy(self) -> bool:
        return self.busy

    def request_move_to_xy(self, x_value: float, y_value: float) -> bool:
        self.move_requests.append((float(x_value), float(y_value)))
        return self.move_accepted


class _FakeStageMotion:
    def __init__(self) -> None:
        self.accepted = True
        self.planned_requests: list[PlannedXYMoveRequest] = []
        self.alignment_pending = False

    def request_planned_xy_move(self, request: PlannedXYMoveRequest) -> bool:
        self.planned_requests.append(request)
        return self.accepted

    def discard_alignment_rotation(self) -> bool:
        return False

    def alignment_rotation_pending(self) -> bool:
        return self.alignment_pending


class _FakeSettingsManager:
    def __init__(self) -> None:
        self.last_design_directory: Path | None = None

    def set_design_last_directory(self, path: Path) -> None:
        self.last_design_directory = Path(path)


class _FakeDesignPanel:
    def __init__(self) -> None:
        self.directories: list[Path] = []
        self.status_messages: list[str] = []

    def set_design_dialog_directory(self, path: Path) -> None:
        self.directories.append(Path(path))

    def set_status_message(self, message: str) -> None:
        self.status_messages.append(str(message))


class _FakeCell:
    def __init__(self) -> None:
        self.name = "TOP"

    def get_polygons(
        self, *args: object, **kwargs: object
    ) -> dict[tuple[int, int], list[np.ndarray]]:
        return {(1, 0): [np.asarray([[0.0, 0.0], [10.0, 0.0], [10.0, 10.0]])]}


class _FakeLibrary:
    unit = 1e-6
    precision = 1e-9

    def __init__(self) -> None:
        self.cells = [_FakeCell()]

    def top_level(self) -> list[_FakeCell]:
        return self.cells


def _make_document(tmp_path: Path) -> DesignDocument:
    tmp_path.mkdir(parents=True, exist_ok=True)
    design_path = tmp_path / "loaded.gds"
    design_path.write_bytes(b"loaded-design")
    return DesignDocument._from_components(
        path=design_path,
        library=_FakeLibrary(),
        top_cell_name="TOP",
    )


def _make_window() -> tuple[Main, _FakeStageController, list[str]]:
    window = Main.__new__(Main)
    stage_controller = _FakeStageController()
    statuses: list[str] = []
    window.stage_controller = stage_controller
    window._stage_motion = _FakeStageMotion()
    window._design_session = DesignSession()
    window._coordinate_system_coordinator = types.SimpleNamespace(
        current_design_lease=lambda: types.SimpleNamespace(
            document=window._design_session.document
        )
    )
    window._pending_persisted_design_state = None
    window._pending_persisted_design_position = None
    window._design_load_generation = 1
    window._design_load_restore_states = {}
    window._design_load_show_window = {}
    window._design_load_previous_sessions = {}
    window._design_load_previous_markup = {}
    window._design_markup = None
    window._design_markup_direct_guide_ids = []
    window._design_markup_store = None
    window._design_markup_request_id = 0
    window._design_markup_load_request_id = None
    window._design_load_pending = False
    window._design_markup_load_contexts = {}
    window._design_markup_pending_visibility = None
    window._pending_alignment_preparation = None
    window._last_selected_design_point = None
    window._design_snap_enabled = False
    window._current_design_stage_xy = None
    window.design_layout_window = None
    window.design_navigator_panel = None
    window.view = types.SimpleNamespace(
        set_design_minimap_data=lambda **_kwargs: None,
    )
    window.settings_manager = _FakeSettingsManager()
    window._show_status = lambda message, _timeout=0: statuses.append(message)
    window._reset_manual_alignment = lambda **_kwargs: statuses.append(
        "reset_alignment"
    )
    window._set_design_snap_enabled = lambda enabled: setattr(
        window, "_design_snap_enabled", bool(enabled)
    )
    window._refresh_design_panel = lambda: statuses.append("refresh_panel")
    window._refresh_design_position = lambda: statuses.append("refresh_position")
    window._restore_route_measurement_state_after_design_load = lambda: statuses.append(
        "restore_route"
    )

    def finish_markup_load(
        document: DesignDocument,
        *,
        generation: int,
        candidate_session: DesignSession,
        plan: object,
        show_window: bool,
        previous_markup: MarkupDocument | None,
    ) -> None:
        del previous_markup
        context = _PendingDesignMarkupLoad(
            generation=generation,
            session=candidate_session,
            plan=plan,
            show_window=show_window,
            previous_markup=None,
        )
        Main._commit_pending_design_load(
            window,
            context,
            MarkupDocument.empty(document.path),
        )
        statuses.append("load_markup")

    window._begin_design_markup_load = finish_markup_load
    window._raw_stage_xy_from_design_xy = lambda _design_xy: (1.5, -2.0)
    return window, stage_controller, statuses


def _activate_candidate_success(
    owner: object,
    *,
    session_state: object,
    workspace_after: object | None = None,
    **_kwargs: object,
) -> CoordinateTransition:
    assert session_state is not None
    if workspace_after is not None:
        design_load.design_workspace.apply_design_workspace_checkpoint(
            owner,
            workspace_after,
        )
    return CoordinateTransition(CoordinateSystemSnapshot(True, (), None))


def test_maybe_restore_persisted_design_clears_design_and_unhomes_xy_when_xy_changed(
    monkeypatch,
) -> None:
    window, stage_controller, statuses = _make_window()
    monkeypatch.setattr(
        design_load.design_workspace,
        "save_controller_state_without_design",
        lambda _owner: statuses.append("saved_without_design"),
    )
    window._pending_persisted_design_state = {
        "document_path": "C:\\designs\\sample.gds"
    }
    window._pending_persisted_design_position = (1.0, 2.0, 3.0)

    design_load.design_workspace.maybe_restore_persisted_design(
        window,
        (1.25, 2.5, 3.0),
    )

    assert stage_controller.unhomed_requests == [{"X", "Y"}]
    assert stage_controller.homed_axes == {"Z"}
    assert "saved_without_design" in statuses
    assert any("Controller X/Y coordinates changed" in item for item in statuses)


def test_on_design_document_loaded_error_saves_controller_state_without_design_when_restoring(
    monkeypatch,
) -> None:
    window, _stage_controller, statuses = _make_window()
    monkeypatch.setattr(
        design_load.design_workspace,
        "save_controller_state_without_design",
        lambda _owner: statuses.append("saved_without_design"),
    )
    window._design_load_restore_states[1] = {"document_path": "missing.gds"}
    window._design_load_show_window[1] = False

    Main._on_design_document_loaded(window, 1, None, RuntimeError("boom"))

    assert statuses == ["boom", "saved_without_design"]


def test_on_design_document_loaded_success_refreshes_persists_and_restores_route(
    monkeypatch,
    tmp_path: Path,
) -> None:
    window, _stage_controller, statuses = _make_window()
    monkeypatch.setattr(
        main_module.connection_flow,
        "persist_controller_state_if_available",
        lambda _owner: statuses.append("persisted"),
    )
    monkeypatch.setattr(
        main_module.coordinate_flow,
        "activate_current_design",
        _activate_candidate_success,
    )
    document = _make_document(tmp_path)
    panel = _FakeDesignPanel()
    window.design_navigator_panel = panel
    window._design_load_show_window[1] = False

    Main._on_design_document_loaded(window, 1, document, None)

    assert window._design_session.document is document
    assert window._design_snap_enabled
    assert statuses[:2] == ["reset_alignment", "refresh_panel"]
    assert "refresh_position" in statuses
    assert "persisted" in statuses
    assert "restore_route" in statuses
    assert "load_markup" in statuses
    assert any("Loaded design 'loaded.gds' (TOP)." == item for item in statuses)
    assert panel.directories == [tmp_path]
    assert window.settings_manager.last_design_directory == tmp_path


def _machine_snapshot(
    raw_machine: tuple[float, ...],
    *,
    work_offset: tuple[float, ...] | None = None,
    calibrations: dict[str, AxisCalibrationSettings] | None = None,
) -> MachineCoordinateSnapshot:
    offset = work_offset or tuple(0.0 for _ in raw_machine)
    work_position = tuple(
        raw_machine[index] - offset[index] for index in range(len(raw_machine))
    )
    axis_index = {"X": 0, "Y": 1, "Z": 2, "A": 3, "B": 4, "C": 5}
    mapper = StageAxisCalibrationMapper(
        calibrations=calibrations or default_axis_calibrations(),
        position_reporting_mode="work",
        active_work_coordinate_system="G54",
        controller_coordinate_offsets={"G54": offset},
        axis_index=axis_index,
    )
    return MachineCoordinateSnapshot.from_status(
        _Status(
            state="Idle",
            synchronized_machine_position=raw_machine,
            display_position=work_position,
            work_position=work_position,
            work_offset=offset,
            coordinate_system="G54",
        ),
        mapper,
        axis_index,
    )


def test_design_load_worker_carries_precomputed_frame_metadata(
    monkeypatch,
    tmp_path: Path,
) -> None:
    window, _stage_controller, _statuses = _make_window()
    document = _make_document(tmp_path)
    emitted: list[tuple[object, ...]] = []
    metadata = object()
    window.design_document_loaded = types.SimpleNamespace(
        emit=lambda *args: emitted.append(args)
    )
    monkeypatch.setattr(
        design_load.DesignDocument,
        "load",
        lambda _path: document,
    )
    monkeypatch.setattr(
        main_module.DesignFrameMetadata,
        "from_document",
        lambda _document: metadata,
    )
    monkeypatch.setattr(
        design_load.threading,
        "Thread",
        lambda **kwargs: types.SimpleNamespace(start=kwargs["target"]),
    )
    monkeypatch.setattr(
        design_load,
        "toggle_design_layout_window",
        lambda *_args: None,
    )
    window._coordinate_system_coordinator = types.SimpleNamespace(
        cancel_registration=lambda _reason: CoordinateTransition(
            CoordinateSystemSnapshot(False, (), None)
        )
    )
    monkeypatch.setattr(
        main_module.coordinate_flow,
        "apply_coordinate_transition",
        lambda *_args: None,
    )

    Main._start_design_document_load(
        window,
        str(document.path),
        restore_state=None,
        show_window=False,
    )

    payload = emitted[-1][1]
    assert payload.document is document
    assert payload.frame_metadata is metadata


def test_restored_top_cell_reconciles_worker_frame_metadata(tmp_path: Path) -> None:
    window, _stage_controller, _statuses = _make_window()
    top_document = replace(
        _make_document(tmp_path),
        file_backed=True,
        cell_names=("ALT", "TOP"),
        cell_bounds={
            "TOP": (0.0, 0.0, 10.0, 10.0),
            "ALT": (20.0, 20.0, 30.0, 30.0),
        },
    )
    alt_document = top_document.with_top_cell("ALT")
    restore_state = export_persisted_session_state(
        DesignSession(document=alt_document).snapshot_state()
    )
    assert restore_state is not None
    worker_metadata = main_module.DesignFrameMetadata.from_document(top_document)
    captured: list[tuple[DesignDocument, object]] = []
    window._design_load_restore_states[1] = restore_state
    window._design_load_show_window[1] = False
    window._begin_design_markup_load = lambda document, **kwargs: captured.append(
        (document, kwargs["frame_metadata"])
    )

    Main._on_design_document_loaded(
        window,
        1,
        _LoadedDesignDocument(
            alt_document,
            worker_metadata,
            prepare_persisted_session_restore(alt_document, restore_state),
        ),
        None,
    )

    assert captured[0][0].top_cell_name == "ALT"
    assert captured[0][1].top_cell_name == "ALT"
