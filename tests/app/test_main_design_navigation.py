from __future__ import annotations

import os
import types
from dataclasses import replace
from pathlib import Path

import numpy as np
import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from tests.app.import_reset import restore_real_imports_for_main


restore_real_imports_for_main(clear_probe_station_gui=True)

import main as main_module
from main import Main
from probe_station_gui.design.markup import MarkupDocument, MarkupLoadChoice
from probe_station_gui.design.markup_store import StoreLoadResult
from probe_station_gui.design.selection_model import (
    MixedArrayRequest,
    SelectionModel,
    markup_entity_id,
    route_entity_id,
)
from probe_station_gui.design.model import DesignRegistration
from probe_station_gui.design.klayout_types import StructureBoundsResult
from probe_station_gui.design.session import DesignSession
from probe_station_gui.coordinates.registry import CoordinateFrameRegistry
from probe_station_gui.coordinates.lifecycle import (
    CoordinateFrameLifecycle,
    DesignFrameUsabilitySnapshot,
    DesignUsabilityContext,
    FrameLifecycleEffects,
    FramePublication,
    FramePublicationResult,
)
from probe_station_gui.coordinates.provenance import (
    RUNTIME_PROVENANCE_REASON,
    RUNTIME_PROVENANCE_STATUS,
)
from probe_station_gui.design.frame_registration import (
    ContactReferenceToken,
    commit_xyb_registration,
    new_design_frame_draft,
    set_focus_reference,
)
from probe_station_gui.design.registration_lifecycle import (
    ContactOperationToken,
    DesignRegistrationLifecycle,
    FocusCompletion,
    FocusCompletionKind,
    RegistrationCancellation,
    RegistrationCaptureOutcome,
    RegistrationCaptureToken,
    RegistrationContext,
    RegistrationEffects,
)
from probe_station_gui.route.model import MeasurementRoute
from probe_station_gui.stage import position_update
from probe_station_gui.stage.axis_calibration import StageAxisCalibrationMapper
from probe_station_gui.stage.machine_coordinates import MachineCoordinateSnapshot
from probe_station_gui.stage.types import _Status
from probe_station_gui.settings.axis_calibration_config import (
    AxisCalibrationSettings,
    default_axis_calibrations,
)
from probe_station_gui.settings.software_coordinates import RotationPivotSettings

DesignDocument = main_module.DesignDocument


class _FakeStageController:
    def __init__(self) -> None:
        self.homed_axes = {"X", "Y", "Z"}
        self.unhomed_requests: list[set[str]] = []
        self.move_requests: list[tuple[float, float]] = []
        self.busy = False

    def mark_axes_unhomed(self, axes: set[str]) -> set[str]:
        normalized = {str(axis).strip().upper() for axis in axes}
        self.unhomed_requests.append(normalized)
        removed = self.homed_axes.intersection(normalized)
        self.homed_axes -= removed
        return set(removed)

    def is_busy(self) -> bool:
        return self.busy

    def request_move_to_xy(self, x_value: float, y_value: float) -> None:
        self.move_requests.append((float(x_value), float(y_value)))


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

    def get_polygons(self, *args: object, **kwargs: object) -> dict[tuple[int, int], list[np.ndarray]]:
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


def _lifecycle_context(
    *,
    objective: str = "X5",
    frame_id: str = "design-frame",
    frame_version: int = 1,
    z_ready: bool = False,
    a_ready: bool = False,
) -> RegistrationContext:
    return RegistrationContext(
        session_identity=17,
        source_identity=("C:/designs/device.gds", "load-1"),
        top_cell_name="TOP",
        visible_layers=((1, 0),),
        rotation_quarter_turns=0,
        frame_id=frame_id,
        frame_version=frame_version,
        fov_size=(20.0, 20.0),
        objective_name=objective,
        optical_calibration_identity=f"{objective}-calibration",
        xyb_ready=True,
        z_ready=z_ready,
        a_ready=a_ready,
    )


def _pending_focus_operation(
    lifecycle: DesignRegistrationLifecycle,
    context: RegistrationContext,
    *,
    target_xy: tuple[float, float] = (4.0, 5.0),
    move_completed: bool = False,
):
    lifecycle.set_focus_candidate(
        types.SimpleNamespace(center=(5.0, 5.0)),
        context,
    )
    started = lifecycle.accept_focus(context)
    token = started.focus_token
    assert token is not None
    bound = lifecycle.accept_focus(
        context,
        FocusCompletion(
            kind=FocusCompletionKind.TARGET_BOUND,
            token=token,
            target_xy=target_xy,
        ),
    )
    assert bound.accepted is True
    if move_completed:
        moved = lifecycle.accept_focus(
            context,
            FocusCompletion(
                kind=FocusCompletionKind.MOVE_FINISHED,
                token=token,
                target_xy=target_xy,
                succeeded=True,
            ),
        )
        assert moved.start_autofocus is True
    return token


