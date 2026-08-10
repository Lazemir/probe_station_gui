from __future__ import annotations

import os
import threading
import time
import types
from dataclasses import replace
from pathlib import Path

import numpy as np
import pytest
from PySide6.QtCore import QObject, Qt, Signal
from PySide6.QtWidgets import QApplication

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from tests.app.import_reset import restore_real_imports_for_main


restore_real_imports_for_main()

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
from probe_station_gui.design.session import DesignSession
from probe_station_gui.coordinates.coordinator import CoordinateSystemCoordinator
from probe_station_gui.coordinates.registry import CoordinateFrameRegistry
from probe_station_gui.coordinates.coordinator_model import (
    AutofocusResult,
    CoordinateAdapterCompletion,
    CoordinateSystemSnapshot,
    CoordinateTransition,
    FirstContactRequest,
    FocusMoveResult,
    MachinePoseCaptureResult,
    MachineProfileObservation,
    PhysicalAReadResult,
    ReadPhysicalAIntent,
    RegistrationCaptureRequest,
    RegistrationOpticalObservation,
    RegistrationWorkflowSnapshot,
)
from probe_station_gui.coordinates.persistence import (
    CoordinateFrameDocument,
    CoordinateFrameLoadResult,
)
from probe_station_gui.coordinates.lifecycle import (
    DesignFrameUsabilitySnapshot,
    DesignUsabilityContext,
)
from probe_station_gui.coordinates.provenance import (
    RUNTIME_PROVENANCE_REASON,
    RUNTIME_PROVENANCE_STATUS,
)
from probe_station_gui.design.frame_registration import (
    commit_xyb_registration,
    new_design_frame_draft,
)
from probe_station_gui.route.model import MeasurementRoute
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


def _make_window() -> tuple[Main, _FakeStageController, list[str]]:
    window = Main.__new__(Main)
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


