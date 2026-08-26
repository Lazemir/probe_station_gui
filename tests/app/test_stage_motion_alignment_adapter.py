from __future__ import annotations

import dataclasses
import types
from pathlib import Path

from main import Main
from probe_station_gui.application import alignment as alignment_app
from probe_station_gui.application.stage_motion_types import AlignmentRotationCompletion
from probe_station_gui.coordinates.coordinator_model import (
    CoordinateSystemSnapshot,
    CoordinateTransition,
)
from probe_station_gui.design.session_registration import AlignmentPreparation
from tests.app.test_main_design_markup_navigation import (
    _activate_candidate_success,
    _make_mixed_edit_window,
    main_module,
)


def _preparation() -> AlignmentPreparation:
    return AlignmentPreparation(
        design_marks=((0.0, 0.0), (1.0, 0.0)),
        stage_marks_before_rotation=((10.0, 20.0), (11.0, 20.0)),
        stage_marks_after_rotation=((10.0, 20.0), (11.0, 20.0)),
        pivot_stage=(0.0, 0.0),
        rotation_deg=1.25,
        design_distance_mm=1.0,
        stage_distance_mm=1.0,
        distance_ratio=1.0,
    )


def test_preparation_success_preserves_legacy_order(monkeypatch) -> None:
    owner = Main.__new__(Main)
    preparation = _preparation()
    events: list[object] = []
    transition = object()
    owner._coordinate_system_coordinator = types.SimpleNamespace(
        apply_registration_alignment=lambda request: (
            events.append(("apply", request.preparation)) or transition
        )
    )
    monkeypatch.setattr(
        alignment_app.coordinate_flow,
        "apply_coordinate_transition",
        lambda _owner, value: events.append(("transition", value)),
    )
    owner._finish_alignment_draft = lambda: events.append("draft")
    owner._set_design_snap_enabled = lambda value: events.append(("snap", value))
    owner._refresh_design_panel = lambda: events.append("panel")
    owner._refresh_design_position = lambda: events.append("position")
    owner._collapse_alignment_panel_if_ready = lambda: events.append("collapse")
    owner._microscope_interaction = types.SimpleNamespace(
        clear_target=lambda: events.append("clear")
    )
    owner._show_status = lambda message, timeout: events.append(
        ("status", message, timeout)
    )
    owner.MANUAL_JOG_SETTLE_POLL_DELAYS_MS = (50, 100)
    owner._schedule_status_refreshes = lambda delays: events.append(("refresh", delays))
    correlation = alignment_app._AlignmentRotationCorrelation(preparation, False)

    Main._on_alignment_rotation_finished(
        owner, AlignmentRotationCompletion(correlation, True, "opaque")
    )

    assert events == [
        ("apply", preparation),
        ("transition", transition),
        "draft",
        ("snap", False),
        "panel",
        "position",
        "collapse",
        "clear",
        (
            "status",
            "Design calibration complete. Rotation +1.250 deg, spacing ratio "
            "1.000. RMS 0.0000 mm, max 0.0000 mm.",
            7000,
        ),
        ("refresh", (50, 100)),
    ]


def test_preparation_failure_reports_and_refreshes_without_apply() -> None:
    owner = Main.__new__(Main)
    events: list[object] = []
    owner._show_status = lambda message, timeout: events.append(
        ("status", message, timeout)
    )
    owner.MANUAL_JOG_SETTLE_POLL_DELAYS_MS = (50,)
    owner._schedule_status_refreshes = lambda delays: events.append(("refresh", delays))
    correlation = alignment_app._AlignmentRotationCorrelation(_preparation(), False)

    Main._on_alignment_rotation_finished(
        owner, AlignmentRotationCompletion(correlation, False, "blocked")
    )

    assert events == [
        ("status", "Design calibration rotation failed: blocked", 7000),
        ("refresh", (50,)),
    ]