def _make_window() -> tuple[Main, _FakeStageController, list[str]]:
    window = Main.__new__(Main)
    window._design_registration_lifecycle = DesignRegistrationLifecycle()
    stage_controller = _FakeStageController()
    statuses: list[str] = []
    window.stage_controller = stage_controller
    window._design_session = DesignSession()
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
    window._reset_manual_alignment = lambda **_kwargs: statuses.append("reset_alignment")
    window._set_design_snap_enabled = lambda enabled: setattr(window, "_design_snap_enabled", bool(enabled))
    window._refresh_design_panel = lambda: statuses.append("refresh_panel")
    window._refresh_design_position = lambda: statuses.append("refresh_position")
    window._restore_route_measurement_state_after_design_load = lambda: statuses.append("restore_route")

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
        context = main_module._PendingDesignMarkupLoad(
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
    window._clear_planned_move_prediction = lambda **_kwargs: statuses.append("clear_prediction")
    return window, stage_controller, statuses


def test_maybe_restore_persisted_design_clears_design_and_unhomes_xy_when_xy_changed(
    monkeypatch,
) -> None:
    window, stage_controller, statuses = _make_window()
    monkeypatch.setattr(
        main_module.connection_flow,
        "save_controller_state_without_design",
        lambda _owner: statuses.append("saved_without_design"),
    )
    window._pending_persisted_design_state = {"document_path": "C:\\designs\\sample.gds"}
    window._pending_persisted_design_position = (1.0, 2.0, 3.0)

    main_module.connection_flow.maybe_restore_persisted_design(
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
        main_module.connection_flow,
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


def _set_rotation_settings(
    window: object,
    pivot: tuple[float, float] = (0.0, 0.0),
) -> None:
    if getattr(window, "_design_registration_lifecycle", None) is None:
        window._design_registration_lifecycle = DesignRegistrationLifecycle()
    window.settings_manager = types.SimpleNamespace(
        settings=types.SimpleNamespace(
            software_coordinates=types.SimpleNamespace(
                pivot=RotationPivotSettings(x_mm=pivot[0], y_mm=pivot[1])
            )
        )
    )
    window._active_objective_xy_offset = lambda: (0.0, 0.0)


def _record_legacy_work_provenance(
    session: DesignSession,
    work_offset: tuple[float, ...],
) -> None:
    session.record_legacy_stage_coordinate_provenance(
        {
            "position_reporting_mode": "work",
            "coordinate_system": "G54",
            "work_offset": list(work_offset),
        }
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
        main_module.DesignDocument,
        "load",
        lambda _path: document,
    )
    monkeypatch.setattr(
        main_module.DesignFrameMetadata,
        "from_document",
        lambda _document: metadata,
    )
    monkeypatch.setattr(
        main_module.threading,
        "Thread",
        lambda **kwargs: types.SimpleNamespace(start=kwargs["target"]),
    )
    monkeypatch.setattr(
        main_module,
        "toggle_design_layout_window",
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
    restore_state = DesignSession(document=alt_document).export_persisted_state()
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
        main_module._LoadedDesignDocument(top_document, worker_metadata),
        None,
    )

    assert captured[0][0].top_cell_name == "ALT"
    assert captured[0][1].top_cell_name == "ALT"


def test_legacy_migration_without_synchronized_provenance_preserves_legacy_payload(
    tmp_path: Path,
) -> None:
    document = _make_document(tmp_path)
    session = DesignSession(document=document)
    session.source_design_marks = ((0.0, 0.0), (1000.0, 0.0))
    session.source_stage_marks = ((3.0, 4.0), (4.0, 4.0))
    persisted_before = session.export_persisted_state()
    registry = CoordinateFrameRegistry()
    statuses: list[str] = []
    window = Main.__new__(Main)
    window._design_session = session
    window._coordinate_frame_registry = registry
    window._design_registration_lifecycle = DesignRegistrationLifecycle()
    _set_rotation_settings(window)
    window._coordinate_frames_loaded = True
    window._active_design_frame_metadata = main_module.DesignFrameMetadata.from_document(
        document
    )
    window.stage_controller = types.SimpleNamespace(
        latest_machine_coordinate_snapshot=lambda: None,
    )
    window._show_status = lambda message, _timeout: statuses.append(message)

    Main._activate_loaded_design_frame(window)

    assert registry.snapshot().records == ()
    assert session.export_persisted_state() == persisted_before
    assert statuses == []


def test_legacy_migration_waiting_for_b_is_runtime_invalid_but_persisted(
    tmp_path: Path,
) -> None:
    document = _make_document(tmp_path)
    session = DesignSession(document=document)
    session.source_design_marks = ((0.0, 0.0), (1000.0, 0.0))
    session.source_stage_marks = ((3.0, 4.0), (4.0, 4.0))
    session._rebuild_registration()
    _record_legacy_work_provenance(session, (0.0, 0.0, 0.0, 0.0, 0.0))
    assert session.registration is not None and session.registration.valid
    persisted_before = session.export_persisted_state()
    registry = CoordinateFrameRegistry()
    window = Main.__new__(Main)
    window._design_session = session
    window._coordinate_frame_registry = registry
    _set_rotation_settings(window)
    window._coordinate_frames_loaded = True
    window._active_design_frame_metadata = main_module.DesignFrameMetadata.from_document(
        document
    )
    window.stage_controller = types.SimpleNamespace(
        latest_machine_coordinate_snapshot=lambda: None
    )

    Main._activate_loaded_design_frame(window)

    assert session.active_frame_id is None
    assert session.registration is not None
    assert session.registration.valid is False
    assert session.registration_status == (
        "Design registration requires a current B position."
    )
    assert session.source_design_marks == ((0.0, 0.0), (1000.0, 0.0))
    assert session.source_stage_marks == ((3.0, 4.0), (4.0, 4.0))
    assert session.export_persisted_state() == persisted_before
    assert registry.snapshot().records == ()


def test_first_fresh_b_status_retries_legacy_migration_once(
    monkeypatch,
    tmp_path: Path,
) -> None:
    document = _make_document(tmp_path)
    session = DesignSession(document=document)
    session.source_design_marks = ((0.0, 0.0), (1000.0, 0.0))
    session.source_stage_marks = ((3.0, 4.0), (4.0, 4.0))
    _record_legacy_work_provenance(session, (0.0, 0.0, 0.0, 0.0, 0.0))
    session._rebuild_registration()
    registry = CoordinateFrameRegistry()
    snapshot: dict[str, MachineCoordinateSnapshot | None] = {"value": None}
    publish_calls: list[bool] = []
    window = Main.__new__(Main)
    window._design_session = session
    window._coordinate_frame_registry = registry
    window._coordinate_frames_loaded = True
    window._active_design_frame_metadata = main_module.DesignFrameMetadata.from_document(
        document
    )
    window._last_reported_b_position = None
    window._raw_stage_xy_from_camera_stage_xy = lambda point: point
    window._design_navigation_xy_from_physical_machine_xy = lambda point: point
    window._show_status = lambda *_args: None
    _set_rotation_settings(window)
    window.stage_controller = types.SimpleNamespace(
        latest_machine_coordinate_snapshot=lambda: snapshot["value"],
        axes_are_homed=lambda axes: axes.issubset({"X", "Y"}),
    )
    monkeypatch.setattr(
        main_module.connection_flow,
        "publish_coordinate_frames",
        lambda _owner, *, legacy_migration=False: publish_calls.append(
            bool(legacy_migration)
        ),
    )

    Main._activate_loaded_design_frame(window)
    assert session.active_frame_id is None

    fresh_position = (0.0, 0.0, 0.0, 0.0, 12.0)
    snapshot["value"] = _machine_snapshot(fresh_position)
    signal_plan = types.SimpleNamespace(
        b_axis=types.SimpleNamespace(current_b=12.0),
    )
    position_update._apply_b_axis_registration(
        window,
        signal_plan,
        fresh_position,
    )
    position_update._apply_b_axis_registration(
        window,
        signal_plan,
        fresh_position,
    )

    assert session.active_frame_id is not None
    assert session.registration is not None and session.registration.valid
    assert len(registry.snapshot().records) == 1
    assert registry.snapshot().generation == 1
    assert publish_calls == [True]


def test_legacy_migration_uses_verified_capture_wco_not_changed_current_wco(
    tmp_path: Path,
) -> None:
    document = _make_document(tmp_path)
    window = Main.__new__(Main)
    window._design_session = DesignSession(document=document)
    window._raw_stage_xy_from_camera_stage_xy = lambda point: (
        point[0] + 100.0,
        point[1] - 200.0,
    )
    calibrations = default_axis_calibrations()
    calibrations["X"] = AxisCalibrationSettings(
        enabled=True,
        controller_points=[0.0, 10.0, 20.0],
        physical_points=[0.0, 12.0, 30.0],
    )
    calibrations["Y"] = AxisCalibrationSettings(
        enabled=True,
        controller_points=[0.0, 10.0, 20.0],
        physical_points=[0.0, 8.0, 25.0],
    )
    snapshot = _machine_snapshot(
        (15.0, 16.0, 0.0, 0.0, 0.0),
        work_offset=(4.0, 5.0, 0.0, 0.0, 0.0),
        calibrations=calibrations,
    )
    window.stage_controller = types.SimpleNamespace(
        latest_machine_coordinate_snapshot=lambda: snapshot,
    )
    legacy = {
        "source_design_marks": [[0.0, 0.0], [1000.0, 0.0]],
        "source_stage_marks": [[-97.0, 204.0], [-96.0, 204.0]],
        "check_design_marks": [[500.0, 0.0]],
        "check_stage_marks": [[-96.5, 204.0]],
        "stage_coordinate_provenance": {
            "position_reporting_mode": "work",
            "coordinate_system": "G54",
            "work_offset": [1.0, 2.0, 0.0, 0.0, 0.0],
        },
    }

    converted = Main._legacy_design_state_for_frame_migration(window, legacy)

    assert np.asarray(converted["source_stage_marks"]) == pytest.approx(
        np.asarray([[4.8, 4.8], [6.0, 4.8]])
    )
    assert np.asarray(converted["check_stage_marks"]) == pytest.approx(
        np.asarray([[5.4, 4.8]])
    )


@pytest.mark.parametrize(
    "capture_provenance",
    [
        None,
        {
            "position_reporting_mode": "work",
            "coordinate_system": "G54",
            "work_offset": [[]],
        },
    ],
    ids=("absent", "malformed"),
)
def test_legacy_migration_without_verified_capture_wco_blocks_and_preserves_payload(
    capture_provenance: object,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    document = _make_document(tmp_path)
    session = DesignSession(document=document)
    session.source_design_marks = ((0.0, 0.0), (1000.0, 0.0))
    session.source_stage_marks = ((3.0, 4.0), (4.0, 4.0))
    if capture_provenance is not None:
        session._legacy_stage_coordinate_provenance = capture_provenance
    expected_payload = {
        **session.export_persisted_state(),
        **(
            {}
            if capture_provenance is None
            else {"stage_coordinate_provenance": capture_provenance}
        ),
    }
    registry = CoordinateFrameRegistry()
    statuses: list[str] = []
    window = Main.__new__(Main)
    window._design_session = session
    window._coordinate_frame_registry = registry
    window._coordinate_frames_loaded = True
    window._active_design_frame_metadata = main_module.DesignFrameMetadata.from_document(
        document
    )
    window._raw_stage_xy_from_camera_stage_xy = lambda point: point
    window._design_navigation_xy_from_physical_machine_xy = lambda point: point
    window.stage_controller = types.SimpleNamespace(
        latest_machine_coordinate_snapshot=lambda: _machine_snapshot(
            (13.0, 24.0, 0.0, 0.0, 5.0),
            work_offset=(10.0, 20.0, 0.0, 0.0, 0.0),
        ),
        axes_are_homed=lambda axes: axes.issubset({"X", "Y"}),
    )
    window._show_status = lambda message, _timeout: statuses.append(str(message))
    _set_rotation_settings(window)
    monkeypatch.setattr(
        main_module.connection_flow,
        "publish_coordinate_frames",
        lambda _owner, *, legacy_migration=False: None,
    )

    Main._activate_loaded_design_frame(window)

    assert registry.snapshot().records == ()
    assert session.active_frame_id is None
    assert session.stage_from_design((500.0, 0.0)) is None
    assert "capture-time" in session.registration_status.lower()
    assert session.export_persisted_state() == expected_payload
    assert statuses and "capture-time" in statuses[-1].lower()


def test_stage_mark_capture_commits_active_frame_without_motion(
    monkeypatch,
    tmp_path: Path,
) -> None:
    base_document = _make_document(tmp_path)
    document = base_document.with_rotation_delta(1)
    canonical_design_marks = ((0.0, 0.0), (1000.0, 0.0))
    registry = CoordinateFrameRegistry()
    draft = registry.add(new_design_frame_draft(document, existing_names=()))
    session = DesignSession(document=document)
    session.link_active_frame(draft)
    session.source_design_marks = tuple(
        base_document.rotate_point(point, 1) for point in canonical_design_marks
    )
    snapshots = iter(
        (
            _machine_snapshot(
                (13.0, 24.0, 0.0, 0.0, 42.0),
                work_offset=(10.0, 20.0, 0.0, 0.0, 30.0),
            ),
            _machine_snapshot(
                (14.0, 24.0, 0.0, 0.0, 42.0),
                work_offset=(10.0, 20.0, 0.0, 0.0, 30.0),
            ),
        )
    )
    requests: list[tuple[object, tuple[str, ...]]] = []
    events: list[object] = []
    window = Main.__new__(Main)
    window._design_registration_lifecycle = DesignRegistrationLifecycle()
    window._design_session = session
    window._coordinate_frame_registry = registry
    window.settings_manager = types.SimpleNamespace(
        settings=types.SimpleNamespace(
            software_coordinates=types.SimpleNamespace(
                pivot=RotationPivotSettings()
            )
        )
    )
    window.stage_controller = types.SimpleNamespace(
        request_machine_coordinate_snapshot=lambda token, *, axes: (
            requests.append((token, tuple(axes))) or True
        ),
        current_stage_position=lambda: pytest.fail("synchronous GUI hardware read"),
    )
    window._camera_stage_xy_from_raw_stage_xy = lambda point: (
        point[0] - 100.0,
        point[1] + 200.0,
    )
    window._refresh_design_panel = lambda: None
    window._refresh_design_position = lambda: None
    statuses: list[str] = []
    window._show_status = lambda message, _timeout=0: statuses.append(str(message))
    monkeypatch.setattr(
        main_module.connection_flow,
        "publish_coordinate_frames",
        lambda _owner: events.append(("publish",)),
    )

    Main._capture_stage_source_mark(window)
    first_token, first_axes = requests.pop()
    assert first_axes == ("X", "Y", "B")
    first_snapshot = next(snapshots)
    window.stage_controller.latest_machine_coordinate_snapshot = lambda: first_snapshot
    Main._on_registration_machine_coordinate_snapshot_finished(
        window,
        first_token,
        True,
        first_snapshot,
        "",
    )
    Main._capture_stage_source_mark(window)
    second_token, _second_axes = requests.pop()
    second_snapshot = next(snapshots)
    window.stage_controller.latest_machine_coordinate_snapshot = lambda: second_snapshot
    Main._on_registration_machine_coordinate_snapshot_finished(
        window,
        second_token,
        True,
        second_snapshot,
        "",
    )

    committed = registry.get(draft.frame_id)
    assert committed is not None
    assert all(committed.readiness[axis].available for axis in ("X", "Y", "B"))
    assert not committed.readiness["Z"].available
    metadata = main_module.DesignFrameMetadata.from_mapping(committed.metadata)
    assert metadata.source_design_marks == canonical_design_marks
    assert metadata.source_machine_marks == ((13.0, 24.0), (14.0, 24.0))
    assert committed.transform.reference_b_deg == 42.0
    assert session.source_stage_marks_compact() == [(-97.0, 204.0), (-96.0, 204.0)]
    assert events == [("publish",)]
    assert isinstance(window._design_registration_lifecycle, DesignRegistrationLifecycle)
    assert not hasattr(window, "_pending_registration_physical_marks")
    assert not hasattr(window, "_pending_registration_mark_capture")
    assert "captured" in statuses[-1].lower()


def test_stage_mark_capture_normalizes_mixed_b_samples_to_first_context(
    monkeypatch,
    tmp_path: Path,
) -> None:
    document = _make_document(tmp_path)
    registry = CoordinateFrameRegistry()
    draft = registry.add(new_design_frame_draft(document, existing_names=()))
    session = DesignSession(document=document)
    session.link_active_frame(draft)
    session.source_design_marks = ((0.0, 0.0), (1000.0, 0.0))
    requests: list[object] = []
    window = Main.__new__(Main)
    window._design_session = session
    window._coordinate_frame_registry = registry
    window.stage_controller = types.SimpleNamespace(
        request_machine_coordinate_snapshot=lambda token, *, axes: (
            requests.append(token) or True
        )
    )
    _set_rotation_settings(window)
    window._camera_stage_xy_from_raw_stage_xy = lambda point: point
    window._design_navigation_xy_from_physical_machine_xy = lambda point: point
    window._refresh_design_panel = lambda: None
    window._refresh_design_position = lambda: None
    window._show_status = lambda *_args: None
    monkeypatch.setattr(
        main_module.connection_flow,
        "publish_coordinate_frames",
        lambda _owner: None,
    )

    Main._capture_stage_source_mark(window)
    first = _machine_snapshot((10.0, 0.0, 0.0, 0.0, 0.0))
    Main._on_registration_machine_coordinate_snapshot_finished(
        window,
        requests.pop(),
        True,
        first,
        "",
    )
    assert session.source_stage_marks_compact() == [(10.0, 0.0)]
    assert isinstance(window._design_registration_lifecycle, DesignRegistrationLifecycle)
    assert not hasattr(window, "_pending_registration_physical_marks")
    assert not hasattr(window, "_pending_registration_mark_capture")

    Main._capture_stage_source_mark(window)
    second = _machine_snapshot((0.0, 11.0, 0.0, 0.0, 90.0))
    Main._on_registration_machine_coordinate_snapshot_finished(
        window,
        requests.pop(),
        True,
        second,
        "",
    )

    committed = registry.get(draft.frame_id)
    assert committed is not None and committed.transform is not None
    metadata = main_module.DesignFrameMetadata.from_mapping(committed.metadata)
    assert np.allclose(metadata.source_machine_marks, ((10.0, 0.0), (11.0, 0.0)))
    assert committed.transform.reference_b_deg == pytest.approx(0.0)


def test_operator_align_normalizes_physical_marks_captured_at_different_b_and_pivots(
    monkeypatch,
    tmp_path: Path,
) -> None:
    document = _make_document(tmp_path)
    registry = CoordinateFrameRegistry()
    draft = registry.add(new_design_frame_draft(document, existing_names=()))
    session = DesignSession(document=document)
    session.link_active_frame(draft)
    requests: list[tuple[object, tuple[str, ...]]] = []
    rotations: list[float] = []
    publishes: list[object] = []
    statuses: list[str] = []
    manual_ui_states: list[
        tuple[int | None, tuple[tuple[float, float] | None, ...]]
    ] = []
    apply_states: list[
        tuple[int | None, tuple[tuple[float, float] | None, ...]]
    ] = []
    window = Main.__new__(Main)
    window._design_session = session
    window._coordinate_frame_registry = registry
    window._coordinate_frames_loaded = True
    window._active_design_frame_metadata = main_module.DesignFrameMetadata.from_document(
        document
    )
    window._manual_alignment_pick_slot = None
    window._manual_alignment_capture_context = None
    window._manual_alignment_points = [None, None]
    window._pending_alignment_preparation = None
    window._pending_quick_alignment_rotation = False
    window._last_selected_design_point = None
    window._design_snap_enabled = True
    window.alignment_panel = None
    window.alignment_dock = None
    window.design_layout_window = None
    window.stage_controller = types.SimpleNamespace(
        request_machine_coordinate_snapshot=lambda token, *, axes: (
            requests.append((token, tuple(axes))) or True
        ),
        current_stage_position=lambda: pytest.fail(
            "operator Align performed a synchronous GUI-thread position read"
        ),
        latest_machine_coordinate_snapshot=lambda: None,
        request_rotate_b=lambda angle: rotations.append(float(angle)),
    )
    _set_rotation_settings(window)
    current_pivot = [(0.0, 0.0)]
    window._rotation_geometry_snapshot = lambda: types.SimpleNamespace(
        pivot_machine_xy=current_pivot[0]
    )
    window._camera_stage_xy_from_raw_stage_xy = lambda point: point
    window._design_navigation_xy_from_physical_machine_xy = lambda point: point
    window._set_alignment_panel_expanded = lambda: None
    window._set_design_snap_enabled = lambda _enabled: None
    window._refresh_manual_alignment_ui = lambda: manual_ui_states.append(
        (
            window._manual_alignment_pick_slot,
            tuple(window._alignment_stage_draft),
        )
    )
    window._update_stage_coordinate_apply_state = lambda: apply_states.append(
        (
            window._manual_alignment_pick_slot,
            tuple(window._alignment_stage_draft),
        )
    )
    window._refresh_design_panel = lambda: None
    window._refresh_design_position = lambda: None
    window._update_coordinate_display = lambda **_kwargs: None
    window._show_status = lambda message, _timeout=0: statuses.append(str(message))
    monkeypatch.setattr(
        main_module.connection_flow,
        "publish_coordinate_frames",
        lambda owner, **_kwargs: publishes.append(owner),
    )

    Main._on_alignment_draft_accepted(
        window,
        ((0.0, 0.0), (1000.0, 0.0)),
    )
    Main._request_alignment_capture(window, 0, "image")
    Main._capture_manual_alignment_point(
        window,
        0,
        (10.0, 20.0),
        source="image",
    )
    first_request, first_axes = requests.pop(0)
    assert isinstance(first_request, RegistrationCaptureToken)
    assert first_request.mark_index == 0
    assert first_request.capture_source == "image"
    assert first_request.configured_target_xy == (10.0, 20.0)
    assert first_request.pivot_machine_xy == (0.0, 0.0)
    assert first_axes == ("X", "Y", "B")
    assert window._manual_alignment_pick_slot == 0
    Main._arm_manual_alignment_pick(window, 1)
    assert window._manual_alignment_pick_slot == 1
    manual_ui_states.clear()
    apply_states.clear()
    first_snapshot = _machine_snapshot((10.0, 20.0, 0.0, 0.0, 0.0))
    Main._on_registration_machine_coordinate_snapshot_finished(
        window,
        first_request,
        True,
        first_snapshot,
        "",
    )
    assert window._manual_alignment_pick_slot == 1
    assert window._alignment_stage_draft == [(10.0, 20.0), None]
    assert manual_ui_states == []
    assert apply_states == []

    current_pivot[0] = (10.0, 20.0)
    Main._capture_manual_alignment_center_shortcut(window)
    second_request, second_axes = requests.pop(0)
    assert isinstance(second_request, RegistrationCaptureToken)
    assert second_request.mark_index == 1
    assert second_request.capture_source == "center"
    assert second_request.configured_target_xy is None
    assert second_request.pivot_machine_xy == (10.0, 20.0)
    assert second_axes == ("X", "Y", "B")
    # At B=90 around the captured pivot, (9, 20) normalizes to (10, 21)
    # in the first sample's B=0 reference plane.
    second_snapshot = _machine_snapshot((9.0, 20.0, 0.0, 0.0, 90.0))
    window.stage_controller.latest_machine_coordinate_snapshot = (
        lambda: second_snapshot
    )
    manual_ui_states.clear()
    apply_states.clear()
    Main._on_registration_machine_coordinate_snapshot_finished(
        window,
        second_request,
        True,
        second_snapshot,
        "",
    )
    assert window._manual_alignment_pick_slot is None
    assert manual_ui_states == [
        (None, ((10.0, 20.0), (9.0, 20.0)))
    ]
    assert apply_states == [
        (None, ((10.0, 20.0), (9.0, 20.0)))
    ]

    committed = registry.get(draft.frame_id)
    assert committed is not None
    assert committed.version == draft.version + 1
    assert all(committed.readiness[axis].available for axis in ("X", "Y", "B"))
    metadata = main_module.DesignFrameMetadata.from_mapping(committed.metadata)
    assert metadata.source_design_marks == ((0.0, 0.0), (1000.0, 0.0))
    assert metadata.source_machine_marks == ((10.0, 20.0), (10.0, 21.0))
    assert committed.transform is not None
    assert committed.transform.reference_b_deg == pytest.approx(0.0)
    assert session.active_frame_id == committed.frame_id
    assert session.registration is not None and session.registration.valid
    assert publishes == [window]
    assert rotations == []
    assert "complete" in statuses[-1].lower()
    assert not hasattr(window, "_pending_operator_alignment_capture")
    assert not hasattr(window, "_alignment_physical_draft")
    assert not hasattr(window, "_alignment_operation_id")


def test_operator_alignment_fit_failure_restores_exact_capture_baseline(
    monkeypatch,
    tmp_path: Path,
) -> None:
    document = _make_document(tmp_path)
    registry = CoordinateFrameRegistry()
    draft = registry.add(new_design_frame_draft(document, existing_names=()))
    session = DesignSession(document=document)
    session.link_active_frame(draft)
    requests: list[object] = []
    statuses: list[str] = []
    window = Main.__new__(Main)
    window._design_session = session
    window._coordinate_frame_registry = registry
    window._coordinate_frames_loaded = True
    window._active_design_frame_metadata = main_module.DesignFrameMetadata.from_document(
        document
    )
    window._manual_alignment_pick_slot = None
    window._manual_alignment_capture_context = None
    window._manual_alignment_points = [None, None]
    window._pending_alignment_preparation = None
    window._pending_quick_alignment_rotation = False
    window._last_selected_design_point = None
    window._design_snap_enabled = True
    window.alignment_panel = None
    window.alignment_dock = None
    window.design_layout_window = None
    window.stage_controller = types.SimpleNamespace(
        request_machine_coordinate_snapshot=lambda token, *, axes: (
            requests.append(token) or True
        ),
        latest_machine_coordinate_snapshot=lambda: None,
    )
    _set_rotation_settings(window)
    window._camera_stage_xy_from_raw_stage_xy = lambda point: point
    window._design_navigation_xy_from_physical_machine_xy = lambda point: point
    window._set_alignment_panel_expanded = lambda: None
    window._refresh_manual_alignment_ui = lambda: None
    window._update_stage_coordinate_apply_state = lambda: None
    window._refresh_design_panel = lambda: None
    window._refresh_design_position = lambda: None
    window._update_coordinate_display = lambda **_kwargs: None
    window._show_status = lambda message, _timeout=0: statuses.append(str(message))
    monkeypatch.setattr(
        main_module,
        "commit_xyb_registration",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            main_module.DesignModelError("alignment fit failed")
        ),
    )

    Main._on_alignment_draft_accepted(
        window,
        ((0.0, 0.0), (1000.0, 0.0)),
    )
    for slot, point in enumerate(((1.0, 2.0), (2.0, 2.0))):
        Main._request_alignment_capture(window, slot, "center")
        Main._on_registration_machine_coordinate_snapshot_finished(
            window,
            requests.pop(0),
            True,
            _machine_snapshot((point[0], point[1], 0.0, 0.0, 5.0)),
            "",
        )

    assert registry.get(draft.frame_id) == draft
    assert window._alignment_stage_draft == [None, None]
    assert session.source_stage_marks_compact() == []
    assert statuses[-1] == "alignment fit failed"


def test_legacy_stage_mark_capture_persists_its_wco_provenance(tmp_path: Path) -> None:
    document = _make_document(tmp_path)
    session = DesignSession(document=document)
    session.source_design_marks = ((0.0, 0.0),)
    requests: list[object] = []
    snapshot = _machine_snapshot(
        (13.0, 24.0, 0.0, 0.0, 5.0),
        work_offset=(10.0, 20.0, 0.0, 0.0, 0.0),
    )
    window = Main.__new__(Main)
    window._design_session = session
    window._coordinate_frame_registry = CoordinateFrameRegistry()
    _set_rotation_settings(window)
    window.stage_controller = types.SimpleNamespace(
        request_machine_coordinate_snapshot=lambda token, *, axes: (
            requests.append(token) or True
        ),
    )
    window._camera_stage_xy_from_raw_stage_xy = lambda point: point
    window._refresh_design_panel = lambda: None
    window._refresh_design_position = lambda: None
    window._show_status = lambda *_args: None

    Main._capture_stage_source_mark(window)
    Main._on_registration_machine_coordinate_snapshot_finished(
        window,
        requests.pop(),
        True,
        snapshot,
        "",
    )

    persisted = session.export_persisted_state()
    assert persisted is not None
    assert persisted["stage_coordinate_provenance"] == {
        "position_reporting_mode": "work",
        "coordinate_system": "G54",
        "work_offset": [10.0, 20.0, 0.0, 0.0, 0.0],
    }


def test_legacy_stage_mark_captures_under_different_wcos_fail_closed(
    tmp_path: Path,
) -> None:
    session = DesignSession(document=_make_document(tmp_path))
    first = {
        "position_reporting_mode": "work",
        "coordinate_system": "G54",
        "work_offset": [1.0, 2.0, 0.0, 0.0, 0.0],
    }
    second = {
        "position_reporting_mode": "work",
        "coordinate_system": "G54",
        "work_offset": [4.0, 5.0, 0.0, 0.0, 0.0],
    }

    session.record_legacy_stage_coordinate_provenance(first)
    session.add_source_stage_mark((3.0, 4.0))
    session.record_legacy_stage_coordinate_provenance(second)

    persisted = session.export_persisted_state()
    assert persisted is not None
    assert persisted["stage_coordinate_provenance"] == {
        "conflicting_capture_provenance": [first, second]
    }


def _registration_failure_window(
    tmp_path: Path,
    *,
    submission_accepted: bool,
) -> tuple[
    Main,
    DesignSession,
    DesignRegistrationLifecycle,
    list[RegistrationCaptureToken],
    list[RegistrationEffects],
    list[str],
    list[tuple[str, object]],
]:
    document = _make_document(tmp_path)
    registry = CoordinateFrameRegistry()
    draft = registry.add(new_design_frame_draft(document, existing_names=()))
    session = DesignSession(document=document)
    session.link_active_frame(draft)
    session.source_design_marks = ((0.0, 0.0),)
    session.source_stage_marks = ((9.0, 10.0),)
    session.registration_status = "Baseline registration."
    lifecycle = DesignRegistrationLifecycle()
    requests: list[RegistrationCaptureToken] = []
    applied: list[RegistrationEffects] = []
    statuses: list[str] = []
    overlay_events: list[tuple[str, object]] = []
    window = Main.__new__(Main)
    window._design_session = session
    window._coordinate_frame_registry = registry
    window._design_registration_lifecycle = lifecycle
    _set_rotation_settings(window)

    def submit(token: RegistrationCaptureToken, *, axes: tuple[str, ...]) -> bool:
        assert axes == ("X", "Y", "B")
        requests.append(token)
        return submission_accepted

    window.stage_controller = types.SimpleNamespace(
        request_machine_coordinate_snapshot=submit
    )
    window._camera_stage_xy_from_raw_stage_xy = lambda point: point
    window._refresh_design_panel = lambda: None
    window._refresh_design_position = lambda: None
    window._show_status = lambda message, _timeout=0: statuses.append(str(message))
    window.design_layout_window = types.SimpleNamespace(
        set_focus_candidate=lambda value: overlay_events.append(("candidate", value)),
        set_selected_focus_point=lambda value: overlay_events.append(("point", value)),
    )

    def apply(effects: RegistrationEffects) -> None:
        applied.append(effects)
        Main._apply_registration_effects(window, effects)

    window._apply_registration_effects = apply
    return window, session, lifecycle, requests, applied, statuses, overlay_events


def test_rejected_stage_snapshot_submission_applies_rejection_once_and_closes_request(
    tmp_path: Path,
) -> None:
    window, session, lifecycle, requests, applied, statuses, overlay = (
        _registration_failure_window(tmp_path, submission_accepted=False)
    )
    baseline = (
        session.source_stage_marks,
        session.registration,
        session.registration_status,
    )

    Main._capture_stage_source_mark(window)

    assert len(requests) == 1
    assert len(applied) == 2
    assert applied[-1].accepted is True
    assert applied[-1].reason == "Machine coordinates are unavailable."
    assert statuses == ["Machine coordinates are unavailable."]
    assert overlay == []
    assert (
        session.source_stage_marks,
        session.registration,
        session.registration_status,
    ) == baseline
    already_closed = lifecycle.accept_sample(
        requests[0],
        RegistrationCaptureOutcome(succeeded=False),
    )
    assert already_closed.accepted is False


def test_current_failed_snapshot_outcome_applies_effect_once_and_closes_request(
    tmp_path: Path,
) -> None:
    window, session, lifecycle, requests, applied, statuses, overlay = (
        _registration_failure_window(tmp_path, submission_accepted=True)
    )
    baseline = (
        session.source_stage_marks,
        session.registration,
        session.registration_status,
    )
    Main._capture_stage_source_mark(window)
    applied.clear()

    Main._on_registration_machine_coordinate_snapshot_finished(
        window,
        requests[0],
        False,
        None,
        "calibration unavailable",
    )

    assert len(applied) == 1
    assert applied[0].accepted is True
    assert applied[0].reason == "calibration unavailable"
    assert statuses == ["calibration unavailable"]
    assert overlay == []
    assert (
        session.source_stage_marks,
        session.registration,
        session.registration_status,
    ) == baseline
    already_closed = lifecycle.accept_sample(
        requests[0],
        RegistrationCaptureOutcome(succeeded=False),
    )
    assert already_closed.accepted is False


def test_snapshot_conversion_failure_applies_each_returned_effect_once(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    window, session, lifecycle, requests, applied, statuses, overlay = (
        _registration_failure_window(tmp_path, submission_accepted=True)
    )
    baseline = (
        session.source_stage_marks,
        session.registration,
        session.registration_status,
    )
    Main._capture_stage_source_mark(window)
    applied.clear()
    monkeypatch.setattr(
        main_module.offsets,
        "raw_stage_to_camera_stage",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            RuntimeError("conversion failed")
        ),
    )

    Main._on_registration_machine_coordinate_snapshot_finished(
        window,
        requests[0],
        True,
        _machine_snapshot((1.0, 2.0, 0.0, 0.0, 3.0)),
        "",
    )

    assert len(applied) == 2
    assert [effects.reason for effects in applied] == [None, "conversion failed"]
    assert all(effects.accepted for effects in applied)
    assert statuses == ["conversion failed"]
    assert overlay == []
    assert (
        session.source_stage_marks,
        session.registration,
        session.registration_status,
    ) == baseline
    already_closed = lifecycle.accept_sample(
        requests[0],
        RegistrationCaptureOutcome(succeeded=False),
    )
    assert already_closed.accepted is False


def test_stage_mark_capture_rejects_calibration_failure_before_mutating_session(
    tmp_path: Path,
) -> None:
    document = _make_document(tmp_path)
    session = DesignSession(document=document)
    session.source_design_marks = ((0.0, 0.0),)
    statuses: list[str] = []
    window = Main.__new__(Main)
    window._design_session = session
    requests: list[object] = []
    _set_rotation_settings(window)
    window.stage_controller = types.SimpleNamespace(
        request_machine_coordinate_snapshot=lambda token, *, axes: (
            requests.append(token) or True
        ),
        current_stage_position=lambda: pytest.fail("synchronous GUI hardware read"),
    )
    window._refresh_design_panel = lambda: None
    window._refresh_design_position = lambda: None
    window._show_status = lambda message, _timeout: statuses.append(message)

    Main._capture_stage_source_mark(window)
    Main._on_registration_machine_coordinate_snapshot_finished(
        window,
        requests.pop(),
        False,
        None,
        "calibration unavailable",
    )

    assert not session.source_stage_marks_compact()
    assert statuses == ["calibration unavailable"]


def test_stage_mark_capture_busy_and_stale_callbacks_do_not_mutate_session(
    tmp_path: Path,
) -> None:
    document = _make_document(tmp_path)
    registry = CoordinateFrameRegistry()
    draft = registry.add(new_design_frame_draft(document, existing_names=()))
    session = DesignSession(document=document)
    session.link_active_frame(draft)
    session.source_design_marks = ((0.0, 0.0),)
    statuses: list[str] = []
    window = Main.__new__(Main)
    window._design_session = session
    window._coordinate_frame_registry = registry
    window._design_registration_lifecycle = DesignRegistrationLifecycle()
    window._refresh_design_panel = lambda: None
    window._refresh_design_position = lambda: None
    window._show_status = lambda message, _timeout: statuses.append(str(message))
    _set_rotation_settings(window)
    window.stage_controller = types.SimpleNamespace(
        request_machine_coordinate_snapshot=lambda _token, *, axes: False,
        current_stage_position=lambda: pytest.fail("synchronous GUI hardware read"),
    )

    Main._capture_stage_source_mark(window)
    assert session.source_stage_marks_compact() == []

    requests: list[object] = []
    window.stage_controller.request_machine_coordinate_snapshot = (
        lambda token, *, axes: requests.append(token) or True
    )
    Main._capture_stage_source_mark(window)
    stale_token = requests.pop()
    cancellation = window._design_registration_lifecycle.cancel(
        RegistrationCancellation.FRAME_CHANGED
    )
    Main._apply_registration_effects(window, cancellation)
    session.active_frame_id = "00000000-0000-0000-0000-000000000001"
    snapshot = _machine_snapshot((1.0, 2.0, 0.0, 0.0, 3.0))
    Main._on_registration_machine_coordinate_snapshot_finished(
        window,
        stale_token,
        True,
        snapshot,
        "",
    )

    assert session.source_stage_marks_compact() == []
    assert not hasattr(window, "_pending_registration_physical_marks")
    assert not hasattr(window, "_pending_registration_mark_capture")


@pytest.mark.parametrize(
    "callback_case",
    ("successful", "failed", "conversion-failed", "legacy"),
)
def test_stale_registration_callback_is_inert_before_snapshot_processing(
    monkeypatch,
    tmp_path: Path,
    callback_case: str,
) -> None:
    document = _make_document(tmp_path / callback_case)
    registry = CoordinateFrameRegistry()
    session = DesignSession(document=document)
    if callback_case != "legacy":
        draft = registry.add(new_design_frame_draft(document, existing_names=()))
        session.link_active_frame(draft)
    session.source_design_marks = ((0.0, 0.0),)
    requests: list[object] = []
    statuses: list[str] = []
    refreshes: list[str] = []
    applied_effects: list[RegistrationEffects] = []
    conversion_calls: list[object] = []
    window = Main.__new__(Main)
    window._design_session = session
    window._coordinate_frame_registry = registry
    window._design_registration_lifecycle = DesignRegistrationLifecycle()
    _set_rotation_settings(window)
    window.stage_controller = types.SimpleNamespace(
        request_machine_coordinate_snapshot=lambda token, *, axes: (
            requests.append(token) or True
        )
    )
    window._camera_stage_xy_from_raw_stage_xy = lambda point: point
    window._refresh_design_panel = lambda: refreshes.append("panel")
    window._refresh_design_position = lambda: refreshes.append("position")
    window._show_status = lambda message, _timeout=0: statuses.append(str(message))
    window._apply_registration_effects = lambda effects: applied_effects.append(
        effects
    )

    Main._capture_stage_source_mark(window)
    token = requests.pop()
    window._design_registration_lifecycle.cancel(
        RegistrationCancellation.FRAME_CHANGED
    )
    applied_effects.clear()
    before_session = session.export_persisted_state()
    before_records = registry.snapshot().records

    def convert_stage_point(*args: object, **kwargs: object) -> tuple[float, float]:
        conversion_calls.append((args, kwargs))
        if callback_case == "conversion-failed":
            raise RuntimeError("conversion failed")
        return (1.0, 2.0)

    monkeypatch.setattr(
        main_module.offsets,
        "raw_stage_to_camera_stage",
        convert_stage_point,
    )
    success = callback_case != "failed"
    snapshot = (
        _machine_snapshot((1.0, 2.0, 0.0, 0.0, 3.0))
        if success
        else None
    )

    Main._on_registration_machine_coordinate_snapshot_finished(
        window,
        token,
        success,
        snapshot,
        "coordinates unavailable" if not success else "",
    )

    assert conversion_calls == []
    assert statuses == []
    assert refreshes == []
    assert applied_effects == []
    assert session.export_persisted_state() == before_session
    assert registry.snapshot().records == before_records


def test_failed_registration_fit_discards_tentative_evidence_before_retry(
    monkeypatch,
    tmp_path: Path,
) -> None:
    document = _make_document(tmp_path)
    registry = CoordinateFrameRegistry()
    draft = registry.add(new_design_frame_draft(document, existing_names=()))
    session = DesignSession(document=document)
    session.link_active_frame(draft)
    session.source_design_marks = ((0.0, 0.0), (1000.0, 0.0))
    requests: list[object] = []
    statuses: list[str] = []
    window = Main.__new__(Main)
    window._design_session = session
    window._coordinate_frame_registry = registry
    window.stage_controller = types.SimpleNamespace(
        request_machine_coordinate_snapshot=lambda token, *, axes: (
            requests.append(token) or True
        )
    )
    _set_rotation_settings(window)
    window._camera_stage_xy_from_raw_stage_xy = lambda point: point
    window._design_navigation_xy_from_physical_machine_xy = lambda point: point
    window._refresh_design_panel = lambda: None
    window._refresh_design_position = lambda: None
    window._show_status = lambda message, _timeout=0: statuses.append(str(message))
    original_commit = main_module.commit_xyb_registration
    monkeypatch.setattr(
        main_module,
        "commit_xyb_registration",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            main_module.DesignModelError("fit failed")
        ),
    )

    for xy in ((1.0, 2.0), (2.0, 2.0)):
        Main._capture_stage_source_mark(window)
        Main._on_registration_machine_coordinate_snapshot_finished(
            window,
            requests.pop(),
            True,
            _machine_snapshot((*xy, 0.0, 0.0, 5.0)),
            "",
        )

    assert registry.get(draft.frame_id) == draft
    assert not hasattr(window, "_pending_registration_physical_marks")
    assert session.source_stage_marks_compact() == []
    assert statuses[-1] == "fit failed"
    assert "captured" not in statuses[-1].lower()

    monkeypatch.setattr(main_module, "commit_xyb_registration", original_commit)
    Main._capture_stage_source_mark(window)
    Main._on_registration_machine_coordinate_snapshot_finished(
        window,
        requests.pop(),
        True,
        _machine_snapshot((3.0, 2.0, 0.0, 0.0, 5.0)),
        "",
    )

    assert session.source_stage_marks_compact() == [(3.0, 2.0)]
    assert not hasattr(window, "_pending_registration_physical_marks")
    assert registry.get(draft.frame_id) == draft


def test_registration_publish_failure_rolls_back_and_does_not_announce_success(
    monkeypatch,
    tmp_path: Path,
) -> None:
    document = _make_document(tmp_path)
    registry = CoordinateFrameRegistry()
    draft = registry.add(new_design_frame_draft(document, existing_names=()))
    session = DesignSession(document=document)
    session.link_active_frame(draft)
    session.source_design_marks = ((0.0, 0.0), (1000.0, 0.0))
    requests: list[object] = []
    statuses: list[str] = []
    window = Main.__new__(Main)
    window._design_session = session
    window._coordinate_frame_registry = registry
    window.stage_controller = types.SimpleNamespace(
        request_machine_coordinate_snapshot=lambda token, *, axes: (
            requests.append(token) or True
        )
    )
    _set_rotation_settings(window)
    window._camera_stage_xy_from_raw_stage_xy = lambda point: point
    window._design_navigation_xy_from_physical_machine_xy = lambda point: point
    window._refresh_design_panel = lambda: None
    window._refresh_design_position = lambda: None
    window._show_status = lambda message, _timeout=0: statuses.append(str(message))
    monkeypatch.setattr(
        main_module.connection_flow,
        "publish_coordinate_frames",
        lambda _owner: (_ for _ in ()).throw(RuntimeError("save unavailable")),
    )

    for xy in ((1.0, 2.0), (2.0, 2.0)):
        Main._capture_stage_source_mark(window)
        Main._on_registration_machine_coordinate_snapshot_finished(
            window,
            requests.pop(),
            True,
            _machine_snapshot((*xy, 0.0, 0.0, 5.0)),
            "",
        )

    restored = registry.get(draft.frame_id)
    assert restored is not None
    assert restored.transform == draft.transform
    assert not restored.readiness["X"].available
    assert not hasattr(window, "_pending_registration_physical_marks")
    assert session.source_stage_marks_compact() == []
    assert statuses[-1] == "save unavailable"
    assert "captured" not in statuses[-1].lower()


def test_registration_save_failure_rolls_back_before_success_is_announced(
    monkeypatch,
    tmp_path: Path,
) -> None:
    document = _make_document(tmp_path)
    registry = CoordinateFrameRegistry()
    draft = registry.add(new_design_frame_draft(document, existing_names=()))
    session = DesignSession(document=document)
    session.link_active_frame(draft)
    session.source_design_marks = ((0.0, 0.0), (1000.0, 0.0))
    requests: list[object] = []
    statuses: list[str] = []
    window = Main.__new__(Main)
    window._coordinate_frame_lifecycle = CoordinateFrameLifecycle()
    window._design_session = session
    window._coordinate_frame_registry = registry
    window.stage_controller = types.SimpleNamespace(
        request_machine_coordinate_snapshot=lambda token, *, axes: (
            requests.append(token) or True
        )
    )
    _set_rotation_settings(window)
    window._camera_stage_xy_from_raw_stage_xy = lambda point: point
    window._design_navigation_xy_from_physical_machine_xy = lambda point: point
    window._refresh_design_panel = lambda: None
    window._refresh_design_position = lambda: None
    window._show_status = lambda message, _timeout=0: statuses.append(str(message))
    monkeypatch.setattr(
        main_module.connection_flow,
        "publish_coordinate_frames",
        lambda _owner: 41,
    )

    for xy in ((1.0, 2.0), (2.0, 2.0)):
        Main._capture_stage_source_mark(window)
        Main._on_registration_machine_coordinate_snapshot_finished(
            window,
            requests.pop(),
            True,
            _machine_snapshot((*xy, 0.0, 0.0, 5.0)),
            "",
        )

    assert registry.get(draft.frame_id) != draft
    assert statuses[-1] == "Saving design registration."
    assert "captured" not in statuses[-1].lower()

    Main._on_coordinate_frame_store_failed(
        window,
        types.SimpleNamespace(
            request_id=41,
            operation="save",
            message="disk full",
        ),
    )

    restored = registry.get(draft.frame_id)
    assert restored is not None
    assert restored.transform == draft.transform
    assert not restored.readiness["X"].available
    assert session.source_stage_marks_compact() == []
    assert all("captured" not in status.lower() for status in statuses[-2:])


def test_deferred_registration_failure_applies_exact_capture_baseline_once(
    monkeypatch,
    tmp_path: Path,
) -> None:
    document = _make_document(tmp_path)
    registry = CoordinateFrameRegistry()
    draft = registry.add(new_design_frame_draft(document, existing_names=()))
    session = DesignSession(document=document)
    session.link_active_frame(draft)
    session.source_design_marks = ((0.0, 0.0), (1000.0, 0.0))
    baseline_registration = DesignRegistration.empty()
    session.source_stage_marks = (None, (8.0, 9.0))
    session.check_stage_marks = [(10.0, 11.0)]
    session.registration = baseline_registration
    session.registration_status = "Exact durable baseline."
    requests: list[object] = []
    tracked: list[FramePublication] = []
    statuses: list[str] = []
    lifecycle = CoordinateFrameLifecycle()
    track_publication = lifecycle.track_publication

    def track(publication: FramePublication) -> None:
        tracked.append(publication)
        track_publication(publication)

    lifecycle.track_publication = track
    window = Main.__new__(Main)
    window._coordinate_frame_lifecycle = lifecycle
    window._design_session = session
    window._coordinate_frame_registry = registry
    window.stage_controller = types.SimpleNamespace(
        request_machine_coordinate_snapshot=lambda token, *, axes: (
            requests.append(token) or True
        ),
        latest_machine_coordinate_snapshot=lambda: _machine_snapshot(
            (50.0, 60.0, 0.0, 0.0, 75.0)
        ),
    )
    _set_rotation_settings(window)
    window._camera_stage_xy_from_raw_stage_xy = lambda point: (
        point[0] - 100.0,
        point[1] + 200.0,
    )
    window._design_navigation_xy_from_physical_machine_xy = lambda point: point
    window._refresh_design_panel = lambda: None
    window._refresh_design_position = lambda: None
    window._show_status = lambda message, _timeout=0: statuses.append(str(message))
    monkeypatch.setattr(
        main_module.connection_flow,
        "publish_coordinate_frames",
        lambda _owner: 41,
    )

    for xy in ((1.0, 2.0), (2.0, 2.0)):
        Main._capture_stage_source_mark(window)
        Main._on_registration_machine_coordinate_snapshot_finished(
            window,
            requests.pop(),
            True,
            _machine_snapshot((*xy, 0.0, 0.0, 5.0)),
            "",
        )

    assert len(tracked) == 1
    saved_rollback = tracked[0].registration_rollback_effects
    current = registry.get(draft.frame_id)
    assert current is not None and current != draft
    session.link_active_frame(
        current,
        machine_b_deg=75.0,
        pivot_machine_xy=(50.0, 60.0),
    )
    session.source_stage_marks = ((901.0, 902.0),)
    session.check_stage_marks = [(903.0, 904.0)]
    session.registration = DesignRegistration.empty()
    session.registration_status = "Changed after publication."
    session.link_active_frame = lambda *_args, **_kwargs: pytest.fail(
        "exact registration rollback must not reconstruct the session"
    )
    _set_rotation_settings(window, (50.0, 60.0))
    window._camera_stage_xy_from_raw_stage_xy = lambda point: (
        point[0] + 300.0,
        point[1] - 400.0,
    )
    applied: list[RegistrationEffects] = []

    def apply_registration_effects(effects: RegistrationEffects) -> None:
        applied.append(effects)
        Main._apply_registration_effects(window, effects)

    window._apply_registration_effects = apply_registration_effects

    Main._on_coordinate_frame_store_failed(
        window,
        types.SimpleNamespace(
            request_id=41,
            operation="save",
            message="disk full",
        ),
    )

    assert saved_rollback is not None
    assert applied == [saved_rollback]
    restored = registry.get(draft.frame_id)
    assert restored is not None
    assert restored.transform == draft.transform
    assert restored.readiness == draft.readiness
    assert session.source_stage_marks == (None, (8.0, 9.0))
    assert session.check_stage_marks == [(10.0, 11.0)]
    assert session.registration is baseline_registration
    assert session.registration_status == "Exact durable baseline."


def test_saved_callback_delegates_acknowledgement_to_coordinate_lifecycle(
    tmp_path: Path,
) -> None:
    document = _make_document(tmp_path)
    registry = CoordinateFrameRegistry()
    draft = registry.add(new_design_frame_draft(document, existing_names=()))
    committed = registry.replace(
        commit_xyb_registration(
            draft,
            design_points=((0.0, 0.0), (1000.0, 0.0)),
            physical_machine_points=((1.0, 2.0), (2.0, 2.0)),
            physical_b_deg=5.0,
            pivot_machine_xy=(0.0, 0.0),
        ),
        expected_version=draft.version,
    )
    publication = FramePublication(
        request_id=41,
        records=(committed,),
        previous_record=draft,
        committed_record=committed,
        machine_b_deg=5.0,
        pivot_machine_xy=(0.0, 0.0),
        success_message="registration saved",
    )
    calls: list[FramePublicationResult] = []

    class _Lifecycle:
        def finish_publication(
            self,
            result: FramePublicationResult,
        ) -> FrameLifecycleEffects:
            calls.append(result)
            return FrameLifecycleEffects(
                acknowledged_publications=(publication,),
            )

    statuses: list[str] = []
    window = Main.__new__(Main)
    window._coordinate_frame_lifecycle = _Lifecycle()
    window._coordinate_frame_registry = registry
    window._design_session = types.SimpleNamespace(active_frame_id=draft.frame_id)
    window._show_status = lambda message, _timeout=0: statuses.append(str(message))

    Main._on_coordinate_frames_saved(
        window,
        types.SimpleNamespace(request_id=41),
    )

    assert calls == [FramePublicationResult(41, True)]
    assert statuses == ["registration saved"]
    assert not hasattr(window, "_registration_persistence_transactions")


def test_main_coordinate_callback_executes_lifecycle_effects_once(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    effects = FrameLifecycleEffects(refresh_display=True)
    calls: list[FramePublicationResult] = []
    applied: list[FrameLifecycleEffects] = []

    class _Lifecycle:
        def finish_publication(
            self,
            result: FramePublicationResult,
        ) -> FrameLifecycleEffects:
            calls.append(result)
            return effects

    owner = Main.__new__(Main)
    owner._coordinate_frame_lifecycle = _Lifecycle()
    owner._apply_coordinate_frame_lifecycle_effects = applied.append
    monkeypatch.setattr(
        main_module.connection_flow,
        "handle_coordinate_frame_saved",
        lambda _owner, _result: None,
    )

    Main._on_coordinate_frames_saved(
        owner,
        types.SimpleNamespace(request_id=12),
    )

    assert calls == [FramePublicationResult(12, True)]
    assert applied == [effects]


def test_main_lifecycle_seams_have_no_policy_pass_through_helpers() -> None:
    assert "_registration_capture_lifecycle" not in Main.__dict__
    assert "_cancel_registration_capture" not in Main.__dict__


def test_failed_callback_executes_lifecycle_rollback_effect(
    tmp_path: Path,
) -> None:
    document = _make_document(tmp_path)
    registry = CoordinateFrameRegistry()
    draft = registry.add(new_design_frame_draft(document, existing_names=()))
    committed = registry.replace(
        commit_xyb_registration(
            draft,
            design_points=((0.0, 0.0), (1000.0, 0.0)),
            physical_machine_points=((1.0, 2.0), (2.0, 2.0)),
            physical_b_deg=5.0,
            pivot_machine_xy=(0.0, 0.0),
        ),
        expected_version=draft.version,
    )
    publication = FramePublication(
        request_id=41,
        records=(committed,),
        previous_record=draft,
        committed_record=committed,
        machine_b_deg=5.0,
        pivot_machine_xy=(0.0, 0.0),
        success_message="registration saved",
    )
    calls: list[FramePublicationResult] = []

    class _Lifecycle:
        def finish_publication(
            self,
            result: FramePublicationResult,
        ) -> FrameLifecycleEffects:
            calls.append(result)
            return FrameLifecycleEffects(
                rollback_publications=(publication,),
                refresh_display=True,
            )

    session = DesignSession(document=document)
    session.link_active_frame(
        committed,
        machine_b_deg=5.0,
        pivot_machine_xy=(0.0, 0.0),
    )
    window = Main.__new__(Main)
    window._coordinate_frame_lifecycle = _Lifecycle()
    window._coordinate_frame_registry = registry
    window._coordinate_frame_document = None
    window._design_session = session
    window._design_navigation_xy_from_physical_machine_xy = lambda point: point
    window._refresh_design_panel = lambda: None
    window._refresh_design_position = lambda: None
    window._show_status = lambda *_args: None

    Main._on_coordinate_frame_store_failed(
        window,
        types.SimpleNamespace(
            request_id=41,
            operation="save",
            message="disk full",
        ),
    )

    assert calls == [FramePublicationResult(41, False)]
    restored = registry.get(draft.frame_id)
    assert restored is not None
    assert restored.transform == draft.transform
    assert restored.readiness == draft.readiness
    assert session.registration is None
    assert not hasattr(window, "_registration_persistence_transactions")


def test_failed_callback_executes_every_lifecycle_rollback_effect(
    tmp_path: Path,
) -> None:
    document = _make_document(tmp_path)
    registry = CoordinateFrameRegistry()
    first_draft = registry.add(
        new_design_frame_draft(document, existing_names=())
    )
    second_draft = registry.add(
        new_design_frame_draft(
            document,
            existing_names=(first_draft.name,),
        )
    )
    first_committed = registry.replace(
        commit_xyb_registration(
            first_draft,
            design_points=((0.0, 0.0), (1000.0, 0.0)),
            physical_machine_points=((1.0, 2.0), (2.0, 2.0)),
            physical_b_deg=5.0,
            pivot_machine_xy=(0.0, 0.0),
        ),
        expected_version=first_draft.version,
    )
    second_committed = registry.replace(
        commit_xyb_registration(
            second_draft,
            design_points=((0.0, 0.0), (1000.0, 0.0)),
            physical_machine_points=((3.0, 4.0), (4.0, 4.0)),
            physical_b_deg=7.0,
            pivot_machine_xy=(0.5, 0.25),
        ),
        expected_version=second_draft.version,
    )
    publications = (
        FramePublication(
            request_id=41,
            records=(first_committed,),
            previous_record=first_draft,
            committed_record=first_committed,
            machine_b_deg=5.0,
            pivot_machine_xy=(0.0, 0.0),
        ),
        FramePublication(
            request_id=42,
            records=(second_committed,),
            previous_record=second_draft,
            committed_record=second_committed,
            machine_b_deg=7.0,
            pivot_machine_xy=(0.5, 0.25),
            operator_alignment=True,
        ),
    )
    calls: list[FramePublicationResult] = []

    class _Lifecycle:
        def finish_publication(
            self,
            result: FramePublicationResult,
        ) -> FrameLifecycleEffects:
            calls.append(result)
            return FrameLifecycleEffects(
                rollback_publications=publications,
                refresh_display=True,
            )

    session = DesignSession(document=document)
    session.link_active_frame(
        first_committed,
        machine_b_deg=5.0,
        pivot_machine_xy=(0.0, 0.0),
    )
    presentation: list[str] = []
    snap_states: list[bool] = []
    window = Main.__new__(Main)
    window._coordinate_frame_lifecycle = _Lifecycle()
    window._coordinate_frame_registry = registry
    window._coordinate_frame_document = None
    window._design_session = session
    window._design_navigation_xy_from_physical_machine_xy = lambda point: point
    window._refresh_design_panel = lambda: presentation.append("panel")
    window._refresh_design_position = lambda: presentation.append("position")
    window._set_design_snap_enabled = lambda enabled: snap_states.append(enabled)
    window._show_status = lambda *_args: None

    Main._on_coordinate_frame_store_failed(
        window,
        types.SimpleNamespace(
            request_id=42,
            operation="save",
            message="disk full",
        ),
    )

    assert calls == [FramePublicationResult(42, False)]
    for draft in (first_draft, second_draft):
        restored = registry.get(draft.frame_id)
        assert restored is not None
        assert restored.transform == draft.transform
        assert restored.readiness == draft.readiness
    assert session.registration is None
    assert snap_states == [True]
    assert presentation == ["panel", "position"]


@pytest.mark.parametrize(
    "save_sequence",
    ("contiguous", "intervening-focus", "trailing-focus"),
)
def test_coalesced_registration_save_failure_restores_earliest_durable_record(
    tmp_path: Path,
    save_sequence: str,
) -> None:
    document = _make_document(tmp_path)
    registry = CoordinateFrameRegistry()
    draft = registry.add(new_design_frame_draft(document, existing_names=()))
    first = registry.replace(
        commit_xyb_registration(
            draft,
            design_points=((0.0, 0.0), (1000.0, 0.0)),
            physical_machine_points=((1.0, 2.0), (2.0, 2.0)),
            physical_b_deg=5.0,
            pivot_machine_xy=(0.0, 0.0),
        ),
        expected_version=draft.version,
    )
    current = first
    if save_sequence != "contiguous":
        current = registry.replace(
            set_focus_reference(first, physical_machine_z_mm=0.25),
            expected_version=first.version,
        )
    lifecycle = CoordinateFrameLifecycle()
    lifecycle.track_publication(
        FramePublication(
            request_id=41,
            records=(first,),
            previous_record=draft,
            committed_record=first,
            machine_b_deg=5.0,
            pivot_machine_xy=(0.0, 0.0),
            success_message="first saved",
        )
    )
    failed_request_id = 42
    if save_sequence != "contiguous":
        lifecycle.track_publication(
            FramePublication(request_id=42, records=(current,))
        )
    if save_sequence != "trailing-focus":
        second_previous = current
        current = registry.replace(
            commit_xyb_registration(
                second_previous,
                design_points=((0.0, 0.0), (1000.0, 0.0)),
                physical_machine_points=((3.0, 4.0), (4.0, 4.0)),
                physical_b_deg=6.0,
                pivot_machine_xy=(0.0, 0.0),
            ),
            expected_version=second_previous.version,
        )
        failed_request_id = 42 if save_sequence == "contiguous" else 43
        lifecycle.track_publication(
            FramePublication(
                request_id=failed_request_id,
                records=(current,),
                previous_record=second_previous,
                committed_record=current,
                machine_b_deg=6.0,
                pivot_machine_xy=(0.0, 0.0),
                success_message="second saved",
            )
        )
    session = DesignSession(document=document)
    session.link_active_frame(
        current,
        machine_b_deg=6.0,
        pivot_machine_xy=(0.0, 0.0),
    )
    window = Main.__new__(Main)
    window._coordinate_frame_lifecycle = lifecycle
    window._design_session = session
    window._coordinate_frame_registry = registry
    window._design_navigation_xy_from_physical_machine_xy = lambda point: point
    window._refresh_design_panel = lambda: None
    window._refresh_design_position = lambda: None
    window._show_status = lambda _message, _timeout=0: None

    Main._on_coordinate_frame_store_failed(
        window,
        types.SimpleNamespace(
            request_id=failed_request_id,
            operation="save",
            message="disk full",
        ),
    )

    restored = registry.get(draft.frame_id)
    assert restored is not None
    assert restored.transform == draft.transform
    assert not restored.readiness["X"].available
    assert session.registration is None
    assert not hasattr(window, "_registration_persistence_transactions")


@pytest.mark.parametrize("newer_outcome", ("saved", "failed"))
def test_older_registration_save_failure_defers_to_newer_publication(
    tmp_path: Path,
    newer_outcome: str,
) -> None:
    document = _make_document(tmp_path)
    registry = CoordinateFrameRegistry()
    draft = registry.add(new_design_frame_draft(document, existing_names=()))
    registered = registry.replace(
        commit_xyb_registration(
            draft,
            design_points=((0.0, 0.0), (1000.0, 0.0)),
            physical_machine_points=((1.0, 2.0), (2.0, 2.0)),
            physical_b_deg=5.0,
            pivot_machine_xy=(0.0, 0.0),
        ),
        expected_version=draft.version,
    )
    focused = registry.replace(
        set_focus_reference(registered, physical_machine_z_mm=0.25),
        expected_version=registered.version,
    )
    session = DesignSession(document=document)
    session.link_active_frame(
        focused,
        machine_b_deg=5.0,
        pivot_machine_xy=(0.0, 0.0),
    )
    lifecycle = CoordinateFrameLifecycle()
    lifecycle.track_publication(
        FramePublication(
            request_id=41,
            records=(registered,),
            previous_record=draft,
            committed_record=registered,
            machine_b_deg=5.0,
            pivot_machine_xy=(0.0, 0.0),
            success_message="registration saved",
        )
    )
    lifecycle.track_publication(
        FramePublication(request_id=42, records=(focused,))
    )
    window = Main.__new__(Main)
    window._coordinate_frame_lifecycle = lifecycle
    window._design_session = session
    window._coordinate_frame_registry = registry
    window._design_navigation_xy_from_physical_machine_xy = lambda point: point
    window._refresh_design_panel = lambda: None
    window._refresh_design_position = lambda: None
    window._show_status = lambda _message, _timeout=0: None

    Main._on_coordinate_frame_store_failed(
        window,
        types.SimpleNamespace(
            request_id=41,
            operation="save",
            message="disk full",
        ),
    )

    assert registry.get(draft.frame_id) == focused
    assert not hasattr(window, "_registration_persistence_transactions")

    if newer_outcome == "saved":
        Main._on_coordinate_frames_saved(
            window,
            types.SimpleNamespace(request_id=42),
        )
        assert registry.get(draft.frame_id) == focused
    else:
        Main._on_coordinate_frame_store_failed(
            window,
            types.SimpleNamespace(
                request_id=42,
                operation="save",
                message="still full",
            ),
        )
        restored = registry.get(draft.frame_id)
        assert restored is not None
        assert restored.transform == draft.transform
        assert session.registration is None
    assert not hasattr(window, "_registration_persistence_transactions")


def test_design_navigation_inverts_universal_calibration_and_current_wco(
    tmp_path: Path,
) -> None:
    document = _make_document(tmp_path)
    pivot = (10.0, -2.0)
    registry = CoordinateFrameRegistry()
    committed = registry.add(
        commit_xyb_registration(
            new_design_frame_draft(document, existing_names=()),
            design_points=((0.0, 0.0), (1000.0, 0.0)),
            physical_machine_points=((12.0, 8.0), (13.0, 8.0)),
            physical_b_deg=10.0,
            pivot_machine_xy=pivot,
        )
    )
    session = DesignSession(document=document)
    session.link_active_frame(committed)
    calibrations = default_axis_calibrations()
    calibrations["X"] = AxisCalibrationSettings(
        enabled=True,
        controller_points=[0.0, 10.0, 20.0],
        physical_points=[0.0, 12.0, 30.0],
    )
    calibrations["Y"] = AxisCalibrationSettings(
        enabled=True,
        controller_points=[0.0, 10.0, 20.0],
        physical_points=[0.0, 8.0, 25.0],
    )
    snapshot = _machine_snapshot(
        (15.0, 16.0, 0.0, 0.0, 20.0),
        work_offset=(4.0, 5.0, 0.0, 0.0, 6.0),
        calibrations=calibrations,
    )
    window = Main.__new__(Main)
    window._design_session = session
    window._coordinate_frame_registry = registry
    window._coordinate_frames_loaded = True
    window.stage_controller = types.SimpleNamespace(
        latest_machine_coordinate_snapshot=lambda: snapshot,
    )
    window.settings_manager = types.SimpleNamespace(
        settings=types.SimpleNamespace(
            software_coordinates=types.SimpleNamespace(
                pivot=RotationPivotSettings(x_mm=pivot[0], y_mm=pivot[1])
            )
        )
    )
    window._active_objective_xy_offset = lambda: (0.0, 0.0)

    physical_target = committed.transform.frame_xy_to_machine(
        (0.5, 0.25),
        machine_b_deg=snapshot.physical_machine_pose.require("B"),
        pivot_machine_xy=pivot,
    )
    expected = (
        snapshot.physical_machine_to_configured_controller("X", physical_target[0]),
        snapshot.physical_machine_to_configured_controller("Y", physical_target[1]),
    )

    assert Main._raw_stage_xy_from_design_xy(window, (500.0, 250.0)) == pytest.approx(
        expected
    )


def test_direct_design_conversion_rejects_stale_usability_snapshot(
    tmp_path: Path,
) -> None:
    document = _make_document(tmp_path)
    registry = CoordinateFrameRegistry()
    committed = registry.add(
        commit_xyb_registration(
            new_design_frame_draft(document, existing_names=()),
            design_points=((0.0, 0.0), (1000.0, 0.0)),
            physical_machine_points=((12.0, 8.0), (13.0, 8.0)),
            physical_b_deg=10.0,
            pivot_machine_xy=(0.0, 0.0),
        )
    )
    session = DesignSession(document=document)
    session.link_active_frame(committed)
    snapshot = _machine_snapshot((12.0, 8.0, 0.0, 0.0, 10.0))
    window = Main.__new__(Main)
    window._design_session = session
    window._coordinate_frame_registry = registry
    window._coordinate_frames_loaded = True
    window._coordinate_frame_authority_blocked_axes = set()
    window.stage_controller = types.SimpleNamespace(
        latest_machine_coordinate_snapshot=lambda: snapshot
    )
    window._active_objective_xy_offset = lambda: (0.0, 0.0)
    _set_rotation_settings(window)

    usability = Main._snapshot_active_design_frame_usability(window)
    registry.replace(committed.with_name("Renamed"), expected_version=committed.version)

    assert (
        Main._raw_stage_xy_from_design_usability_snapshot(
            window,
            usability,
            (500.0, 0.0),
        )
        is None
    )


def test_main_design_usability_method_is_a_thin_lifecycle_adapter(
    tmp_path: Path,
) -> None:
    document = _make_document(tmp_path)
    registry = CoordinateFrameRegistry()
    record = registry.add(new_design_frame_draft(document, existing_names=()))
    session = DesignSession(document=document)
    session.link_active_frame(record)
    machine_snapshot = _machine_snapshot((12.0, 8.0, 0.0, 0.0, 10.0))
    expected = object()
    captured: list[DesignUsabilityContext] = []

    class _Lifecycle:
        def design_usability(self, context: DesignUsabilityContext) -> object:
            captured.append(context)
            return expected

    window = Main.__new__(Main)
    window._design_session = session
    window._coordinate_frame_registry = registry
    window._coordinate_frames_loaded = True
    window._coordinate_frame_authority_blocked_axes = {"b"}
    window._coordinate_frame_lifecycle = _Lifecycle()
    window.stage_controller = types.SimpleNamespace(
        latest_machine_coordinate_snapshot=lambda: machine_snapshot,
    )
    window._rotation_geometry_snapshot = lambda: types.SimpleNamespace(
        pivot_machine_xy=(3.0, 4.0),
    )
    window._active_objective_xy_offset = lambda: (0.5, -0.25)

    result = Main._snapshot_active_design_frame_usability(window)

    assert result is expected
    assert len(captured) == 1
    context = captured[0]
    assert context.frames_loaded is True
    assert context.record is record
    assert context.selected_frame_id == record.frame_id
    assert context.authority_blocked_axes == frozenset({"B"})
    assert context.pivot_machine_xy == (3.0, 4.0)
    assert context.document is document
    assert context.machine_coordinate_snapshot is machine_snapshot
    assert context.objective_xy_offset == (0.5, -0.25)


def test_main_currentness_uses_lifecycle_usability_without_rechecking_policy(
    tmp_path: Path,
) -> None:
    document = _make_document(tmp_path)
    registry = CoordinateFrameRegistry()
    record = registry.add(new_design_frame_draft(document, existing_names=()))
    session = DesignSession(document=document)
    session.link_active_frame(record)
    machine_snapshot = _machine_snapshot((12.0, 8.0, 0.0, 0.0, 10.0))
    current = DesignFrameUsabilitySnapshot(
        load_complete=True,
        frame_id=record.frame_id,
        frame_version=record.version,
        transform=record.transform,
        readiness=tuple(record.readiness.items()),
        provenance_error=None,
        authority_blocked_axes=frozenset(),
        rejection_reason=None,
        document=document,
        metadata=None,
        machine_coordinate_snapshot=machine_snapshot,
        pivot_machine_xy=(0.0, 0.0),
        objective_xy_offset=(0.0, 0.0),
    )
    previous = replace(current, machine_coordinate_snapshot=None)
    captured: list[DesignUsabilityContext] = []

    class _Lifecycle:
        def design_usability(
            self,
            context: DesignUsabilityContext,
        ) -> DesignFrameUsabilitySnapshot:
            captured.append(context)
            return current

    window = Main.__new__(Main)
    window._design_session = session
    window._coordinate_frame_registry = registry
    window._coordinate_frames_loaded = True
    window._coordinate_frame_authority_blocked_axes = {"X", "Y", "B"}
    window._coordinate_frame_lifecycle = _Lifecycle()
    window.stage_controller = types.SimpleNamespace(
        latest_machine_coordinate_snapshot=lambda: machine_snapshot,
    )
    window._rotation_geometry_snapshot = lambda: types.SimpleNamespace(
        pivot_machine_xy=(0.0, 0.0),
    )
    window._active_objective_xy_offset = lambda: (0.0, 0.0)

    result = Main._design_frame_usability_snapshot_is_current(window, previous)

    assert result is True
    assert len(captured) == 1


def test_gui_route_rejects_verified_missing_reference_draft_before_materializing(
    tmp_path: Path,
) -> None:
    document = _make_document(tmp_path)
    registry = CoordinateFrameRegistry()
    draft = registry.add(new_design_frame_draft(document, existing_names=()))
    session = DesignSession(document=document)
    session.link_active_frame(draft)
    session.source_design_marks = ((0.0, 0.0), (1000.0, 0.0))
    session.source_stage_marks = ((10.0, 20.0), (11.0, 20.0))
    session._rebuild_registration()
    assert session.registration is not None and session.registration.valid
    session.route = types.SimpleNamespace(points=[object()])
    materialized: list[object] = []
    statuses: list[str] = []
    window = Main.__new__(Main)
    window._design_session = session
    window._coordinate_frame_registry = registry
    window._coordinate_frames_loaded = True
    window._coordinate_frame_authority_blocked_axes = set()
    window._route_measurement_points = lambda route: (
        materialized.append(route) or [types.SimpleNamespace(index=1)]
    )
    window._api_structure_number_for_measurement_point = lambda point: point.index
    window._show_route_runtime_status = (
        lambda message, _timeout: statuses.append(str(message))
    )
    window._show_status = lambda message, _timeout=0: statuses.append(str(message))

    plan = Main._route_measurement_start_plan(
        window,
        types.SimpleNamespace(
            current_point=1,
            previous_ok_only=False,
            previous_csv_path="",
        ),
    )

    assert plan is None
    assert materialized == []
    assert statuses


def test_unverified_design_provenance_blocks_conversion_and_motion_until_verified(
    tmp_path: Path,
) -> None:
    document = _make_document(tmp_path)
    registry = CoordinateFrameRegistry()
    committed = registry.add(
        commit_xyb_registration(
            new_design_frame_draft(document, existing_names=()),
            design_points=((0.0, 0.0), (1000.0, 0.0)),
            physical_machine_points=((12.0, 8.0), (13.0, 8.0)),
            physical_b_deg=10.0,
            pivot_machine_xy=(0.0, 0.0),
        )
    )
    session = DesignSession(document=document)
    session.link_active_frame(committed)
    snapshot = _machine_snapshot((12.0, 8.0, 0.0, 0.0, 10.0))
    move_requests: list[tuple[float, float]] = []
    statuses: list[str] = []
    window = Main.__new__(Main)
    window._design_session = session
    window._coordinate_frame_registry = registry
    window._coordinate_frames_loaded = False
    window.stage_controller = types.SimpleNamespace(
        latest_machine_coordinate_snapshot=lambda: snapshot,
        is_busy=lambda: False,
        request_move_to_xy=lambda x_value, y_value: move_requests.append(
            (float(x_value), float(y_value))
        ),
    )
    _set_rotation_settings(window)
    window._show_status = lambda message, _timeout=0: statuses.append(str(message))
    window._refresh_design_panel = lambda: None
    window._clear_planned_move_prediction = lambda **_kwargs: None
    window._last_selected_design_point = None
    window._pending_planned_move_target_xy = None
    window._pending_planned_move_source_label = None

    assert Main._raw_stage_xy_from_design_xy(window, (500.0, 0.0)) is None
    assert not Main._move_to_design_coordinate(
        window,
        (500.0, 0.0),
        source_label="design window",
    )

    window._coordinate_frames_loaded = True
    for status, reason in (
        ("pending", "Design coordinate provenance is being checked."),
        ("blocked", "Design source changed since registration."),
    ):
        registry.reset(
            (
                replace(
                    committed,
                    metadata={
                        **committed.metadata,
                        RUNTIME_PROVENANCE_STATUS: status,
                        RUNTIME_PROVENANCE_REASON: reason,
                    },
                ),
            )
        )
        assert Main._raw_stage_xy_from_design_xy(window, (500.0, 0.0)) is None
        assert not Main._move_to_design_coordinate(
            window,
            (500.0, 0.0),
            source_label="design window",
        )

    verified = replace(
        committed,
        metadata={
            **committed.metadata,
            RUNTIME_PROVENANCE_STATUS: "verified",
            RUNTIME_PROVENANCE_REASON: "",
        },
    )
    registry.reset((verified,))
    session.link_active_frame(verified)

    assert Main._raw_stage_xy_from_design_xy(window, (500.0, 0.0)) is not None
    assert Main._move_to_design_coordinate(
        window,
        (500.0, 0.0),
        source_label="design window",
    )
    assert len(move_requests) == 1


@pytest.mark.parametrize("registration_kind", ["draft", "legacy"])
def test_operator_design_move_requires_ready_durable_xyb_registration(
    registration_kind: str,
    tmp_path: Path,
) -> None:
    document = _make_document(tmp_path / registration_kind)
    registry = CoordinateFrameRegistry()
    session = DesignSession(document=document)
    if registration_kind == "draft":
        draft = registry.add(new_design_frame_draft(document, existing_names=()))
        session.link_active_frame(draft)
    else:
        session.source_design_marks = ((0.0, 0.0), (1000.0, 0.0))
        session.source_stage_marks = ((10.0, 20.0), (11.0, 20.0))
        session._rebuild_registration()
        assert session.registration is not None and session.registration.valid
        assert session.active_frame_id is None
    move_requests: list[tuple[float, float]] = []
    statuses: list[str] = []
    snapshot = _machine_snapshot((10.0, 20.0, 0.0, 0.0, 5.0))
    window = Main.__new__(Main)
    window._design_session = session
    window._coordinate_frame_registry = registry
    window._coordinate_frames_loaded = True
    window.stage_controller = types.SimpleNamespace(
        latest_machine_coordinate_snapshot=lambda: snapshot,
        is_busy=lambda: False,
        request_move_to_xy=lambda x_value, y_value: move_requests.append(
            (float(x_value), float(y_value))
        ),
    )
    _set_rotation_settings(window)
    window._raw_stage_xy_from_camera_stage_xy = lambda point: point
    window._show_status = lambda message, _timeout=0: statuses.append(str(message))
    window._refresh_design_panel = lambda: None
    window._clear_planned_move_prediction = lambda **_kwargs: None
    window._last_selected_design_point = None
    window._pending_planned_move_target_xy = None
    window._pending_planned_move_source_label = None

    assert Main._raw_stage_xy_from_design_xy(window, (500.0, 0.0)) is None
    assert not Main._move_to_design_coordinate(
        window,
        (500.0, 0.0),
        source_label="design window",
    )
    assert move_requests == []
    assert statuses


def test_pending_provenance_cannot_reactivate_on_homing_or_start_route(
    tmp_path: Path,
) -> None:
    document = _make_document(tmp_path)
    registry = CoordinateFrameRegistry()
    committed = registry.add(
        commit_xyb_registration(
            new_design_frame_draft(document, existing_names=()),
            design_points=((0.0, 0.0), (1000.0, 0.0)),
            physical_machine_points=((12.0, 8.0), (13.0, 8.0)),
            physical_b_deg=10.0,
            pivot_machine_xy=(0.0, 0.0),
        )
    )
    pending = replace(
        committed,
        metadata={
            **committed.metadata,
            RUNTIME_PROVENANCE_STATUS: "pending",
            RUNTIME_PROVENANCE_REASON: (
                "Design coordinate provenance is being checked."
            ),
        },
    )
    registry.reset((pending,))
    session = DesignSession(document=document)
    session.link_active_frame(committed)
    session.invalidate_registration("Design coordinate provenance is being checked.")
    session.route = types.SimpleNamespace(points=[object()])
    snapshot = _machine_snapshot((12.0, 8.0, 0.0, 0.0, 10.0))
    statuses: list[str] = []
    route_point_calls: list[object] = []
    route_point = types.SimpleNamespace(index=1)
    window = Main.__new__(Main)
    window._design_session = session
    window._coordinate_frame_registry = registry
    window._coordinate_frames_loaded = False
    window._design_navigation_xy_from_physical_machine_xy = lambda point: point
    window.stage_controller = types.SimpleNamespace(
        axes_are_homed=lambda axes: axes.issubset({"X", "Y"}),
        latest_machine_coordinate_snapshot=lambda: snapshot,
    )
    _set_rotation_settings(window)
    window._route_measurement_points = lambda route: (
        route_point_calls.append(route) or [route_point]
    )
    window._api_structure_number_for_measurement_point = lambda point: point.index
    window._show_route_runtime_status = (
        lambda message, _timeout: statuses.append(str(message))
    )
    window._show_status = lambda message, _timeout=0: statuses.append(str(message))

    Main._apply_coordinate_frame_authority_blocks(window)

    assert window._coordinate_frame_authority_blocked_axes == {"X", "Y", "B"}
    assert session.registration is not None
    assert not session.registration.valid

    session.link_active_frame(pending)
    assert session.registration is not None
    assert session.registration.valid
    window._coordinate_frames_loaded = True
    plan = Main._route_measurement_start_plan(
        window,
        types.SimpleNamespace(
            current_point=1,
            previous_ok_only=False,
            previous_csv_path="",
        ),
    )

    assert plan is None
    assert route_point_calls == []
    assert any("provenance" in message.lower() for message in statuses)


def test_authority_uses_xy_homing_and_tracked_calibrated_b_without_b_homing(
    tmp_path: Path,
) -> None:
    document = _make_document(tmp_path)
    registry = CoordinateFrameRegistry()
    committed = registry.add(
        main_module.commit_xyb_registration(
            new_design_frame_draft(document, existing_names=()),
            design_points=((0.0, 0.0), (1000.0, 0.0)),
            physical_machine_points=((3.0, 4.0), (4.0, 4.0)),
            physical_b_deg=12.0,
            pivot_machine_xy=(0.0, 0.0),
        )
    )
    session = DesignSession(document=document)
    session.link_active_frame(committed)
    homed: set[str] = set()
    homing_queries: list[set[str]] = []

    def axes_are_homed(axes: set[str]) -> bool:
        homing_queries.append(set(axes))
        assert "B" not in axes
        return axes.issubset(homed)

    window = Main.__new__(Main)
    window._design_session = session
    window._coordinate_frame_registry = registry
    window._coordinate_frames_loaded = True
    window._design_navigation_xy_from_physical_machine_xy = lambda point: point
    _set_rotation_settings(window)
    b_snapshot = _machine_snapshot((0.0, 0.0, 0.0, 0.0, 42.0))
    window.stage_controller = types.SimpleNamespace(
        axes_are_homed=axes_are_homed,
        latest_machine_coordinate_snapshot=lambda: b_snapshot,
    )

    Main._apply_coordinate_frame_authority_blocks(window)

    assert window._coordinate_frame_authority_blocked_axes == {"X", "Y"}
    assert session.registration is not None
    assert not session.registration.valid
    assert registry.get(committed.frame_id) == committed

    homed.update({"X", "Y"})
    Main._apply_coordinate_frame_authority_blocks(window)

    assert window._coordinate_frame_authority_blocked_axes == set()
    assert session.registration is not None
    assert session.registration.valid
    assert homing_queries == [{"X"}, {"Y"}, {"X"}, {"Y"}]
    assert registry.get(committed.frame_id) == committed
    assert registry.snapshot().generation == 1


def test_authority_temporarily_blocks_b_when_tracked_position_is_unknown(
    tmp_path: Path,
) -> None:
    document = _make_document(tmp_path)
    registry = CoordinateFrameRegistry()
    committed = registry.add(
        main_module.commit_xyb_registration(
            new_design_frame_draft(document, existing_names=()),
            design_points=((0.0, 0.0), (1000.0, 0.0)),
            physical_machine_points=((3.0, 4.0), (4.0, 4.0)),
            physical_b_deg=12.0,
            pivot_machine_xy=(0.0, 0.0),
        )
    )
    session = DesignSession(document=document)
    session.link_active_frame(committed)
    window = Main.__new__(Main)
    window._design_session = session
    window._coordinate_frame_registry = registry
    window._coordinate_frames_loaded = True
    window._design_navigation_xy_from_physical_machine_xy = lambda point: point
    _set_rotation_settings(window)
    window.stage_controller = types.SimpleNamespace(
        axes_are_homed=lambda axes: axes.issubset({"X", "Y"}),
        latest_machine_coordinate_snapshot=lambda: None,
    )

    Main._apply_coordinate_frame_authority_blocks(window)

    assert window._coordinate_frame_authority_blocked_axes == {"B"}
    assert session.registration is not None
    assert not session.registration.valid
    assert registry.get(committed.frame_id) == committed


def test_check_capture_at_changed_b_preserves_transform_and_normalizes_metadata(
    monkeypatch,
    tmp_path: Path,
) -> None:
    document = _make_document(tmp_path)
    pivot = (10.0, -2.0)
    registry = CoordinateFrameRegistry()
    committed = registry.add(
        main_module.commit_xyb_registration(
            new_design_frame_draft(document, existing_names=()),
            design_points=((0.0, 0.0), (1000.0, 0.0)),
            physical_machine_points=((3.0, 4.0), (3.0, 5.0)),
            physical_b_deg=12.0,
            pivot_machine_xy=pivot,
        )
    )
    assert committed.transform is not None
    check_design = (500.0, 0.0)
    current_b = 42.0
    current_check_machine = committed.transform.frame_xy_to_machine(
        (0.5, 0.0),
        machine_b_deg=current_b,
        pivot_machine_xy=pivot,
    )
    reference_check_machine = committed.transform.frame_xy_to_machine(
        (0.5, 0.0),
        machine_b_deg=12.0,
        pivot_machine_xy=pivot,
    )
    session = DesignSession(document=document)
    session.link_active_frame(committed)
    session.check_design_marks = [check_design]
    window = Main.__new__(Main)
    window._design_session = session
    window._coordinate_frame_registry = registry
    _set_rotation_settings(window, pivot)
    capture_snapshot = _machine_snapshot(
        (
            current_check_machine[0],
            current_check_machine[1],
            0.0,
            0.0,
            current_b,
        )
    )
    requests: list[object] = []
    window.stage_controller = types.SimpleNamespace(
        request_machine_coordinate_snapshot=lambda token, *, axes: (
            requests.append(token) or True
        ),
        latest_machine_coordinate_snapshot=lambda: capture_snapshot,
    )
    window._camera_stage_xy_from_raw_stage_xy = lambda point: point
    window._refresh_design_panel = lambda: None
    window._refresh_design_position = lambda: None
    window._show_status = lambda *_args: None
    monkeypatch.setattr(
        main_module.connection_flow,
        "publish_coordinate_frames",
        lambda _owner: None,
    )

    Main._capture_stage_check_mark(window)
    Main._on_registration_machine_coordinate_snapshot_finished(
        window,
        requests.pop(),
        True,
        capture_snapshot,
        "",
    )

    updated = registry.get(committed.frame_id)
    assert updated is not None
    assert updated.transform == committed.transform
    assert updated.readiness == committed.readiness
    metadata = main_module.DesignFrameMetadata.from_mapping(updated.metadata)
    assert metadata.source_design_marks == ((0.0, 0.0), (1000.0, 0.0))
    assert metadata.source_machine_marks == ((3.0, 4.0), (3.0, 5.0))
    assert metadata.check_design_marks == (check_design,)
    assert metadata.check_machine_marks[0] == pytest.approx(reference_check_machine)
    assert metadata.rms_residual_mm == pytest.approx(0.0, abs=1e-12)
    assert metadata.max_residual_mm == pytest.approx(0.0, abs=1e-12)


def test_source_capture_at_changed_b_retains_existing_check_evidence(
    monkeypatch,
    tmp_path: Path,
) -> None:
    document = _make_document(tmp_path)
    pivot = (10.0, -2.0)
    registry = CoordinateFrameRegistry()
    committed = registry.add(
        main_module.commit_xyb_registration(
            new_design_frame_draft(document, existing_names=()),
            design_points=((0.0, 0.0), (1000.0, 0.0)),
            physical_machine_points=((3.0, 4.0), (3.0, 5.0)),
            physical_b_deg=12.0,
            pivot_machine_xy=pivot,
            check_design_points=((500.0, 500.0),),
            check_machine_points=((2.5, 4.5),),
        )
    )
    assert committed.transform is not None
    current_b = 42.0
    third_design = (0.0, 1000.0)
    third_machine = committed.transform.frame_xy_to_machine(
        (0.0, 1.0),
        machine_b_deg=current_b,
        pivot_machine_xy=pivot,
    )
    existing_check_at_current_b = committed.transform.frame_xy_to_machine(
        (0.5, 0.5),
        machine_b_deg=current_b,
        pivot_machine_xy=pivot,
    )
    session = DesignSession(document=document)
    session.link_active_frame(
        committed,
        machine_b_deg=current_b,
        pivot_machine_xy=pivot,
    )
    session.add_source_design_mark(third_design)
    statuses: list[str] = []
    requests: list[object] = []
    window = Main.__new__(Main)
    window._design_session = session
    window._coordinate_frame_registry = registry
    _set_rotation_settings(window, pivot)
    window.stage_controller = types.SimpleNamespace(
        request_machine_coordinate_snapshot=lambda token, *, axes: (
            requests.append(token) or True
        )
    )
    window._camera_stage_xy_from_raw_stage_xy = lambda point: point
    window._design_navigation_xy_from_physical_machine_xy = lambda point: point
    window._refresh_design_panel = lambda: None
    window._refresh_design_position = lambda: None
    window._show_status = lambda message, _timeout: statuses.append(str(message))
    monkeypatch.setattr(
        main_module.connection_flow,
        "publish_coordinate_frames",
        lambda _owner: None,
    )

    Main._capture_stage_source_mark(window)
    Main._on_registration_machine_coordinate_snapshot_finished(
        window,
        requests.pop(),
        True,
        _machine_snapshot((*third_machine, 0.0, 0.0, current_b)),
        "",
    )

    updated = registry.get(committed.frame_id)
    assert updated is not None
    assert updated.version == committed.version + 1
    metadata = main_module.DesignFrameMetadata.from_mapping(updated.metadata)
    assert metadata.source_design_marks == (
        (0.0, 0.0),
        (1000.0, 0.0),
        third_design,
    )
    assert metadata.check_design_marks == ((500.0, 500.0),)
    assert metadata.check_machine_marks[0] == pytest.approx(
        existing_check_at_current_b
    )
    assert statuses and "captured" in statuses[-1].lower()


def test_source_capture_at_changed_b_rotates_measured_source_evidence(
    monkeypatch,
    tmp_path: Path,
) -> None:
    document = _make_document(tmp_path)
    pivot = (10.0, -2.0)
    reference_b = 12.0
    source_design = ((0.0, 0.0), (1000.0, 0.0), (0.0, 1000.0))
    source_machine = ((3.0, 4.0), (4.02, 4.01), (2.98, 5.04))
    registry = CoordinateFrameRegistry()
    committed = registry.add(
        main_module.commit_xyb_registration(
            new_design_frame_draft(document, existing_names=()),
            design_points=source_design,
            physical_machine_points=source_machine,
            physical_b_deg=reference_b,
            pivot_machine_xy=pivot,
        )
    )
    assert committed.transform is not None
    initial_metadata = main_module.DesignFrameMetadata.from_mapping(
        committed.metadata
    )
    assert initial_metadata.max_residual_mm is not None
    assert initial_metadata.max_residual_mm > 0.0

    current_b = 42.0
    delta_b = current_b - reference_b
    expected_existing_at_current_b = tuple(
        tuple(
            np.asarray(pivot)
            + np.asarray(
                main_module.rotate_xy(
                    (point[0] - pivot[0], point[1] - pivot[1]),
                    delta_b,
                )
            )
        )
        for point in source_machine
    )
    synthesized_existing_at_current_b = tuple(
        committed.transform.frame_xy_to_machine(
            (
                point[0] * initial_metadata.design_unit_mm,
                point[1] * initial_metadata.design_unit_mm,
            ),
            machine_b_deg=current_b,
            pivot_machine_xy=pivot,
        )
        for point in source_design
    )
    assert not np.allclose(
        expected_existing_at_current_b,
        synthesized_existing_at_current_b,
    )
    fourth_design = (1000.0, 1000.0)
    fourth_machine = committed.transform.frame_xy_to_machine(
        (1.0, 1.0),
        machine_b_deg=current_b,
        pivot_machine_xy=pivot,
    )
    session = DesignSession(document=document)
    session.link_active_frame(
        committed,
        machine_b_deg=current_b,
        pivot_machine_xy=pivot,
    )
    session.add_source_design_mark(fourth_design)
    statuses: list[str] = []
    requests: list[object] = []
    window = Main.__new__(Main)
    window._design_session = session
    window._coordinate_frame_registry = registry
    _set_rotation_settings(window, pivot)
    window.stage_controller = types.SimpleNamespace(
        request_machine_coordinate_snapshot=lambda token, *, axes: (
            requests.append(token) or True
        )
    )
    window._camera_stage_xy_from_raw_stage_xy = lambda point: point
    window._design_navigation_xy_from_physical_machine_xy = lambda point: point
    window._refresh_design_panel = lambda: None
    window._refresh_design_position = lambda: None
    window._show_status = lambda message, _timeout: statuses.append(str(message))
    monkeypatch.setattr(
        main_module.connection_flow,
        "publish_coordinate_frames",
        lambda _owner: None,
    )

    Main._capture_stage_source_mark(window)
    Main._on_registration_machine_coordinate_snapshot_finished(
        window,
        requests.pop(),
        True,
        _machine_snapshot((*fourth_machine, 0.0, 0.0, current_b)),
        "",
    )

    updated = registry.get(committed.frame_id)
    assert updated is not None
    metadata = main_module.DesignFrameMetadata.from_mapping(updated.metadata)
    assert np.allclose(
        metadata.source_machine_marks[: len(source_machine)],
        expected_existing_at_current_b,
    )
    assert metadata.max_residual_mm is not None
    assert metadata.max_residual_mm > 0.0
    assert statuses and "captured" in statuses[-1].lower()


def test_legacy_migration_projection_failure_leaves_registry_and_link_unchanged(
    tmp_path: Path,
) -> None:
    document = _make_document(tmp_path)
    registry = CoordinateFrameRegistry()
    session = DesignSession(document=document)
    session.source_design_marks = ((0.0, 0.0), (1000.0, 0.0))
    session.source_stage_marks = ((3.0, 4.0), (4.0, 4.0))
    _record_legacy_work_provenance(session, (0.0, 0.0, 0.0, 0.0, 0.0))
    before = registry.snapshot()
    statuses: list[str] = []
    window = Main.__new__(Main)
    window._design_session = session
    window._coordinate_frame_registry = registry
    window._coordinate_frames_loaded = True
    window._active_design_frame_metadata = main_module.DesignFrameMetadata.from_document(
        document
    )
    window._raw_stage_xy_from_camera_stage_xy = lambda point: point
    window._camera_stage_xy_from_raw_stage_xy = lambda point: point
    _set_rotation_settings(window)
    snapshot = _machine_snapshot((0.0, 0.0, 0.0, 0.0, 12.0))
    window.stage_controller = types.SimpleNamespace(
        latest_machine_coordinate_snapshot=lambda: snapshot,
    )
    window._design_navigation_xy_from_physical_machine_xy = lambda _point: (
        (_ for _ in ()).throw(RuntimeError("inverse calibration unavailable"))
    )
    window._show_status = lambda message, _timeout: statuses.append(str(message))

    Main._activate_loaded_design_frame(window)

    assert registry.snapshot() == before
    assert session.active_frame_id is None
    assert statuses == [
        "Design frame coordinates are unavailable: inverse calibration unavailable"
    ]


def test_reset_design_registration_creates_an_independent_persistent_draft(
    monkeypatch,
    tmp_path: Path,
) -> None:
    document = _make_document(tmp_path)
    registry = CoordinateFrameRegistry()
    first = registry.add(new_design_frame_draft(document, existing_names=()))
    session = DesignSession(document=document)
    session.link_active_frame(first)
    events: list[object] = []
    window = Main.__new__(Main)
    window._design_session = session
    window._coordinate_frame_registry = registry
    window._coordinate_frames_loaded = True
    window._active_design_frame_metadata = main_module.DesignFrameMetadata.from_mapping(
        first.metadata
    )
    _set_rotation_settings(window)
    window.stage_controller = types.SimpleNamespace(
        latest_machine_coordinate_snapshot=lambda: _machine_snapshot(
            (0.0, 0.0, 0.0, 0.0, 0.0)
        )
    )
    window._pending_alignment_preparation = object()
    window._last_selected_design_point = (1.0, 2.0)
    window._set_design_snap_enabled = lambda value: events.append(("snap", value))
    window._refresh_design_panel = lambda: events.append(("panel",))
    window._refresh_design_position = lambda: events.append(("position",))
    window._show_status = lambda message, _timeout: events.append(("status", message))
    window._apply_coordinate_frame_authority_blocks = lambda: events.append(
        ("authority",)
    )
    monkeypatch.setattr(
        main_module.connection_flow,
        "publish_coordinate_frames",
        lambda _owner: events.append(("publish",)),
    )

    Main._clear_design_registration(window)

    records = registry.snapshot().records
    assert len(records) == 2
    assert session.active_frame_id != first.frame_id
    assert session.active_frame_id == records[-1].frame_id
    assert records[-1].name == f"{document.path.stem} (2)"
    new_metadata = main_module.DesignFrameMetadata.from_mapping(records[-1].metadata)
    assert new_metadata.source_design_marks == ()
    assert new_metadata.source_machine_marks == ()
    assert ("publish",) in events


def test_registration_focus_completion_publishes_only_after_matching_replace(
    monkeypatch,
    tmp_path: Path,
) -> None:
    document = _make_document(tmp_path)
    registry = CoordinateFrameRegistry()
    registered = registry.add(
        commit_xyb_registration(
            new_design_frame_draft(document, existing_names=()),
            design_points=((0.0, 0.0), (1000.0, 0.0)),
            physical_machine_points=((1.0, 2.0), (2.0, 2.0)),
            physical_b_deg=0.0,
            pivot_machine_xy=(0.0, 0.0),
        )
    )
    context = _lifecycle_context(
        frame_id=registered.frame_id,
        frame_version=registered.version,
    )
    lifecycle = DesignRegistrationLifecycle()
    token = _pending_focus_operation(lifecycle, context, move_completed=True)
    events: list[object] = []
    window = Main.__new__(Main)
    window._coordinate_frame_registry = registry
    window._design_session = types.SimpleNamespace(active_frame_id=registered.frame_id)
    window._design_registration_lifecycle = lifecycle
    window._design_focus_overlay_context_key = lambda: context
    window.design_layout_window = None
    window._refresh_design_panel = lambda: events.append("refresh")
    window._show_status = lambda message, _timeout: events.append(("status", message))
    monkeypatch.setattr(
        main_module.connection_flow,
        "publish_coordinate_frames",
        lambda _owner: events.append("publish"),
    )

    Main._on_registration_focus_autofocus_finished(
        window,
        token,
        True,
        6.0,
        "Focused.",
    )

    focused = registry.get(registered.frame_id)
    assert focused is not None and focused.readiness["Z"].available
    assert focused.transform.z_zero_machine_mm == 6.0
    assert events.index("publish") < events.index("refresh")


def test_registration_focus_completion_ignores_inactive_frame(
    monkeypatch,
    tmp_path: Path,
) -> None:
    document = _make_document(tmp_path)
    registry = CoordinateFrameRegistry()
    registered = registry.add(
        commit_xyb_registration(
            new_design_frame_draft(document, existing_names=()),
            design_points=((0.0, 0.0), (1000.0, 0.0)),
            physical_machine_points=((1.0, 2.0), (2.0, 2.0)),
            physical_b_deg=0.0,
            pivot_machine_xy=(0.0, 0.0),
        )
    )
    context = _lifecycle_context(
        frame_id=registered.frame_id,
        frame_version=registered.version,
    )
    lifecycle = DesignRegistrationLifecycle()
    token = _pending_focus_operation(lifecycle, context, move_completed=True)
    events: list[str] = []
    window = Main.__new__(Main)
    window._coordinate_frame_registry = registry
    window._design_session = types.SimpleNamespace(active_frame_id="another-frame")
    window._design_registration_lifecycle = lifecycle
    window._design_focus_overlay_context_key = lambda: replace(
        context,
        frame_id="another-frame",
    )
    window.design_layout_window = None
    window._refresh_design_panel = lambda: events.append("refresh")
    window._show_status = lambda *_args: events.append("status")
    monkeypatch.setattr(
        main_module.connection_flow,
        "publish_coordinate_frames",
        lambda _owner: events.append("publish"),
    )

    Main._on_registration_focus_autofocus_finished(
        window,
        token,
        True,
        6.0,
        "Focused.",
    )

    current = registry.get(registered.frame_id)
    assert current is not None
    assert current.transform.z_zero_machine_mm is None
    assert events == []


def test_registration_focus_move_completion_requires_matching_token_target_and_frame(
    tmp_path: Path,
) -> None:
    document = _make_document(tmp_path)
    registry = CoordinateFrameRegistry()
    registered = registry.add(
        commit_xyb_registration(
            new_design_frame_draft(document, existing_names=()),
            design_points=((0.0, 0.0), (1000.0, 0.0)),
            physical_machine_points=((1.0, 2.0), (2.0, 2.0)),
            physical_b_deg=0.0,
            pivot_machine_xy=(0.0, 0.0),
        )
    )
    context = {"value": _lifecycle_context(
        frame_id=registered.frame_id,
        frame_version=registered.version,
    )}
    lifecycle = DesignRegistrationLifecycle()
    token = _pending_focus_operation(lifecycle, context["value"])
    autofocus: list[object] = []
    window = Main.__new__(Main)
    window._coordinate_frame_registry = registry
    window._design_session = types.SimpleNamespace(active_frame_id=registered.frame_id)
    window._design_registration_lifecycle = lifecycle
    window._design_focus_overlay_context_key = lambda: context["value"]
    window.design_layout_window = None
    window._show_status = lambda *_args: None
    window.design_registration_autofocus_finished = types.SimpleNamespace(emit=lambda *_args: None)
    window.stage_controller = types.SimpleNamespace(
        request_registration_autofocus=lambda actual_token, _completion: (
            autofocus.append(actual_token) or True
        )
    )

    Main._on_registration_focus_move_finished(
        window, object(), (4.0, 5.0), True, "unrelated"
    )
    Main._on_registration_focus_move_finished(
        window, token, (40.0, 50.0), True, "wrong target"
    )
    assert autofocus == []

    context["value"] = replace(context["value"], frame_id="switched")
    Main._on_registration_focus_move_finished(
        window, token, (4.0, 5.0), True, "arrived"
    )
    assert autofocus == []

    context["value"] = _lifecycle_context(
        frame_id=registered.frame_id,
        frame_version=registered.version,
    )
    token = _pending_focus_operation(lifecycle, context["value"])
    Main._on_registration_focus_move_finished(
        window, token, (4.0, 5.0), True, "arrived"
    )
    assert autofocus == [token]


def test_registration_focus_failed_specific_move_does_not_start_autofocus(
    tmp_path: Path,
) -> None:
    document = _make_document(tmp_path)
    registry = CoordinateFrameRegistry()
    registered = registry.add(
        commit_xyb_registration(
            new_design_frame_draft(document, existing_names=()),
            design_points=((0.0, 0.0), (1000.0, 0.0)),
            physical_machine_points=((1.0, 2.0), (2.0, 2.0)),
            physical_b_deg=0.0,
            pivot_machine_xy=(0.0, 0.0),
        )
    )
    context = _lifecycle_context(
        frame_id=registered.frame_id,
        frame_version=registered.version,
    )
    lifecycle = DesignRegistrationLifecycle()
    token = _pending_focus_operation(lifecycle, context)
    autofocus: list[object] = []
    statuses: list[str] = []
    window = Main.__new__(Main)
    window._coordinate_frame_registry = registry
    window._design_session = types.SimpleNamespace(active_frame_id=registered.frame_id)
    window._design_registration_lifecycle = lifecycle
    window._design_focus_overlay_context_key = lambda: context
    window.design_layout_window = None
    window._show_status = lambda message, _timeout: statuses.append(message)
    window.design_registration_autofocus_finished = types.SimpleNamespace(emit=lambda *_args: None)
    window.stage_controller = types.SimpleNamespace(
        request_registration_autofocus=lambda *_args: autofocus.append(token) or True
    )

    Main._on_registration_focus_move_finished(
        window, token, (4.0, 5.0), False, "move failed"
    )

    assert autofocus == []
    assert statuses == ["move failed"]
    assert not hasattr(window, "_pending_registration_focus_token")


def test_stale_contact_completion_never_publishes_or_captures_a(
    monkeypatch,
    tmp_path: Path,
) -> None:
    document = _make_document(tmp_path)
    registry = CoordinateFrameRegistry()
    focused = registry.add(
        set_focus_reference(
            commit_xyb_registration(
                new_design_frame_draft(document, existing_names=()),
                design_points=((0.0, 0.0), (1000.0, 0.0)),
                physical_machine_points=((1.0, 2.0), (2.0, 2.0)),
                physical_b_deg=0.0,
                pivot_machine_xy=(0.0, 0.0),
            ),
            physical_machine_z_mm=6.0,
        )
    )
    events: list[str] = []
    window = Main.__new__(Main)
    window._coordinate_frame_registry = registry
    window._refresh_design_panel = lambda: events.append("refresh")
    monkeypatch.setattr(
        main_module.connection_flow,
        "publish_coordinate_frames",
        lambda _owner: events.append("publish"),
    )

    Main._on_design_contact_reference_ready(
        window,
        ContactReferenceToken(focused.frame_id, focused.version + 1),
        8.0,
    )

    current = registry.get(focused.frame_id)
    assert current is not None
    assert current.transform.a_zero_machine_mm is None
    assert events == []


def test_invalid_contact_completion_fails_closed_before_registry_effects() -> None:
    context = _lifecycle_context(z_ready=True)
    lifecycle = DesignRegistrationLifecycle()
    eligible = lifecycle.accept_first_contact(context, None)
    token = eligible.contact_token
    assert token is not None
    window = Main.__new__(Main)
    window._design_registration_lifecycle = lifecycle
    window._design_session = types.SimpleNamespace()
    window._coordinate_frame_registry = types.SimpleNamespace(
        get=lambda _frame_id: pytest.fail("registry must not be read")
    )
    window._design_registration_context = lambda **_kwargs: context
    window.design_layout_window = None

    Main._on_design_contact_reference_ready(window, token, "invalid")

    assert lifecycle.accept_first_contact(context, None, token=token).capture_contact


def test_contact_callback_eligibility_delegates_to_registration_lifecycle() -> None:
    context = _lifecycle_context(z_ready=True)
    calls: list[tuple[RegistrationContext, object, object]] = []
    lifecycle = types.SimpleNamespace(
        cancel=lambda _reason: RegistrationEffects(),
        accept_first_contact=lambda actual_context, a_mm, token=None: (
            calls.append((actual_context, a_mm, token))
            or RegistrationEffects(reason="Contact reference is unavailable.")
        )
    )
    record = types.SimpleNamespace(
        version=1,
        transform=types.SimpleNamespace(
            z_zero_machine_mm=6.0,
            a_zero_machine_mm=None,
        ),
        readiness={
            "Z": types.SimpleNamespace(available=True),
            "A": types.SimpleNamespace(available=False),
        },
    )
    window = Main.__new__(Main)
    window._design_session = types.SimpleNamespace(active_frame_id="design-frame")
    window._coordinate_frame_registry = types.SimpleNamespace(
        get=lambda _frame_id: record
    )
    window._design_registration_lifecycle = lifecycle
    window._design_registration_context = lambda **_kwargs: context

    callback = Main._design_contact_success_callback(
        window,
        types.SimpleNamespace(frame_id="design-frame", frame_version=1),
    )

    assert callback is None
    assert calls == [(context, None, None)]


def test_contact_callback_never_captures_after_active_frame_switch(
    tmp_path: Path,
) -> None:
    document = _make_document(tmp_path)
    registry = CoordinateFrameRegistry()
    focused = registry.add(
        set_focus_reference(
            commit_xyb_registration(
                new_design_frame_draft(document, existing_names=()),
                design_points=((0.0, 0.0), (1000.0, 0.0)),
                physical_machine_points=((1.0, 2.0), (2.0, 2.0)),
                physical_b_deg=0.0,
                pivot_machine_xy=(0.0, 0.0),
            ),
            physical_machine_z_mm=6.0,
        )
    )
    emitted: list[tuple[object, float]] = []
    window = Main.__new__(Main)
    window._design_registration_lifecycle = DesignRegistrationLifecycle()
    window._coordinate_frame_registry = registry
    window._design_session = DesignSession(document=document)
    window._design_session.link_active_frame(focused)
    window.stage_controller = types.SimpleNamespace(
        run_external_current_physical_machine_coordinates=lambda _axes: {"A": 8.0},
    )
    window.design_contact_reference_ready = types.SimpleNamespace(
        emit=lambda token, value: emitted.append((token, value))
    )
    callback = Main._design_contact_success_callback(
        window,
        main_module.snapshot_route_design_frame(
            frame_id=focused.frame_id,
            frame_version=focused.version,
        ),
    )
    assert callback is not None

    window._design_session.active_frame_id = "another-design-frame"
    callback(object())

    assert emitted == []


def test_contact_callback_captures_synchronized_physical_machine_a(
    tmp_path: Path,
) -> None:
    document = _make_document(tmp_path)
    registry = CoordinateFrameRegistry()
    focused = registry.add(
        set_focus_reference(
            commit_xyb_registration(
                new_design_frame_draft(document, existing_names=()),
                design_points=((0.0, 0.0), (1000.0, 0.0)),
                physical_machine_points=((1.0, 2.0), (2.0, 2.0)),
                physical_b_deg=0.0,
                pivot_machine_xy=(0.0, 0.0),
            ),
            physical_machine_z_mm=6.0,
        )
    )
    emitted: list[tuple[object, float]] = []
    window = Main.__new__(Main)
    window._design_registration_lifecycle = DesignRegistrationLifecycle()
    window._coordinate_frame_registry = registry
    window._design_session = DesignSession(document=document)
    window._design_session.link_active_frame(focused)
    window.stage_controller = types.SimpleNamespace(
        run_external_current_physical_machine_coordinates=lambda axes: (
            {"A": 9.25} if tuple(axes) == ("A",) else pytest.fail("wrong axes")
        )
    )
    window.design_contact_reference_ready = types.SimpleNamespace(
        emit=lambda token, value: emitted.append((token, value))
    )
    callback = Main._design_contact_success_callback(
        window,
        main_module.snapshot_route_design_frame(
            frame_id=focused.frame_id,
            frame_version=focused.version,
        ),
    )
    assert callback is not None

    callback(object())

    assert len(emitted) == 1
    token, physical_a = emitted[0]
    assert isinstance(token, ContactOperationToken)
    assert (token.frame_id, token.frame_version) == (
        focused.frame_id,
        focused.version,
    )
    assert physical_a == 9.25


def test_later_route_does_not_recapture_established_contact_reference(
    monkeypatch,
    tmp_path: Path,
) -> None:
    document = _make_document(tmp_path)
    registry = CoordinateFrameRegistry()
    focused = registry.add(
        set_focus_reference(
            commit_xyb_registration(
                new_design_frame_draft(document, existing_names=()),
                design_points=((0.0, 0.0), (1000.0, 0.0)),
                physical_machine_points=((1.0, 2.0), (2.0, 2.0)),
                physical_b_deg=0.0,
                pivot_machine_xy=(0.0, 0.0),
            ),
            physical_machine_z_mm=6.0,
        )
    )
    reads: list[tuple[str, ...]] = []
    window = Main.__new__(Main)
    window._design_registration_lifecycle = DesignRegistrationLifecycle()
    window._coordinate_frame_registry = registry
    window._design_session = DesignSession(document=document)
    window._design_session.link_active_frame(focused)
    window.stage_controller = types.SimpleNamespace(
        run_external_current_physical_machine_coordinates=lambda axes: (
            reads.append(tuple(axes)) or {"A": 8.0}
        )
    )
    window._refresh_design_panel = lambda: None
    window._show_status = lambda *_args: None
    window.design_contact_reference_ready = types.SimpleNamespace(
        emit=lambda token, value: Main._on_design_contact_reference_ready(
            window,
            token,
            value,
        )
    )
    monkeypatch.setattr(
        main_module.connection_flow,
        "publish_coordinate_frames",
        lambda _owner: None,
    )
    first_route_callback = Main._design_contact_success_callback(
        window,
        main_module.snapshot_route_design_frame(
            frame_id=focused.frame_id,
            frame_version=focused.version,
        ),
    )
    assert first_route_callback is not None

    first_route_callback(object())
    established = registry.get(focused.frame_id)
    assert established is not None
    later_route_callback = Main._design_contact_success_callback(
        window,
        main_module.snapshot_route_design_frame(
            frame_id=established.frame_id,
            frame_version=established.version,
        ),
    )

    assert later_route_callback is None
    assert registry.get(focused.frame_id) == established
    assert established.transform is not None
    assert established.transform.a_zero_machine_mm == 8.0
    assert reads == [("A",)]


def test_design_fov_size_is_returned_in_design_units() -> None:
    window = Main.__new__(Main)
    window._design_session = types.SimpleNamespace(
        registration=DesignRegistration.from_marks(
            ((0.0, 0.0), (1000.0, 0.0)),
            ((0.0, 0.0), (1.0, 0.0)),
            design_unit_mm=0.001,
        )
    )
    window.stage_controller = types.SimpleNamespace(
        current_fov_size_mm=lambda: (2.0, 3.0)
    )

    assert Main._resolve_design_fov_size(window) == pytest.approx((2000.0, 3000.0))


def test_find_focus_submits_visible_fixture_geometry_and_waits_for_worker(
    tmp_path: Path,
) -> None:
    document = _make_document(tmp_path)
    submitted: list[object] = []
    started: list[tuple[float, float]] = []
    displayed: list[tuple[str, object]] = []
    window = Main.__new__(Main)
    window._design_session = types.SimpleNamespace(document=document, active_frame_id=None)
    window._design_registration_lifecycle = DesignRegistrationLifecycle()
    window._resolve_design_fov_size = lambda: (20.0, 20.0)
    window._focus_structure_bounds_worker = types.SimpleNamespace(
        submit=submitted.append
    )
    window._focus_structure_request_id = 0
    window._start_design_focus_reference = started.append
    window._show_status = lambda *_args: None
    window.design_layout_window = types.SimpleNamespace(
        set_focus_candidate=lambda value: displayed.append(("candidate", value)),
        set_selected_focus_point=lambda value: displayed.append(("selected", value)),
    )

    Main._find_design_focus_reference(window)

    assert len(submitted) == 1
    request = submitted[0]
    assert request.config is None
    assert request.fixture_polygons
    assert started == []

    Main._on_focus_structure_bounds_ready(
        window,
        StructureBoundsResult(
            request_id=request.request_id,
            generation=request.generation,
            structure_bounds=((0.0, 0.0, 10.0, 10.0),),
        ),
    )

    assert started == []
    assert not hasattr(window, "_focus_candidate")
    assert displayed[-2][0] == "candidate"
    assert displayed[-2][1].center == (5.0, 5.0)
    assert displayed[-1] == ("selected", (5.0, 5.0))


def test_main_close_delegates_focus_retirement_to_transactional_shutdown(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    stop_timeouts: list[float] = []
    delegated: list[object] = []
    window = Main.__new__(Main)
    window._focus_structure_bounds_worker = types.SimpleNamespace(
        stop=lambda timeout_s: stop_timeouts.append(float(timeout_s))
    )
    window._clear_exact_step_targets = lambda: None
    monkeypatch.setattr(
        main_module.shutdown_ui,
        "close_event",
        lambda owner, event: delegated.append((owner, event)),
    )
    event = object()

    Main.closeEvent(window, event)

    assert stop_timeouts == []
    assert delegated == [(window, event)]


@pytest.mark.parametrize("changed_part", ["fov", "objective", "calibration"])
def test_focus_candidate_acceptance_rejects_stale_optical_context(
    changed_part: str,
) -> None:
    started: list[tuple[tuple[float, float], object]] = []
    statuses: list[str] = []
    context = {"value": _lifecycle_context()}
    lifecycle = DesignRegistrationLifecycle()
    lifecycle.set_focus_candidate(
        types.SimpleNamespace(center=(5.0, 5.0)),
        context["value"],
    )
    window = Main.__new__(Main)
    window._design_registration_lifecycle = lifecycle
    window._design_session = types.SimpleNamespace()
    window._design_focus_overlay_context_key = lambda: context["value"]
    window._start_design_focus_reference = lambda point, token: started.append(
        (point, token)
    )
    window._show_status = lambda message, _timeout: statuses.append(str(message))
    window.design_layout_window = None

    replacements = {
        "fov": replace(context["value"], fov_size=(30.0, 20.0)),
        "objective": replace(context["value"], objective_name="X20"),
        "calibration": replace(
            context["value"],
            optical_calibration_identity="changed-calibration",
        ),
    }
    context["value"] = replacements[changed_part]

    Main._use_selected_design_focus_reference(window, (5.0, 5.0))

    assert started == []
    assert not hasattr(window, "_focus_candidate")
    assert statuses == ["Find a new focus reference for the current view."]


def test_focus_candidate_moves_only_after_explicit_current_context_acceptance() -> None:
    started: list[tuple[tuple[float, float], object]] = []
    context = _lifecycle_context()
    lifecycle = DesignRegistrationLifecycle()
    lifecycle.set_focus_candidate(
        types.SimpleNamespace(center=(5.0, 5.0)),
        context,
    )
    window = Main.__new__(Main)
    window._design_registration_lifecycle = lifecycle
    window._design_session = types.SimpleNamespace()
    window._design_focus_overlay_context_key = lambda: context
    window._start_design_focus_reference = lambda point, token: started.append(
        (point, token)
    )

    Main._use_selected_design_focus_reference(window, (6.0, 7.0))

    assert started[0][0] == (6.0, 7.0)
    assert started[0][1] is not None


def test_focus_move_rejection_applies_one_lifecycle_cancellation_effect() -> None:
    context = _lifecycle_context()
    token = types.SimpleNamespace(request_id="focus-token")
    bound_effects = RegistrationEffects(accepted=True)
    cancellation_effects = RegistrationEffects(
        accepted=True,
        reason="Move was rejected.",
    )
    completions: list[FocusCompletion] = []

    class _Lifecycle:
        def accept_focus(
            self,
            actual_context: RegistrationContext,
            completion: FocusCompletion,
        ) -> RegistrationEffects:
            assert actual_context == context
            completions.append(completion)
            if completion.kind is FocusCompletionKind.TARGET_BOUND:
                return bound_effects
            assert completion.kind is FocusCompletionKind.CANCELLED
            return cancellation_effects

    window = Main.__new__(Main)
    window._design_registration_lifecycle = _Lifecycle()
    window._design_focus_overlay_context_key = lambda: context
    window._move_to_design_coordinate = (
        lambda _point, *, source_label, move_request: (
            source_label == "focus reference" and move_request(4.0, 5.0)
        )
    )
    window.stage_controller = types.SimpleNamespace(
        request_token_bound_move_to_xy=lambda *_args: False
    )
    window.design_registration_focus_move_finished = types.SimpleNamespace(
        emit=lambda *_args: None
    )
    applied: list[RegistrationEffects] = []
    window._apply_registration_effects = applied.append

    Main._start_design_focus_reference(window, (6.0, 7.0), token)

    assert [completion.kind for completion in completions] == [
        FocusCompletionKind.TARGET_BOUND,
        FocusCompletionKind.CANCELLED,
    ]
    assert applied == [bound_effects, cancellation_effects]


def test_focus_context_key_tracks_fov_objective_and_optical_calibration(
    tmp_path: Path,
) -> None:
    document = _make_document(tmp_path)
    fov = {"value": (20.0, 30.0)}
    profile_payload = {"pixels_to_mm": [[1.0, 0.0], [0.0, 1.0]]}
    objectives = types.SimpleNamespace(
        active_name="X5",
        objectives={
            "X5": types.SimpleNamespace(to_dict=lambda: dict(profile_payload)),
            "X20": types.SimpleNamespace(to_dict=lambda: {"pixels_to_mm": [[2.0]]}),
        },
    )
    window = Main.__new__(Main)
    window._design_session = types.SimpleNamespace(
        document=document,
        active_frame_id=None,
    )
    window._coordinate_frame_registry = types.SimpleNamespace(get=lambda _frame_id: None)
    window._resolve_design_fov_size = lambda: fov["value"]
    window.settings_manager = types.SimpleNamespace(
        objectives_configuration=lambda: objectives
    )

    initial = Main._design_focus_overlay_context_key(window)
    fov["value"] = (21.0, 30.0)
    changed_fov = Main._design_focus_overlay_context_key(window)
    objectives.active_name = "X20"
    changed_objective = Main._design_focus_overlay_context_key(window)
    objectives.active_name = "X5"
    profile_payload["pixels_to_mm"] = [[3.0, 0.0], [0.0, 3.0]]
    changed_calibration = Main._design_focus_overlay_context_key(window)

    assert changed_fov != initial
    assert changed_objective != changed_fov
    assert changed_calibration != initial


def test_stale_focus_structure_result_is_ignored_after_document_context_change(
    tmp_path: Path,
) -> None:
    document = _make_document(tmp_path)
    submitted: list[object] = []
    started: list[tuple[float, float]] = []
    session = types.SimpleNamespace(document=document, active_frame_id="frame-a")
    window = Main.__new__(Main)
    window._design_registration_lifecycle = DesignRegistrationLifecycle()
    window._design_session = session
    window._coordinate_frame_registry = types.SimpleNamespace(get=lambda _frame_id: None)
    window._resolve_design_fov_size = lambda: (20.0, 20.0)
    window._focus_structure_bounds_worker = types.SimpleNamespace(
        submit=submitted.append
    )
    window._focus_structure_request_id = 0
    window._start_design_focus_reference = started.append
    window._show_status = lambda *_args: None

    Main._find_design_focus_reference(window)
    request = submitted[0]
    session.active_frame_id = "frame-b"
    Main._on_focus_structure_bounds_ready(
        window,
        StructureBoundsResult(
            request_id=request.request_id,
            generation=request.generation,
            structure_bounds=((0.0, 0.0, 10.0, 10.0),),
        ),
    )

    assert started == []


@pytest.mark.parametrize(
    "changed_context",
    [
        ("new-document", "TOP", ((1, 0),), 0, "frame-a", 1),
        ("document", "ALT", ((1, 0),), 0, "frame-a", 1),
        ("document", "TOP", ((2, 0),), 0, "frame-a", 1),
        ("document", "TOP", ((1, 0),), 1, "frame-a", 1),
        ("document", "TOP", ((1, 0),), 0, "frame-b", 1),
        ("document", "TOP", ((1, 0),), 0, "frame-a", 2),
        None,
    ],
)
def test_focus_overlays_clear_for_every_design_context_change(changed_context) -> None:
    cleared: list[object] = []
    initial_context = ("document", "TOP", ((1, 0),), 0, "frame-a", 1)
    lifecycle = DesignRegistrationLifecycle()
    lifecycle.set_focus_candidate(
        types.SimpleNamespace(center=(5.0, 5.0)),
        initial_context,
    )
    window = Main.__new__(Main)
    window._design_registration_lifecycle = lifecycle
    window._design_session = types.SimpleNamespace()
    window._design_focus_overlay_context = initial_context
    window._pending_focus_structure_request_id = 4
    window.design_layout_window = types.SimpleNamespace(
        set_focus_candidate=lambda value: cleared.append(("candidate", value)),
        set_selected_focus_point=lambda value: cleared.append(("selected", value)),
    )
    window._design_focus_overlay_context_key = lambda: changed_context

    Main._synchronize_design_focus_overlay_context(window)

    assert not hasattr(window, "_focus_candidate")
    assert window._pending_focus_structure_request_id is None
    assert cleared == [("candidate", None), ("selected", None)]
    assert lifecycle.accept_focus(initial_context).accepted is False


def test_replacing_committed_source_marks_starts_fresh_draft_before_capture(
    monkeypatch,
    tmp_path: Path,
) -> None:
    document = _make_document(tmp_path)
    registry = CoordinateFrameRegistry()
    committed = registry.add(
        main_module.commit_xyb_registration(
            new_design_frame_draft(document, existing_names=()),
            design_points=((0.0, 0.0), (1000.0, 0.0)),
            physical_machine_points=((1.0, 2.0), (2.0, 2.0)),
            physical_b_deg=0.0,
            pivot_machine_xy=(0.0, 0.0),
        )
    )
    session = DesignSession(document=document)
    session.link_active_frame(committed)
    snapshots = iter(
        (
            _machine_snapshot((10.0, 20.0, 0.0, 0.0, 5.0)),
            _machine_snapshot((11.0, 20.0, 0.0, 0.0, 5.0)),
        )
    )
    requests: list[object] = []
    current_snapshot = {"value": _machine_snapshot((0.0, 0.0, 0.0, 0.0, 5.0))}
    window = Main.__new__(Main)
    window._design_session = session
    window._coordinate_frame_registry = registry
    _set_rotation_settings(window)
    window._coordinate_frames_loaded = True
    window._active_design_frame_metadata = main_module.DesignFrameMetadata.from_mapping(
        committed.metadata
    )
    window._manual_alignment_pick_slot = None
    window._manual_alignment_points = [None, None]
    window._pending_alignment_preparation = None
    window._last_selected_design_point = None
    window.stage_controller = types.SimpleNamespace(
        request_machine_coordinate_snapshot=lambda token, *, axes: (
            requests.append(token) or True
        ),
        latest_machine_coordinate_snapshot=lambda: current_snapshot["value"],
    )
    window._camera_stage_xy_from_raw_stage_xy = lambda point: point
    window._set_design_snap_enabled = lambda _enabled: None
    window._refresh_design_panel = lambda: None
    window._refresh_design_position = lambda: None
    window._set_alignment_panel_expanded = lambda: None
    window._show_status = lambda *_args: None
    window._apply_coordinate_frame_authority_blocks = lambda: None
    monkeypatch.setattr(
        main_module.connection_flow,
        "publish_coordinate_frames",
        lambda _owner: None,
    )

    Main._on_design_layout_point_selected(window, 0, 100.0, 200.0)
    Main._on_design_layout_point_selected(window, 1, 1100.0, 200.0)
    replacement_id = session.active_frame_id
    Main._capture_stage_source_mark(window)
    first_snapshot = next(snapshots)
    current_snapshot["value"] = first_snapshot
    Main._on_registration_machine_coordinate_snapshot_finished(
        window,
        requests.pop(),
        True,
        first_snapshot,
        "",
    )

    first_capture = registry.get(replacement_id)
    assert replacement_id != committed.frame_id
    assert first_capture is not None
    assert not first_capture.readiness["X"].available

    Main._capture_stage_source_mark(window)
    second_snapshot = next(snapshots)
    current_snapshot["value"] = second_snapshot
    Main._on_registration_machine_coordinate_snapshot_finished(
        window,
        requests.pop(),
        True,
        second_snapshot,
        "",
    )

    replacement = registry.get(replacement_id)
    assert replacement is not None
    metadata = main_module.DesignFrameMetadata.from_mapping(replacement.metadata)
    assert metadata.source_design_marks == ((100.0, 200.0), (1100.0, 200.0))
    assert metadata.source_machine_marks == ((10.0, 20.0), (11.0, 20.0))
    assert replacement.readiness["X"].available
    assert registry.get(committed.frame_id) == committed


def test_changing_source_mark_discards_unpaired_physical_capture(
    monkeypatch,
    tmp_path: Path,
) -> None:
    document = _make_document(tmp_path)
    registry = CoordinateFrameRegistry()
    draft = registry.add(new_design_frame_draft(document, existing_names=()))
    session = DesignSession(document=document)
    session.link_active_frame(draft, machine_b_deg=5.0, pivot_machine_xy=(0.0, 0.0))
    session.set_source_design_mark(0, (0.0, 0.0))
    session.set_source_design_mark(1, (1000.0, 0.0))
    stale_physical = (10.0, 20.0)
    requests: list[object] = []
    window = Main.__new__(Main)
    window._design_session = session
    window._coordinate_frame_registry = registry
    window._coordinate_frames_loaded = True
    window._active_design_frame_metadata = main_module.DesignFrameMetadata.from_document(
        document
    )
    window._manual_alignment_pick_slot = None
    window._manual_alignment_points = [None, None]
    window._pending_alignment_preparation = None
    window._last_selected_design_point = None
    _set_rotation_settings(window)
    window.stage_controller = types.SimpleNamespace(
        request_machine_coordinate_snapshot=lambda token, *, axes: (
            requests.append(token) or True
        )
    )
    window._camera_stage_xy_from_raw_stage_xy = lambda point: point
    window._design_navigation_xy_from_physical_machine_xy = lambda point: point
    window._set_design_snap_enabled = lambda _enabled: None
    window._refresh_design_panel = lambda: None
    window._refresh_design_position = lambda: None
    window._set_alignment_panel_expanded = lambda: None
    window._show_status = lambda *_args: None
    monkeypatch.setattr(
        main_module.connection_flow,
        "publish_coordinate_frames",
        lambda _owner: None,
    )

    Main._capture_stage_source_mark(window)
    Main._on_registration_machine_coordinate_snapshot_finished(
        window,
        requests.pop(),
        True,
        _machine_snapshot((*stale_physical, 0.0, 0.0, 5.0)),
        "",
    )
    assert session.source_stage_marks == (stale_physical,)

    Main._on_design_layout_point_selected(window, 0, 100.0, 200.0)
    Main._capture_stage_source_mark(window)
    fresh_snapshot = _machine_snapshot((11.0, 20.0, 0.0, 0.0, 5.0))
    Main._on_registration_machine_coordinate_snapshot_finished(
        window,
        requests.pop(),
        True,
        fresh_snapshot,
        "",
    )

    unchanged = registry.get(draft.frame_id)
    assert unchanged is not None
    metadata = main_module.DesignFrameMetadata.from_mapping(unchanged.metadata)
    assert metadata.source_machine_marks == ()
    assert not unchanged.readiness["X"].available
    assert session.source_stage_marks == ((11.0, 20.0),)
    assert not hasattr(window, "_pending_registration_physical_marks")
    assert not hasattr(window, "_pending_registration_mark_capture")


def test_top_cell_switch_links_the_matching_persistent_frame(
    monkeypatch,
    tmp_path: Path,
) -> None:
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
    top_metadata = main_module.DesignFrameMetadata.from_document(top_document)
    alt_metadata = replace(top_metadata, top_cell_name="ALT")
    registry = CoordinateFrameRegistry()
    top_frame = registry.add(
        new_design_frame_draft(
            top_document,
            existing_names=(),
            metadata=top_metadata,
        )
    )
    alt_frame = registry.add(
        new_design_frame_draft(
            alt_document,
            existing_names=(top_frame.name,),
            metadata=alt_metadata,
        )
    )
    session = DesignSession(document=top_document)
    session.link_active_frame(top_frame)
    window = Main.__new__(Main)
    window._design_session = session
    window._coordinate_frame_registry = registry
    window._coordinate_frames_loaded = True
    window._active_design_frame_metadata = top_metadata
    _set_rotation_settings(window)
    window.stage_controller = types.SimpleNamespace(
        latest_machine_coordinate_snapshot=lambda: _machine_snapshot(
            (0.0, 0.0, 0.0, 0.0, 0.0)
        )
    )
    window._pending_alignment_preparation = None
    window._last_selected_design_point = None
    window._design_load_pending = False
    window._route_measurement_thread = None
    window._route_measurement_session_active = False
    cancellations: list[RegistrationCancellation] = []
    window._design_registration_lifecycle = types.SimpleNamespace(
        cancel=lambda reason: (
            cancellations.append(reason) or RegistrationEffects()
        )
    )
    window._refresh_design_panel = lambda: None
    window._refresh_design_position = lambda: None
    window._show_navigation_status = lambda _plan: None
    window._show_status = lambda *_args: None
    window._apply_coordinate_frame_authority_blocks = lambda: None
    monkeypatch.setattr(
        main_module.connection_flow,
        "publish_coordinate_frames",
        lambda _owner: None,
    )

    Main._set_design_top_cell(window, "ALT")

    assert session.document is not None
    assert session.document.top_cell_name == "ALT"
    assert session.active_frame_id == alt_frame.frame_id
    assert window._active_design_frame_metadata.top_cell_name == "ALT"
    assert cancellations == [RegistrationCancellation.TOP_CELL_CHANGED]
    assert not hasattr(window, "_pending_registration_physical_marks")
    assert not hasattr(window, "_pending_registration_mark_capture")


def test_registration_instance_switch_new_and_route_lineage_preserve_records(
    monkeypatch,
    tmp_path: Path,
) -> None:
    document = _make_document(tmp_path)
    registry = CoordinateFrameRegistry()
    first = registry.add(
        commit_xyb_registration(
            new_design_frame_draft(document, existing_names=()),
            design_points=((0.0, 0.0), (1000.0, 0.0)),
            physical_machine_points=((1.0, 2.0), (2.0, 2.0)),
            physical_b_deg=0.0,
            pivot_machine_xy=(0.0, 0.0),
        )
    )
    second = registry.add(
        commit_xyb_registration(
            new_design_frame_draft(document, existing_names=(first.name,)),
            design_points=((0.0, 0.0), (1000.0, 0.0)),
            physical_machine_points=((10.0, 20.0), (11.0, 20.0)),
            physical_b_deg=0.0,
            pivot_machine_xy=(0.0, 0.0),
        )
    )
    session = DesignSession(document=document)
    session.link_active_frame(first)
    window = Main.__new__(Main)
    window._design_session = session
    window._coordinate_frame_registry = registry
    window._coordinate_frames_loaded = True
    window._active_design_frame_metadata = main_module.DesignFrameMetadata.from_document(
        document
    )
    window._design_load_pending = False
    window._route_measurement_thread = None
    window._pending_alignment_preparation = None
    window._last_selected_design_point = None
    cancellations: list[RegistrationCancellation] = []
    window._design_registration_lifecycle = types.SimpleNamespace(
        cancel=lambda reason: (
            cancellations.append(reason) or RegistrationEffects()
        )
    )
    window._design_focus_overlay_context = object()
    window.design_layout_window = None
    _set_rotation_settings(window)
    window.stage_controller = types.SimpleNamespace(
        latest_machine_coordinate_snapshot=lambda: _machine_snapshot(
            (0.0, 0.0, 0.0, 0.0, 0.0)
        )
    )
    window._design_navigation_xy_from_physical_machine_xy = lambda point: point
    window._refresh_design_panel = lambda: None
    window._refresh_design_position = lambda: None
    window._show_status = lambda *_args: None
    window._apply_coordinate_frame_authority_blocks = lambda: None
    window._set_design_snap_enabled = lambda _enabled: None
    monkeypatch.setattr(
        main_module.connection_flow,
        "publish_coordinate_frames",
        lambda _owner: None,
    )
    original_records = registry.snapshot().records

    window._route_measurement_thread = types.SimpleNamespace(is_alive=lambda: True)
    Main._select_design_registration_instance(window, second.frame_id)
    Main._new_design_registration_instance(window)
    assert session.active_frame_id == first.frame_id
    assert registry.snapshot().records == original_records
    assert cancellations == []
    window._route_measurement_thread = None

    Main._select_design_registration_instance(window, second.frame_id)

    assert session.active_frame_id == second.frame_id
    assert cancellations == [RegistrationCancellation.FRAME_CHANGED]
    assert not hasattr(window, "_pending_registration_physical_marks")
    assert np.allclose(session.source_stage_marks, ((10.0, 20.0), (11.0, 20.0)))
    assert Main._snapshot_active_route_design_frame(window) == (
        main_module.snapshot_route_design_frame(
            frame_id=second.frame_id,
            frame_version=second.version,
        )
    )
    assert registry.snapshot().records == original_records
    assert not hasattr(window, "_focus_candidate")

    Main._select_design_registration_instance(window, first.frame_id)

    assert session.active_frame_id == first.frame_id
    assert np.allclose(session.source_stage_marks, ((1.0, 2.0), (2.0, 2.0)))
    assert registry.snapshot().records == original_records
    assert cancellations == [
        RegistrationCancellation.FRAME_CHANGED,
        RegistrationCancellation.FRAME_CHANGED,
    ]

    Main._new_design_registration_instance(window)

    assert len(registry.snapshot().records) == 3
    assert session.active_frame_id not in {first.frame_id, second.frame_id}
    assert cancellations == [
        RegistrationCancellation.FRAME_CHANGED,
        RegistrationCancellation.FRAME_CHANGED,
        RegistrationCancellation.FRAME_CHANGED,
    ]
    assert not hasattr(window, "_pending_registration_physical_marks")


def test_missing_selected_registration_clears_session_without_deleting_others(
    tmp_path: Path,
) -> None:
    document = _make_document(tmp_path)
    registry = CoordinateFrameRegistry()
    first = registry.add(new_design_frame_draft(document, existing_names=()))
    second = registry.add(
        new_design_frame_draft(document, existing_names=(first.name,))
    )
    session = DesignSession(document=document)
    session.link_active_frame(first)
    window = Main.__new__(Main)
    window._design_registration_lifecycle = DesignRegistrationLifecycle()
    window._design_session = session
    window._coordinate_frame_registry = registry
    window.design_layout_window = None
    registry.reset((second,))

    Main._reconcile_missing_design_registration_instance(window)

    assert session.active_frame_id is None
    assert registry.snapshot().records == (second,)
    assert not hasattr(window, "_focus_candidate")


def _make_mixed_edit_window(tmp_path: Path) -> tuple[Main, list[str]]:
    window, _stage, statuses = _make_window()
    document = _make_document(tmp_path)
    window._design_session.load_document(document)
    route = MeasurementRoute.default_for_document(document)
    route_point = route.add_point((2.0, 3.0))
    window._design_session.route = route
    markup = MarkupDocument.empty(document.path).append_guide(
        (0.0, 0.0),
        (1.0, 0.0),
        guide_id="guide-1",
    )
    window._design_markup = markup
    window._design_markup_direct_guide_ids = ["guide-1"]
    window._route_measurement_thread = None
    selection = SelectionModel(
        frozenset(
            {
                route_entity_id(route_point.id),
                markup_entity_id("guide-1"),
            }
        )
    )
    window.design_layout_window = types.SimpleNamespace(
        selection=selection,
        set_selection=lambda value: statuses.append(f"selection:{len(value.ids)}"),
        set_markup=lambda value: statuses.append(f"markup:{len(value.guides)}"),
        set_guide_undo_available=lambda value: statuses.append(f"undo:{value}"),
        set_route_edit_enabled=lambda value: statuses.append(f"edit:{value}"),
    )
    window._publish_design_markup = lambda: statuses.append("publish_markup")
    return window, statuses


def _pending_markup_context(
    document: DesignDocument,
    *,
    generation: int = 1,
    previous_markup: MarkupDocument | None = None,
) -> main_module._PendingDesignMarkupLoad:
    candidate_session = DesignSession()
    plan = main_module.design_navigation.design_document_loaded_plan(
        candidate_session,
        document,
        None,
        None,
    )
    assert plan.accepted
    return main_module._PendingDesignMarkupLoad(
        generation=generation,
        session=candidate_session,
        plan=plan,
        show_window=False,
        previous_markup=previous_markup,
    )


def test_mixed_delete_commits_route_and_markup_together(tmp_path: Path) -> None:
    window, statuses = _make_mixed_edit_window(tmp_path)

    Main._delete_design_selection(window)

    assert window._design_session.route is not None
    assert window._design_session.route.points == []
    assert window._design_markup is not None
    assert window._design_markup.guides == ()
    assert "publish_markup" in statuses
    assert "selection:0" in statuses


def test_route_running_blocks_even_markup_only_delete(tmp_path: Path) -> None:
    window, statuses = _make_mixed_edit_window(tmp_path)
    window.design_layout_window.selection = SelectionModel(
        frozenset({markup_entity_id("guide-1")})
    )
    window._route_measurement_thread = types.SimpleNamespace(is_alive=lambda: True)

    Main._delete_design_selection(window)

    assert window._design_markup is not None
    assert [guide.id for guide in window._design_markup.guides] == ["guide-1"]
    assert window._design_session.route is not None
    assert len(window._design_session.route.points) == 1
    assert "publish_markup" not in statuses
    assert "Design editing is locked." in statuses


def test_pending_design_load_blocks_old_route_mutation(tmp_path: Path) -> None:
    window, _statuses = _make_mixed_edit_window(tmp_path)
    window._design_markup_load_request_id = 99
    window._design_load_pending = True

    Main._remove_selected_route_point(window)

    assert window._design_session.route is not None
    assert len(window._design_session.route.points) == 1


def test_mixed_array_keeps_sources_and_copies_both_entity_types(tmp_path: Path) -> None:
    window, statuses = _make_mixed_edit_window(tmp_path)
    original_selection = window.design_layout_window.selection
    request = MixedArrayRequest(
        direction_1=(10.0, 0.0),
        count_1=2,
        direction_2=(0.0, 5.0),
        count_2=1,
        source_ids=original_selection.ids,
    )

    Main._apply_mixed_design_array(window, request)

    assert window._design_session.route is not None
    assert [point.camera_center for point in window._design_session.route.points] == [
        (2.0, 3.0),
        (12.0, 3.0),
    ]
    assert window._design_markup is not None
    assert [(guide.start, guide.end) for guide in window._design_markup.guides] == [
        ((0.0, 0.0), (1.0, 0.0)),
        ((10.0, 0.0), (11.0, 0.0)),
    ]
    assert "publish_markup" in statuses
    assert f"selection:{len(original_selection.ids)}" in statuses


def test_rotate_design_transforms_and_persists_route_and_markup_together(
    tmp_path: Path,
) -> None:
    window, statuses = _make_mixed_edit_window(tmp_path)

    Main._rotate_design_document(window, 1)

    assert window._design_session.document is not None
    assert window._design_session.document.rotation_quarter_turns == 1
    assert window._design_session.route is not None
    assert [point.camera_center for point in window._design_session.route.points] == [
        (7.0, 2.0),
    ]
    assert window._design_markup is not None
    assert [(guide.start, guide.end) for guide in window._design_markup.guides] == [
        ((10.0, 0.0), (10.0, 1.0)),
    ]
    assert "publish_markup" in statuses


@pytest.mark.parametrize(
    ("choice", "expected_guides", "expected_event"),
    [
        (MarkupLoadChoice.KEEP, 1, "publish_markup"),
        (MarkupLoadChoice.START_EMPTY, 0, "delete_markup"),
    ],
)
def test_changed_markup_choice_rebinds_or_discards_saved_guides(
    monkeypatch,
    tmp_path: Path,
    choice: MarkupLoadChoice,
    expected_guides: int,
    expected_event: str,
) -> None:
    window, _stage, statuses = _make_window()
    document = _make_document(tmp_path)
    saved = MarkupDocument.empty(document.path).append_guide(
        (0.0, 0.0),
        (1.0, 0.0),
        guide_id="saved-guide",
    )
    document.path.write_bytes(b"changed-after-markup")
    window._design_markup_load_request_id = 7
    window._design_markup_load_contexts[7] = _pending_markup_context(document)
    window._prompt_changed_markup_choice = lambda _path: choice
    window._publish_design_markup = lambda: statuses.append("publish_markup")
    window._delete_persisted_design_markup = (
        lambda _path: statuses.append("delete_markup")
    )
    monkeypatch.setattr(
        main_module.connection_flow,
        "persist_controller_state_if_available",
        lambda _owner: statuses.append("persisted"),
    )

    Main._on_design_markup_loaded(
        window,
        StoreLoadResult(7, saved.source_path, saved),
    )

    assert window._design_markup is not None
    assert len(window._design_markup.guides) == expected_guides
    assert window._design_markup.matches_source(document.path)
    assert expected_event in statuses


def test_changed_markup_cancel_restores_preload_design(
    monkeypatch,
    tmp_path: Path,
) -> None:
    window, _stage, statuses = _make_window()
    previous_document = _make_document(tmp_path / "previous")
    previous_session = DesignSession()
    previous_session.load_document(previous_document)
    previous_markup = MarkupDocument.empty(previous_document.path).append_guide(
        (0.0, 0.0),
        (1.0, 0.0),
        guide_id="previous-guide",
    )
    new_document = _make_document(tmp_path / "new")
    saved = MarkupDocument.empty(new_document.path).append_guide(
        (2.0, 0.0),
        (3.0, 0.0),
        guide_id="new-guide",
    )
    new_document.path.write_bytes(b"changed")
    window._design_session = previous_session
    window._design_markup = previous_markup
    window._design_markup_load_request_id = 9
    window._design_markup_load_contexts[9] = _pending_markup_context(
        new_document,
        previous_markup=previous_markup,
    )
    window._prompt_changed_markup_choice = (
        lambda _path: MarkupLoadChoice.CANCEL
    )
    monkeypatch.setattr(
        main_module.connection_flow,
        "persist_controller_state_if_available",
        lambda _owner: statuses.append("persisted"),
    )

    Main._on_design_markup_loaded(
        window,
        StoreLoadResult(9, saved.source_path, saved),
    )

    assert window._design_session is previous_session
    assert window._design_markup is previous_markup
    assert "Design load canceled." in statuses
    assert "persisted" not in statuses


def test_changed_markup_cancel_never_commits_or_clears_preload_ui_state(
    monkeypatch,
    tmp_path: Path,
) -> None:
    window, _stage, statuses = _make_window()
    previous_document = _make_document(tmp_path / "previous-live")
    window._design_session.load_document(previous_document)
    previous_markup = MarkupDocument.empty(previous_document.path).append_guide(
        (0.0, 0.0),
        (1.0, 0.0),
        guide_id="previous-guide",
    )
    window._design_markup = previous_markup
    window._design_markup_direct_guide_ids = ["previous-guide"]
    window._last_selected_design_point = (4.0, 5.0)
    previous_selection = SelectionModel(
        frozenset({markup_entity_id("previous-guide")})
    )
    previewed: list[DesignDocument] = []
    finished_previews: list[DesignDocument | None] = []
    pending_states: list[bool] = []
    layout = types.SimpleNamespace(
        selection=previous_selection,
        set_document_preview=previewed.append,
        finish_document_preview=finished_previews.append,
        set_design_load_pending=pending_states.append,
        set_markup=lambda markup: setattr(
            layout,
            "selection",
            (
                previous_selection
                if markup is previous_markup
                else SelectionModel()
            ),
        ),
        set_guide_undo_available=lambda _value: None,
    )
    window.design_layout_window = layout
    loads: list[tuple[int, Path]] = []
    window._design_markup_store = types.SimpleNamespace(
        load=lambda request_id, path: loads.append((request_id, Path(path)))
    )
    window._begin_design_markup_load = types.MethodType(
        Main._begin_design_markup_load,
        window,
    )
    window._design_load_show_window[1] = True
    new_document = _make_document(tmp_path / "new-cancelled")
    saved = MarkupDocument.empty(new_document.path).append_guide(
        (2.0, 0.0),
        (3.0, 0.0),
        guide_id="new-guide",
    )
    new_document.path.write_bytes(b"changed-after-save")
    window._prompt_changed_markup_choice = (
        lambda _path: MarkupLoadChoice.CANCEL
    )
    monkeypatch.setattr(
        main_module.connection_flow,
        "persist_controller_state_if_available",
        lambda _owner: statuses.append("persisted"),
    )

    Main._on_design_document_loaded(window, 1, new_document, None)

    assert window._design_session.document is previous_document
    assert window._design_markup is previous_markup
    assert window._design_markup_direct_guide_ids == ["previous-guide"]
    assert window._last_selected_design_point == (4.0, 5.0)
    assert layout.selection == previous_selection
    assert previewed == [new_document]
    assert pending_states[-1]
    request_id = loads[-1][0]

    Main._on_design_markup_loaded(
        window,
        StoreLoadResult(request_id, saved.source_path, saved),
    )

    assert window._design_session.document is previous_document
    assert window._design_markup is previous_markup
    assert window._design_markup_direct_guide_ids == ["previous-guide"]
    assert window._last_selected_design_point == (4.0, 5.0)
    assert layout.selection == previous_selection
    assert finished_previews == [previous_document]
    assert not pending_states[-1]


def test_newer_design_load_invalidates_older_markup_response(
    monkeypatch,
    tmp_path: Path,
) -> None:
    window, _stage, _statuses = _make_window()
    previous_document = _make_document(tmp_path / "previous-generation")
    window._design_session.load_document(previous_document)
    loads: list[tuple[int, Path]] = []
    window._design_markup_store = types.SimpleNamespace(
        load=lambda request_id, path: loads.append((request_id, Path(path)))
    )
    window._begin_design_markup_load = types.MethodType(
        Main._begin_design_markup_load,
        window,
    )
    previewed: list[DesignDocument] = []
    window.design_layout_window = types.SimpleNamespace(
        set_document_preview=previewed.append,
        finish_document_preview=lambda _document: None,
        set_design_load_pending=lambda _pending: None,
    )
    monkeypatch.setattr(
        main_module,
        "toggle_design_layout_window",
        lambda *_args: None,
    )
    monkeypatch.setattr(
        main_module.threading,
        "Thread",
        lambda **_kwargs: types.SimpleNamespace(start=lambda: None),
    )
    monkeypatch.setattr(
        main_module.connection_flow,
        "persist_controller_state_if_available",
        lambda _owner: None,
    )
    first_document = _make_document(tmp_path / "first-generation")
    second_document = _make_document(tmp_path / "second-generation")
    window._design_load_show_window[1] = False

    Main._on_design_document_loaded(window, 1, first_document, None)
    first_request_id = loads[-1][0]

    Main._start_design_document_load(
        window,
        str(second_document.path),
        restore_state=None,
        show_window=False,
    )
    Main._on_design_markup_loaded(
        window,
        StoreLoadResult(
            first_request_id,
            first_document.path,
            MarkupDocument.empty(first_document.path),
        ),
    )

    assert window._design_session.document is previous_document

    Main._on_design_document_loaded(window, 2, second_document, None)
    second_request_id = loads[-1][0]
    Main._on_design_markup_loaded(
        window,
        StoreLoadResult(
            second_request_id,
            second_document.path,
            MarkupDocument.empty(second_document.path),
        ),
    )

    assert window._design_session.document is second_document
    assert previewed == []


def test_markup_read_failure_commits_the_already_visible_design(
    monkeypatch,
    tmp_path: Path,
) -> None:
    window, _stage, _statuses = _make_window()
    previous_document = _make_document(tmp_path / "previous-read-failure")
    window._design_session.load_document(previous_document)
    loads: list[tuple[int, Path]] = []
    window._design_markup_store = types.SimpleNamespace(
        load=lambda request_id, path: loads.append((request_id, Path(path)))
    )
    window._begin_design_markup_load = types.MethodType(
        Main._begin_design_markup_load,
        window,
    )
    previewed: list[DesignDocument] = []
    finished: list[DesignDocument | None] = []
    pending: list[bool] = []
    window.design_layout_window = types.SimpleNamespace(
        set_document_preview=previewed.append,
        finish_document_preview=finished.append,
        set_design_load_pending=pending.append,
        set_status_message=lambda _message: None,
    )
    monkeypatch.setattr(
        main_module.connection_flow,
        "persist_controller_state_if_available",
        lambda _owner: None,
    )
    monkeypatch.setattr(
        main_module,
        "toggle_design_layout_window",
        lambda *_args: None,
    )
    document = _make_document(tmp_path / "visible-despite-read-failure")
    window._design_load_show_window[1] = True

    Main._on_design_document_loaded(window, 1, document, None)
    request_id = loads[-1][0]

    Main._on_design_markup_store_failed(
        window,
        types.SimpleNamespace(
            request_id=request_id,
            operation="load",
            source_path=document.path,
            message="read failed",
        ),
    )

    assert previewed == [document]
    assert finished == [document]
    assert window._design_session.document is document
    assert window._design_markup == MarkupDocument.empty(document.path)
    assert not pending[-1]


def test_start_empty_deletes_sidecar_even_with_pending_visibility(
    monkeypatch,
    tmp_path: Path,
) -> None:
    window, _stage, statuses = _make_window()
    document = _make_document(tmp_path)
    saved = MarkupDocument.empty(document.path).append_guide(
        (0.0, 0.0),
        (1.0, 0.0),
    )
    document.path.write_bytes(b"changed-after-markup")
    window._design_markup_load_request_id = 11
    window._design_markup_load_contexts[11] = _pending_markup_context(document)
    window._design_markup_pending_visibility = False
    window._prompt_changed_markup_choice = (
        lambda _path: MarkupLoadChoice.START_EMPTY
    )
    window._publish_design_markup = lambda: statuses.append("publish_markup")
    window._delete_persisted_design_markup = (
        lambda _path: statuses.append("delete_markup")
    )
    monkeypatch.setattr(
        main_module.connection_flow,
        "persist_controller_state_if_available",
        lambda _owner: None,
    )

    Main._on_design_markup_loaded(
        window,
        StoreLoadResult(11, saved.source_path, saved),
    )

    assert "delete_markup" in statuses
    assert "publish_markup" not in statuses


def test_design_unload_delegates_registration_lifecycle_once_and_deletes_markup(
    tmp_path: Path,
) -> None:
    window, _stage, statuses = _make_window()
    document = _make_document(tmp_path)
    window._design_session.load_document(document)
    window._design_markup = MarkupDocument.empty(document.path).append_guide(
        (0.0, 0.0),
        (1.0, 0.0),
    )
    window._delete_persisted_design_markup = (
        lambda path: statuses.append(f"delete:{Path(path).name}")
    )
    window._update_design_position = lambda value: statuses.append(
        f"position:{value}"
    )
    cancellations: list[RegistrationCancellation] = []
    lifecycle_effects = RegistrationEffects()
    applied: list[RegistrationEffects] = []
    window._design_registration_lifecycle = types.SimpleNamespace(
        cancel=lambda reason: (
            cancellations.append(reason) or lifecycle_effects
        )
    )
    window._apply_registration_effects = applied.append

    Main._unload_design_document(window)

    assert window._design_session.document is None
    assert window._design_markup is None
    assert cancellations == [RegistrationCancellation.DOCUMENT_UNLOADED]
    assert applied == [lifecycle_effects]
    assert not hasattr(window, "_pending_registration_physical_marks")
    assert not hasattr(window, "_pending_registration_mark_capture")
    assert "delete:loaded.gds" in statuses


def test_empty_markup_visibility_is_persisted_as_a_snapshot(tmp_path: Path) -> None:
    window, _stage, _statuses = _make_window()
    document = _make_document(tmp_path)
    markup = MarkupDocument.empty(document.path, visible=False)
    operations: list[tuple[str, object]] = []
    window._design_markup = markup
    window._design_markup_store = types.SimpleNamespace(
        publish=lambda request_id, value: operations.append(
            ("publish", (request_id, value))
        ),
        delete=lambda request_id, path: operations.append(
            ("delete", (request_id, path))
        ),
    )

    Main._publish_design_markup(window)

    assert operations == [("publish", (1, markup))]


def test_clear_all_deletes_sidecar_instead_of_saving_empty_markup(
    tmp_path: Path,
) -> None:
    window, statuses = _make_mixed_edit_window(tmp_path)
    window._delete_persisted_design_markup = (
        lambda path: statuses.append(f"delete:{Path(path).name}")
    )
    window._publish_design_markup = lambda: statuses.append("publish_markup")

    Main._clear_design_guides(window)

    assert window._design_markup is not None
    assert window._design_markup.guides == ()
    assert "delete:loaded.gds" in statuses
    assert "publish_markup" not in statuses


def test_move_to_design_coordinate_seeds_prediction_and_requests_move_after_acceptance() -> None:
    window, stage_controller, statuses = _make_window()
    window._design_session.load_document(
        DesignDocument(
            path=Path("C:/designs/sample.gds"),
            library=object(),
            top_cell=object(),
            top_cell_name="TOP",
            cell_names=("TOP",),
            dbu=1e-6,
            user_unit=1e-9,
            bounds=(0.0, 0.0, 1.0, 1.0),
            polygons_by_layer={(1, 0): (np.asarray([[0.0, 0.0], [1.0, 0.0]]),)},
            visible_layers=frozenset({(1, 0)}),
        )
    )

    accepted = Main._move_to_design_coordinate(
        window,
        (100.0, 200.0),
        source_label="design window",
    )

    assert accepted
    assert statuses == ["refresh_panel", "clear_prediction"]
    assert window._last_selected_design_point == (100.0, 200.0)
    assert window._pending_planned_move_target_xy == (1.5, -2.0)
    assert window._pending_planned_move_source_label == "design window"
    assert stage_controller.move_requests == [(1.5, -2.0)]


def test_move_to_design_coordinate_reports_busy_race_rejection() -> None:
    window, _stage_controller, _statuses = _make_window()
    window._design_session.load_document(
        DesignDocument(
            path=Path("C:/designs/sample.gds"),
            library=object(),
            top_cell=object(),
            top_cell_name="TOP",
            cell_names=("TOP",),
            dbu=1e-6,
            user_unit=1e-9,
            bounds=(0.0, 0.0, 1.0, 1.0),
            polygons_by_layer={(1, 0): (np.asarray([[0.0, 0.0], [1.0, 0.0]]),)},
            visible_layers=frozenset({(1, 0)}),
        )
    )

    accepted = Main._move_to_design_coordinate(
        window,
        (100.0, 200.0),
        source_label="focus reference",
        move_request=lambda _x, _y: False,
    )

    assert accepted is False
    assert window._pending_planned_move_target_xy is None
    assert window._pending_planned_move_source_label is None
