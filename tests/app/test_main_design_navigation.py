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
from probe_station_gui.design.session import DesignSession
from probe_station_gui.coordinates.registry import CoordinateFrameRegistry
from probe_station_gui.design.frame_registration import new_design_frame_draft
from probe_station_gui.route.model import MeasurementRoute

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


def test_legacy_migration_reports_b_calibration_failure_without_adding_frame(
    tmp_path: Path,
) -> None:
    document = _make_document(tmp_path)
    session = DesignSession(document=document)
    session.source_design_marks = ((0.0, 0.0), (1000.0, 0.0))
    session.source_stage_marks = ((3.0, 4.0), (4.0, 4.0))
    registry = CoordinateFrameRegistry()
    statuses: list[str] = []
    window = Main.__new__(Main)
    window._design_session = session
    window._coordinate_frame_registry = registry
    window._coordinate_frames_loaded = True
    window._active_design_frame_metadata = main_module.DesignFrameMetadata.from_document(
        document
    )
    window.stage_controller = types.SimpleNamespace(
        latest_stage_position=lambda: (0.0, 0.0, 0.0, 0.0, 12.0),
        calibrated_axis_display_value=lambda _axis, _value: (_ for _ in ()).throw(
            RuntimeError("B calibration unavailable")
        ),
    )
    window._show_status = lambda message, _timeout: statuses.append(message)

    Main._activate_loaded_design_frame(window)

    assert registry.snapshot().records == ()
    assert statuses == ["B calibration unavailable"]


def test_legacy_migration_converts_camera_configured_marks_to_physical_machine(
    tmp_path: Path,
) -> None:
    document = _make_document(tmp_path)
    window = Main.__new__(Main)
    window._design_session = DesignSession(document=document)
    window._raw_stage_xy_from_camera_stage_xy = lambda point: (
        point[0] + 100.0,
        point[1] - 200.0,
    )
    window.stage_controller = types.SimpleNamespace(
        calibrated_axis_display_value=lambda axis, value: float(value)
        + {"X": 10.0, "Y": 20.0}[axis],
    )
    legacy = {
        "source_design_marks": [[0.0, 0.0], [1000.0, 0.0]],
        "source_stage_marks": [[-97.0, 204.0], [-96.0, 204.0]],
        "check_design_marks": [[500.0, 0.0]],
        "check_stage_marks": [[-96.5, 204.0]],
    }

    converted = Main._legacy_design_state_for_frame_migration(window, legacy)

    assert converted["source_stage_marks"] == [[13.0, 24.0], [14.0, 24.0]]
    assert converted["check_stage_marks"] == [[13.5, 24.0]]


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
    positions = iter(
        (
            (3.0, 4.0, 0.0, 0.0, 12.0),
            (4.0, 4.0, 0.0, 0.0, 12.0),
        )
    )
    events: list[object] = []
    window = Main.__new__(Main)
    window._design_session = session
    window._coordinate_frame_registry = registry
    window.stage_controller = types.SimpleNamespace(
        current_stage_position=lambda: next(positions),
        calibrated_axis_display_value=lambda axis, value: float(value)
        + {"X": 10.0, "Y": 20.0, "B": 30.0}[axis],
        calibrated_axis_raw_value=lambda axis, value: float(value)
        - {"X": 10.0, "Y": 20.0}[axis],
    )
    window._camera_stage_xy_from_raw_stage_xy = lambda point: (
        point[0] - 100.0,
        point[1] + 200.0,
    )
    window._refresh_design_panel = lambda: None
    window._refresh_design_position = lambda: None
    window._show_status = lambda *_args: None
    monkeypatch.setattr(
        main_module.connection_flow,
        "publish_coordinate_frames",
        lambda _owner: events.append(("publish",)),
    )

    Main._capture_stage_source_mark(window)
    Main._capture_stage_source_mark(window)

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


def test_stage_mark_capture_rejects_calibration_failure_before_mutating_session(
    tmp_path: Path,
) -> None:
    document = _make_document(tmp_path)
    session = DesignSession(document=document)
    session.source_design_marks = ((0.0, 0.0),)
    statuses: list[str] = []
    window = Main.__new__(Main)
    window._design_session = session
    window.stage_controller = types.SimpleNamespace(
        current_stage_position=lambda: (3.0, 4.0, 0.0, 0.0, 12.0),
        calibrated_axis_display_value=lambda _axis, _value: (_ for _ in ()).throw(
            RuntimeError("calibration unavailable")
        ),
    )
    window._refresh_design_panel = lambda: None
    window._refresh_design_position = lambda: None
    window._show_status = lambda message, _timeout: statuses.append(message)

    Main._capture_stage_source_mark(window)

    assert not session.source_stage_marks_compact()
    assert statuses == ["calibration unavailable"]


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
    positions = iter(
        (
            (10.0, 20.0, 0.0, 0.0, 5.0),
            (11.0, 20.0, 0.0, 0.0, 5.0),
        )
    )
    window = Main.__new__(Main)
    window._design_session = session
    window._coordinate_frame_registry = registry
    window._coordinate_frames_loaded = True
    window._active_design_frame_metadata = main_module.DesignFrameMetadata.from_mapping(
        committed.metadata
    )
    window._manual_alignment_pick_slot = None
    window._manual_alignment_points = [None, None]
    window._pending_alignment_preparation = None
    window._last_selected_design_point = None
    window.stage_controller = types.SimpleNamespace(
        current_stage_position=lambda: next(positions),
        calibrated_axis_display_value=lambda _axis, value: float(value),
        calibrated_axis_raw_value=lambda _axis, value: float(value),
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

    first_capture = registry.get(replacement_id)
    assert replacement_id != committed.frame_id
    assert first_capture is not None
    assert not first_capture.readiness["X"].available

    Main._capture_stage_source_mark(window)

    replacement = registry.get(replacement_id)
    assert replacement is not None
    metadata = main_module.DesignFrameMetadata.from_mapping(replacement.metadata)
    assert metadata.source_design_marks == ((100.0, 200.0), (1100.0, 200.0))
    assert metadata.source_machine_marks == ((10.0, 20.0), (11.0, 20.0))
    assert replacement.readiness["X"].available
    assert registry.get(committed.frame_id) == committed


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
    window._pending_alignment_preparation = None
    window._last_selected_design_point = None
    window._design_load_pending = False
    window._route_measurement_thread = None
    window._route_measurement_session_active = False
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


def test_explicit_unload_deletes_markup_sidecar(tmp_path: Path) -> None:
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

    Main._unload_design_document(window)

    assert window._design_session.document is None
    assert window._design_markup is None
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