def test_quick_completion_uses_quick_status_collapse_and_cancel_paths() -> None:
    owner = Main.__new__(Main)
    events: list[object] = []
    owner._show_status = lambda message, timeout: events.append(
        ("status", message, timeout)
    )
    owner._collapse_alignment_panel_if_design_open = lambda: events.append("collapse")
    owner.MANUAL_JOG_SETTLE_POLL_DELAYS_MS = (50,)
    owner._schedule_status_refreshes = lambda delays: events.append(("refresh", delays))
    owner._schedule_cancel_state_refresh = lambda: events.append("cancel")
    correlation = alignment_app._AlignmentRotationCorrelation(None, True)

    Main._on_alignment_rotation_finished(
        owner, AlignmentRotationCompletion(correlation, True, "done")
    )
    Main._on_alignment_rotation_finished(
        owner, AlignmentRotationCompletion(correlation, False, "failed")
    )

    assert events == [
        ("status", "done", 5000),
        "collapse",
        ("refresh", (50,)),
        "cancel",
        ("status", "failed", 5000),
        "cancel",
    ]


def test_foreign_correlation_is_ignored() -> None:
    Main._on_alignment_rotation_finished(
        Main.__new__(Main),
        AlignmentRotationCompletion(object(), True, "foreign"),
    )


def test_alignment_rotation_correlation_is_frozen() -> None:
    correlation_type = alignment_app._AlignmentRotationCorrelation
    assert dataclasses.is_dataclass(correlation_type)
    assert correlation_type.__dataclass_params__.frozen is True


def test_rejected_rotation_does_not_apply_started_motion_effects() -> None:
    owner = Main.__new__(Main)
    events: list[object] = []
    owner._stage_motion = types.SimpleNamespace(
        request_alignment_rotation=lambda *_args: False
    )
    owner._invalidate_design_registration = lambda message: events.append(
        ("invalidate", message)
    )
    owner._set_design_snap_enabled = lambda enabled: events.append(("snap", enabled))
    owner._refresh_manual_alignment_ui = lambda: events.append("manual")
    owner._refresh_design_panel = lambda: events.append("panel")
    owner._refresh_design_position = lambda: events.append("position")
    owner._set_alignment_panel_expanded = lambda: events.append("expand")
    owner._collapse_alignment_panel_if_ready = lambda: events.append("collapse-ready")
    owner._collapse_alignment_panel_if_design_open = lambda: events.append(
        "collapse-design"
    )
    owner._show_status = lambda message, timeout: events.append(
        ("status", message, timeout)
    )
    plan = types.SimpleNamespace(
        points=None,
        apply_prepared_alignment=False,
        preparation=None,
        request_b_rotation=True,
        rotation_deg=2.0,
        pending_preparation=None,
        pending_quick_alignment_rotation=True,
        invalidate_design_registration=True,
        disable_snap=True,
        refresh_manual_ui=True,
        refresh_design_panel=True,
        refresh_design_position=True,
        expand_alignment=True,
        collapse_alignment_if_ready=True,
        collapse_alignment_if_design_open=True,
        status="Rotation started.",
        status_timeout_ms=5000,
    )

    Main._apply_alignment_capture_plan(owner, plan)

    assert events == []


def test_rotate_design_waits_for_accepted_alignment_rotation(
    monkeypatch,
    tmp_path: Path,
) -> None:
    window, statuses = _make_mixed_edit_window(tmp_path)
    window._stage_motion.alignment_pending = True
    window._coordinate_system_coordinator = types.SimpleNamespace(
        snapshot=lambda: CoordinateSystemSnapshot(False, (), None),
        cancel_registration=lambda _reason: CoordinateTransition(
            CoordinateSystemSnapshot(False, (), None)
        ),
    )
    monkeypatch.setattr(
        main_module.coordinate_flow,
        "apply_coordinate_transition",
        lambda *_args: None,
    )
    monkeypatch.setattr(
        main_module.coordinate_flow,
        "activate_current_design",
        _activate_candidate_success,
    )
    before = window._design_session.document

    Main._rotate_design_document(window, 1)

    assert window._design_session.document is before
    assert (
        statuses[-1] == "Wait for chip rotation to finish before rotating the design."
    )