def _activate_candidate_success(
    owner: object,
    *,
    session_state: object,
    **_kwargs: object,
) -> CoordinateTransition:
    owner._design_session.apply_state(session_state)
    return CoordinateTransition(CoordinateSystemSnapshot(True, (), None))


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
    monkeypatch.setattr(
        main_module.connection_flow,
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


def _set_rotation_settings(
    window: object,
    pivot: tuple[float, float] = (0.0, 0.0),
) -> None:
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
    window._coordinate_system_coordinator = types.SimpleNamespace(
        cancel_registration=lambda _reason: CoordinateTransition(
            CoordinateSystemSnapshot(False, (), None)
        )
    )
    monkeypatch.setattr(
        main_module.connection_flow,
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


def test_machine_capture_signal_is_only_translated_to_typed_completion(
    monkeypatch,
) -> None:
    completions: list[CoordinateAdapterCompletion] = []
    rendered: list[CoordinateTransition] = []
    sentinel = object()
    transition = CoordinateTransition(CoordinateSystemSnapshot(True, (), None))
    window = Main.__new__(Main)
    window._manual_alignment_pick_slot = 1
    window._manual_alignment_pick_generation = 9
    window._coordinate_system_coordinator = types.SimpleNamespace(
        complete=lambda completion: completions.append(completion) or transition
    )
    monkeypatch.setattr(
        main_module.connection_flow,
        "apply_coordinate_transition",
        lambda owner, value: rendered.append(value),
    )

    Main._on_registration_machine_coordinate_snapshot_finished(
        window,
        -7,
        True,
        sentinel,
        "captured",
    )

    assert len(completions) == 1
    completion = completions[0]
    assert completion.intent_id == -7
    assert completion.result == MachinePoseCaptureResult(
        -7,
        succeeded=True,
        snapshot=sentinel,
        message="captured",
        active_operator_pick_slot=1,
        active_operator_pick_generation=9,
    )
    assert rendered == [transition]


def test_resolved_image_alignment_capture_submits_typed_request(monkeypatch) -> None:
    requests: list[RegistrationCaptureRequest] = []
    rendered: list[CoordinateTransition] = []
    transition = CoordinateTransition(CoordinateSystemSnapshot(True, (), None))
    window = Main.__new__(Main)
    window._alignment_design_draft = [(0.0, 0.0), (1.0, 0.0)]
    window._manual_alignment_pick_slot = 0
    window._manual_alignment_pick_generation = 4
    window._manual_alignment_capture_context = main_module._ManualAlignmentCaptureContext(
        request_id="image-1",
        slot=0,
        cancelled=main_module.threading.Event(),
    )
    window._rotation_geometry_snapshot = lambda: types.SimpleNamespace(
        pivot_machine_xy=(3.0, 5.0)
    )
    window._camera_stage_xy_from_raw_stage_xy = lambda _point: (-0.5, 0.25)
    window._coordinate_system_coordinator = types.SimpleNamespace(
        capture_registration_mark=lambda request: requests.append(request) or transition
    )
    window._update_coordinate_display = lambda **_kwargs: None
    window._refresh_manual_alignment_ui = lambda: None
    window._update_stage_coordinate_apply_state = lambda: None
    window._show_status = lambda *_args: None
    monkeypatch.setattr(
        main_module.connection_flow,
        "apply_coordinate_transition",
        lambda _owner, value: rendered.append(value),
    )

    Main._on_manual_alignment_point_resolved(
        window,
        "image-1",
        True,
        (0.0, 0.0),
        (7.0, 8.0),
        "",
    )

    assert requests == [
        RegistrationCaptureRequest(
            pivot_machine_xy=(3.0, 5.0),
            objective_xy_offset=(0.5, -0.25),
            operator_alignment=True,
            mark_index=0,
            configured_target_xy=(7.0, 8.0),
            capture_source="image",
            operator_pick_generation=4,
        )
    ]
    assert rendered == [transition]


def test_armed_crosshair_capture_submits_no_b_motion(monkeypatch) -> None:
    requests: list[RegistrationCaptureRequest] = []
    transition = CoordinateTransition(CoordinateSystemSnapshot(True, (), None))
    window = Main.__new__(Main)
    window._alignment_design_draft = [(0.0, 0.0), (1.0, 0.0)]
    window._manual_alignment_pick_slot = 1
    window._manual_alignment_pick_generation = 8
    window._manual_alignment_capture_context = None
    window._rotation_geometry_snapshot = lambda: types.SimpleNamespace(
        pivot_machine_xy=(2.0, 4.0)
    )
    window._camera_stage_xy_from_raw_stage_xy = lambda _point: (0.0, 0.0)
    window._coordinate_system_coordinator = types.SimpleNamespace(
        capture_registration_mark=lambda request: requests.append(request) or transition
    )
    window.stage_controller = types.SimpleNamespace(
        request_rotate_b=lambda *_args: pytest.fail("crosshair capture moved B")
    )
    window._show_status = lambda *_args: None
    monkeypatch.setattr(
        main_module.connection_flow,
        "apply_coordinate_transition",
        lambda *_args: None,
    )

    Main._capture_manual_alignment_center(window, 1)

    assert requests == [
        RegistrationCaptureRequest(
            pivot_machine_xy=(2.0, 4.0),
            objective_xy_offset=(-0.0, -0.0),
            operator_alignment=True,
            mark_index=1,
            configured_target_xy=None,
            capture_source="center",
            operator_pick_generation=8,
        )
    ]


def test_rejected_fresh_frame_keeps_source_mark_and_ui_state(
    monkeypatch,
    tmp_path: Path,
) -> None:
    document = _make_document(tmp_path)
    session = DesignSession(document=document)
    session.source_design_marks = ((1.0, 2.0), (3.0, 4.0))
    session.source_stage_marks = ((5.0, 6.0), (7.0, 8.0))
    window = Main.__new__(Main)
    window._design_session = session
    window._manual_alignment_pick_slot = 1
    window._manual_alignment_points = [(1.0, 2.0), (3.0, 4.0)]
    window._pending_alignment_preparation = object()
    window._last_selected_design_point = (9.0, 10.0)
    calls: list[object] = []
    window._coordinate_system_coordinator = types.SimpleNamespace(
        cancel_registration=lambda _reason: (_ for _ in ()).throw(
            AssertionError("rejected activation cancelled current evidence")
        )
    )
    window._set_design_snap_enabled = lambda value: calls.append(("snap", value))
    window._refresh_design_panel = lambda: calls.append("panel")
    window._set_alignment_panel_expanded = lambda: calls.append("alignment")
    window._show_status = lambda *args: calls.append(("status", args))
    monkeypatch.setattr(
        main_module.connection_flow,
        "activate_current_design",
        lambda *_args, **_kwargs: types.SimpleNamespace(
            accepted=False,
            notices=(),
        ),
    )

    Main._on_design_layout_point_selected(window, 0, 11.0, 12.0)

    assert session.source_design_marks == ((1.0, 2.0), (3.0, 4.0))
    assert session.source_stage_marks == ((5.0, 6.0), (7.0, 8.0))
    assert window._manual_alignment_pick_slot == 1
    assert window._manual_alignment_points == [(1.0, 2.0), (3.0, 4.0)]
    assert window._pending_alignment_preparation is not None
    assert window._last_selected_design_point == (9.0, 10.0)
    assert calls == []


def test_rejected_clear_registration_keeps_current_ui_state(monkeypatch) -> None:
    window = Main.__new__(Main)
    window._pending_alignment_preparation = object()
    window._last_selected_design_point = (9.0, 10.0)
    calls: list[object] = []
    window._set_design_snap_enabled = lambda value: calls.append(("snap", value))
    window._refresh_design_panel = lambda: calls.append("panel")
    window._refresh_design_position = lambda: calls.append("position")
    window._show_status = lambda *args: calls.append(("status", args))
    monkeypatch.setattr(
        main_module.connection_flow,
        "activate_current_design",
        lambda *_args, **_kwargs: types.SimpleNamespace(
            accepted=False,
            notices=(),
        ),
    )

    Main._clear_design_registration(window)

    assert window._pending_alignment_preparation is not None
    assert window._last_selected_design_point == (9.0, 10.0)
    assert calls == []


def test_accepted_clear_registration_restarts_ui(monkeypatch) -> None:
    window = Main.__new__(Main)
    window._pending_alignment_preparation = object()
    window._last_selected_design_point = (9.0, 10.0)
    calls: list[object] = []
    window._set_design_snap_enabled = lambda value: calls.append(("snap", value))
    window._refresh_design_panel = lambda: calls.append("panel")
    window._refresh_design_position = lambda: calls.append("position")
    window._show_status = lambda *args: calls.append(("status", args))
    monkeypatch.setattr(
        main_module.connection_flow,
        "activate_current_design",
        lambda *_args, **_kwargs: types.SimpleNamespace(
            accepted=True,
            notices=(),
        ),
    )

    Main._clear_design_registration(window)

    assert window._pending_alignment_preparation is None
    assert window._last_selected_design_point is None
    assert calls == [
        ("snap", True),
        "panel",
        "position",
        ("status", ("Design calibration restarted.", 4000)),
    ]


def test_focus_callbacks_translate_only_typed_results(monkeypatch) -> None:
    completions: list[CoordinateAdapterCompletion] = []
    rendered: list[CoordinateTransition] = []
    transition = CoordinateTransition(CoordinateSystemSnapshot(True, (), None))
    window = Main.__new__(Main)
    window._coordinate_system_coordinator = types.SimpleNamespace(
        complete=lambda completion: completions.append(completion) or transition
    )
    monkeypatch.setattr(
        main_module.connection_flow,
        "apply_coordinate_transition",
        lambda _owner, value: rendered.append(value),
    )

    Main._on_registration_focus_move_finished(window, 21, (4.0, 5.0), True, "moved")
    Main._on_registration_focus_autofocus_finished(window, 22, True, 3.25, "focused")

    assert completions == [
        CoordinateAdapterCompletion(
            21,
            FocusMoveResult(
                21,
                True,
                "moved",
                completed_target_xy=(4.0, 5.0),
            ),
        ),
        CoordinateAdapterCompletion(
            22,
            AutofocusResult(22, True, physical_z_mm=3.25, message="focused"),
        ),
    ]
    assert rendered == [transition, transition]


def test_main_observes_focus_context_without_owning_lease_policy(
    monkeypatch,
) -> None:
    optical = RegistrationOpticalObservation(
        fov_size=(0.5, 0.5),
        objective_name="5x",
        optical_calibration_identity="calibration-a",
    )
    transition = CoordinateTransition(
        CoordinateSystemSnapshot(True, (), None),
    )
    observations: list[RegistrationOpticalObservation] = []
    rendered: list[CoordinateTransition] = []
    window = Main.__new__(Main)
    window._registration_optical_observation = lambda: optical
    window._coordinate_system_coordinator = types.SimpleNamespace(
        observe_focus_context=lambda value: (
            observations.append(value) or transition
        )
    )
    monkeypatch.setattr(
        main_module.connection_flow,
        "apply_coordinate_transition",
        lambda _owner, value: rendered.append(value),
    )

    Main._observe_design_focus_context(window)

    assert observations == [optical]
    assert rendered == [transition]
    assert not hasattr(window, "_design_focus_overlay_context")


def test_contact_worker_queues_typed_result_before_gui_completion(monkeypatch) -> None:
    requests: list[FirstContactRequest] = []
    completions: list[CoordinateAdapterCompletion] = []
    rendered: list[CoordinateTransition] = []
    queued: list[PhysicalAReadResult] = []
    registration = RegistrationWorkflowSnapshot(
        active_frame_id="frame-1",
        active_frame_version=7,
    )
    armed = CoordinateTransition(
        CoordinateSystemSnapshot(True, (), None, registration=registration),
        intents=(ReadPhysicalAIntent(31),),
    )
    completed = CoordinateTransition(
        CoordinateSystemSnapshot(True, (), None, registration=registration)
    )

    class _Coordinator:
        def arm_first_contact(self, request: FirstContactRequest) -> CoordinateTransition:
            requests.append(request)
            return armed

        def complete(self, completion: CoordinateAdapterCompletion) -> CoordinateTransition:
            completions.append(completion)
            return completed

    window = types.SimpleNamespace(
        _coordinate_system_coordinator=_Coordinator(),
        stage_controller=types.SimpleNamespace(
            run_external_current_physical_machine_coordinates=lambda _axes: {
                "A": 1.75
            }
        ),
        design_contact_a_read_finished=types.SimpleNamespace(
            emit=lambda result: queued.append(result)
        ),
    )
    monkeypatch.setattr(
        main_module.connection_flow,
        "apply_coordinate_transition",
        lambda _owner, value: rendered.append(value),
    )

    capture = Main._design_contact_success_callback(
        window,
        types.SimpleNamespace(frame_id="frame-1", frame_version=7),
    )
    assert callable(capture)
    finalizer = capture(object())

    assert requests == [FirstContactRequest("frame-1", 7)]
    assert completions == []
    assert callable(finalizer)
    finalizer()
    assert queued == [PhysicalAReadResult(31, True, physical_a_mm=1.75)]
    assert completions == []
    assert rendered == [armed]

    Main._on_design_contact_a_read_finished(window, queued[0])

    assert completions == [
        CoordinateAdapterCompletion(
            31,
            PhysicalAReadResult(31, True, physical_a_mm=1.75),
        )
    ]
    assert rendered == [armed, completed]


def test_contact_worker_read_failure_is_queued_without_touching_coordinator(
    monkeypatch,
) -> None:
    reads: list[tuple[str, ...]] = []
    queued: list[PhysicalAReadResult] = []
    armed = CoordinateTransition(
        CoordinateSystemSnapshot(
            True,
            (),
            None,
            registration=RegistrationWorkflowSnapshot(
                active_frame_id="frame-2",
                active_frame_version=8,
            ),
        ),
        intents=(ReadPhysicalAIntent(32),),
    )
    coordinator = types.SimpleNamespace(
        arm_first_contact=lambda _request: armed,
    )
    window = types.SimpleNamespace(
        _coordinate_system_coordinator=coordinator,
        stage_controller=types.SimpleNamespace(
            run_external_current_physical_machine_coordinates=lambda axes: (
                reads.append(tuple(axes))
                or (_ for _ in ()).throw(RuntimeError("read failed"))
            )
        ),
        design_contact_a_read_finished=types.SimpleNamespace(
            emit=lambda result: queued.append(result)
        ),
    )
    monkeypatch.setattr(
        main_module.connection_flow,
        "apply_coordinate_transition",
        lambda *_args: None,
    )

    capture = Main._design_contact_success_callback(
        window,
        types.SimpleNamespace(frame_id="frame-2", frame_version=8),
    )

    assert callable(capture)
    finalizer = capture(object())
    assert callable(finalizer)
    finalizer()
    assert reads == [("A",)]
    assert queued == [
        PhysicalAReadResult(32, False, message="read failed")
    ]


def test_api_contact_callback_arms_and_completes_only_on_creator_thread(
    monkeypatch,
) -> None:
    application = QApplication.instance() or QApplication([])
    creator_thread = threading.get_ident()
    arm_threads: list[int] = []
    complete_threads: list[int] = []
    render_threads: list[int] = []
    read_threads: list[int] = []
    registration = RegistrationWorkflowSnapshot(
        active_frame_id="frame-threaded",
        active_frame_version=9,
    )
    armed = CoordinateTransition(
        CoordinateSystemSnapshot(True, (), None, registration=registration),
        intents=(ReadPhysicalAIntent(33),),
    )
    completed = CoordinateTransition(
        CoordinateSystemSnapshot(True, (), None, registration=registration)
    )

    class _Coordinator:
        def arm_first_contact(self, _request: FirstContactRequest) -> CoordinateTransition:
            arm_threads.append(threading.get_ident())
            return armed

        def complete(self, _completion: CoordinateAdapterCompletion) -> CoordinateTransition:
            complete_threads.append(threading.get_ident())
            return completed

    class _Owner(QObject):
        design_contact_arm_requested = Signal(object)
        design_contact_a_read_finished = Signal(object)

    owner = _Owner()
    owner._coordinate_system_coordinator = _Coordinator()
    owner.stage_controller = types.SimpleNamespace(
        run_external_current_physical_machine_coordinates=lambda _axes: (
            read_threads.append(threading.get_ident()) or {"A": 1.5}
        )
    )
    owner.design_contact_arm_requested.connect(
        lambda request: Main._on_design_contact_arm_requested(owner, request),
        Qt.ConnectionType.BlockingQueuedConnection,
    )
    owner.design_contact_a_read_finished.connect(
        lambda result: Main._on_design_contact_a_read_finished(owner, result),
        Qt.ConnectionType.QueuedConnection,
    )
    monkeypatch.setattr(
        main_module.connection_flow,
        "apply_coordinate_transition",
        lambda _owner, _transition: render_threads.append(threading.get_ident()),
    )
    worker_finished = threading.Event()

    def construct_and_run_callback() -> None:
        capture = Main._design_contact_success_callback(
            owner,
            types.SimpleNamespace(frame_id="frame-threaded", frame_version=9),
        )
        assert callable(capture)
        finalizer = capture(object())
        assert callable(finalizer)
        finalizer()
        worker_finished.set()

    worker = threading.Thread(
        target=construct_and_run_callback,
        name="ApiStageCommand-check_contact",
    )
    worker.start()
    deadline = time.monotonic() + 2.0
    while (worker.is_alive() or not complete_threads) and time.monotonic() < deadline:
        application.processEvents()
        time.sleep(0.001)
    worker.join(timeout=0.2)
    application.processEvents()

    assert worker_finished.is_set()
    assert arm_threads == [creator_thread]
    assert complete_threads == [creator_thread]
    assert render_threads == [creator_thread, creator_thread]
    assert len(read_threads) == 1 and read_threads[0] != creator_thread

    owner._design_contact_success_callback = types.MethodType(
        Main._design_contact_success_callback,
        owner,
    )
    point = types.SimpleNamespace(index=7)
    owner.lcr_controller = object()
    owner.route_measurement_status = types.SimpleNamespace(emit=lambda _message: None)
    owner._api_contact_context = lambda _number: {
        "accepted": True,
        "point": point,
        "contact": {"contact_number": 7},
    }
    owner._api_ensure_measurement_instrument_connected = lambda: None
    owner._api_needle_feedrate = lambda _payload: 7.0
    owner._snapshot_active_route_design_frame = lambda: types.SimpleNamespace(
        frame_id="frame-threaded",
        frame_version=9,
    )
    owner._api_timestamp_utc = lambda: "2026-08-10T00:00:00+00:00"
    created_current: list[dict[str, object]] = []

    class _CurrentContactRunner:
        SHORT_CHECK_SAMPLE_COUNT = main_module.RouteMeasurementRunner.SHORT_CHECK_SAMPLE_COUNT
        AUTO_CONTACT_SEEK_MAX_TOTAL_MM = (
            main_module.RouteMeasurementRunner.AUTO_CONTACT_SEEK_MAX_TOTAL_MM
        )
        AUTO_CONTACT_SEEK_STEP_MM = (
            main_module.RouteMeasurementRunner.AUTO_CONTACT_SEEK_STEP_MM
        )
        DEFAULT_CONTACT_SETTLE_S = (
            main_module.RouteMeasurementRunner.DEFAULT_CONTACT_SETTLE_S
        )

        def __init__(self, **kwargs: object) -> None:
            created_current.append(dict(kwargs))

        def check_contact(self, _point: object) -> object:
            return object()

    api_result: dict[str, object] = {}
    api_finished = threading.Event()

    def construct_current_contact_runner() -> None:
        api_result.update(
            Main._api_measure_current_contact(
                owner,
                {"contact_number": 7},
                seek=False,
            )
        )
        api_finished.set()

    monkeypatch.setattr(main_module, "RouteMeasurementRunner", _CurrentContactRunner)
    monkeypatch.setattr(
        main_module,
        "api_current_contact_response",
        lambda *_args, **_kwargs: {"accepted": True},
    )
    api_worker = threading.Thread(
        target=construct_current_contact_runner,
        name="ApiStageCommand-check_contact-real-path",
    )
    api_worker.start()
    deadline = time.monotonic() + 2.0
    while api_worker.is_alive() and time.monotonic() < deadline:
        application.processEvents()
        time.sleep(0.001)
    api_worker.join(timeout=0.2)

    assert api_finished.is_set()
    assert api_result == {"accepted": True}
    assert callable(created_current[0]["post_success_contact"])

    owner._api_route_lcr_controller = object()
    owner.route_measurement_progress = types.SimpleNamespace(emit=lambda *_args: None)
    owner.route_measurement_result = types.SimpleNamespace(emit=lambda *_args: None)
    owner.route_measurement_waiting_changed = types.SimpleNamespace(
        emit=lambda *_args: None
    )
    owner._capture_api_route_photo_artifact = lambda *_args: None
    owner._api_route_photo_autofocus = lambda *_args, **_kwargs: None
    owner._capture_route_contact_photo = lambda *_args: None
    owner._capture_route_pre_contact_photo = lambda *_args: None
    created_route: list[dict[str, object]] = []

    class _RouteSessionRunner:
        def __init__(self, **kwargs: object) -> None:
            created_route.append(dict(kwargs))

    monkeypatch.setattr(
        main_module,
        "RouteExternalMeasurementSessionRunner",
        _RouteSessionRunner,
    )
    route_finished = threading.Event()

    def construct_route_session_runner() -> None:
        Main._build_api_route_session_runner(
            owner,
            session_id="session-1",
            points=[point],
            selected_point=point,
            start_settings=types.SimpleNamespace(
                measurement_count=5,
                initial_measurement_count=2,
                max_relative_rms=0.01,
                contact_quality_limits=None,
                contact_seek_step_mm=0.001,
                contact_seek_range_mm=0.01,
                contact_settle_s=0.1,
                photo_enabled=False,
                photo_focus_enabled=False,
                photo_settle_s=0.0,
                photo_focus_range_mm=0.03,
            ),
            meter_configuration=types.SimpleNamespace(
                nplc_label=lambda: "1",
                measurement_type_label=lambda: "resistance",
            ),
            needle_feedrate=7.0,
            design_frame_snapshot=types.SimpleNamespace(
                frame_id="frame-threaded",
                frame_version=9,
            ),
        )
        route_finished.set()

    route_worker = threading.Thread(
        target=construct_route_session_runner,
        name="ApiStageCommand-start_route_session-real-path",
    )
    route_worker.start()
    deadline = time.monotonic() + 2.0
    while route_worker.is_alive() and time.monotonic() < deadline:
        application.processEvents()
        time.sleep(0.001)
    route_worker.join(timeout=0.2)

    assert route_finished.is_set()
    assert callable(created_route[0]["post_success_contact"])
    assert arm_threads == [creator_thread, creator_thread, creator_thread]
    assert complete_threads == [creator_thread]
    assert render_threads == [creator_thread] * 4


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
        commit_xyb_registration(
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
        commit_xyb_registration(
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


def test_rejected_design_activation_keeps_previous_session_markup_and_registry(
    monkeypatch,
    tmp_path: Path,
) -> None:
    window, _stage, _statuses = _make_window()
    previous_document = _make_document(tmp_path / "previous-activation")
    window._design_session.load_document(previous_document)
    previous_markup = MarkupDocument.empty(previous_document.path).append_guide(
        (0.0, 0.0),
        (1.0, 0.0),
        guide_id="previous-guide",
    )
    window._design_markup = previous_markup
    registry = CoordinateFrameRegistry()
    coordinator = CoordinateSystemCoordinator(
        registry=registry,
        session=window._design_session,
    )
    load = coordinator.start(MachineProfileObservation("profile-a")).intents[0]
    coordinator.complete(
        CoordinateAdapterCompletion(
            load.intent_id,
            CoordinateFrameLoadResult(
                load.intent_id,
                CoordinateFrameDocument(),
            ),
        )
    )
    window._coordinate_system_coordinator = coordinator
    window._coordinate_frames_loaded = True
    window.stage_controller.axes_are_homed = lambda axes: bool(set(axes) <= {"X", "Y"})
    window.stage_controller.latest_machine_coordinate_snapshot = lambda: _machine_snapshot(
        (1.0, 2.0, 0.0, 0.0, 0.0, 0.0)
    )
    window._rotation_geometry_snapshot = lambda: types.SimpleNamespace(
        pivot_machine_xy=(0.0, 0.0)
    )
    window._active_objective_xy_offset = lambda: (0.0, 0.0)
    window._active_design_frame_metadata = None
    window._design_load_pending = True
    window._design_markup_pending_visibility = True
    success_plans: list[object] = []
    window._apply_design_load_success_plan = lambda *args: success_plans.append(args)
    monkeypatch.setattr(
        main_module.connection_flow.stage_position_panel,
        "refresh_coordinate_frame_display",
        lambda _owner: None,
    )
    candidate_document = _make_document(tmp_path / "rejected-activation")
    context = replace(
        _pending_markup_context(
            candidate_document,
            previous_markup=previous_markup,
        ),
        frame_metadata=object(),
    )
    candidate_markup = MarkupDocument.empty(candidate_document.path)
    session_identity = id(window._design_session)
    baseline = window._design_session.snapshot_state()
    records = registry.snapshot().records

    Main._commit_pending_design_load(window, context, candidate_markup)

    assert id(window._design_session) == session_identity
    assert window._design_session.snapshot_state() == baseline
    assert registry.snapshot().records == records
    assert window._design_markup is previous_markup
    assert window._active_design_frame_metadata is None
    assert success_plans == []
    assert window._design_load_pending is False
    assert window._design_markup_pending_visibility is None



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
    monkeypatch,
    tmp_path: Path,
) -> None:
    window, statuses = _make_mixed_edit_window(tmp_path)
    window._coordinate_system_coordinator = types.SimpleNamespace(
        cancel_registration=lambda _reason: CoordinateTransition(
            CoordinateSystemSnapshot(False, (), None)
        )
    )
    monkeypatch.setattr(
        main_module.connection_flow,
        "apply_coordinate_transition",
        lambda *_args: None,
    )

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
    monkeypatch.setattr(
        main_module.connection_flow,
        "activate_current_design",
        _activate_candidate_success,
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
    window._coordinate_system_coordinator = types.SimpleNamespace(
        cancel_registration=lambda _reason: CoordinateTransition(
            CoordinateSystemSnapshot(False, (), None)
        )
    )
    monkeypatch.setattr(
        main_module.connection_flow,
        "apply_coordinate_transition",
        lambda *_args: None,
    )
    monkeypatch.setattr(
        main_module.connection_flow,
        "activate_current_design",
        _activate_candidate_success,
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
        main_module.connection_flow,
        "activate_current_design",
        _activate_candidate_success,
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
