from __future__ import annotations

from dataclasses import replace

import pytest

from probe_station_gui.design.registration_lifecycle import (
    DesignRegistrationLifecycle,
    RegistrationCancellation,
    RegistrationCaptureContext,
    RegistrationCaptureOutcome,
    RegistrationSample,
)


def _context(
    *,
    frame_version: int = 1,
    top_cell: str = "TOP",
    pivot: tuple[float, float] = (0.0, 0.0),
    objective_offset: tuple[float, float] = (0.0, 0.0),
    mark_kind: str = "source",
    check_design_marks: tuple[tuple[float, float], ...] = (),
    baseline_registration: object = "registered-baseline",
) -> RegistrationCaptureContext:
    return RegistrationCaptureContext(
        session_identity=17,
        frame_id="design-frame",
        frame_version=frame_version,
        source_identity=("C:/designs/device.gds", "load-1"),
        top_cell_name=top_cell,
        rotation_quarter_turns=0,
        pivot_machine_xy=pivot,
        objective_xy_offset=objective_offset,
        mark_kind=mark_kind,
        source_design_marks=((0.0, 0.0), (1.0, 0.0)),
        check_design_marks=check_design_marks,
        baseline_source_stage_marks=(),
        baseline_check_stage_marks=(),
        baseline_registration=baseline_registration,
        baseline_registration_status="Registered.",
    )


def _sample(
    *,
    x: float = 1.0,
    y: float = 2.0,
    b: float = 0.0,
    mark_kind: str = "source",
) -> RegistrationSample:
    return RegistrationSample(
        mark_kind=mark_kind,
        physical_machine_xy=(x, y),
        physical_b_deg=b,
        stage_xy=(x, y),
    )


def test_stale_sample_after_context_change_is_rejected_and_baseline_restored() -> None:
    lifecycle = DesignRegistrationLifecycle()
    token = lifecycle.begin_capture(_context(frame_version=1, top_cell="TOP"))

    cancellation = lifecycle.cancel(RegistrationCancellation.TOP_CELL_CHANGED)
    stale = lifecycle.accept_sample(token, _sample())

    assert cancellation.restore_baseline is True
    assert cancellation.baseline_registration == "registered-baseline"
    assert stale.accepted is False


@pytest.mark.parametrize("succeeded", (True, False))
def test_stale_callback_outcome_is_rejected_before_adapter_processing(
    succeeded: bool,
) -> None:
    lifecycle = DesignRegistrationLifecycle()
    token = lifecycle.begin_capture(_context())
    lifecycle.cancel(RegistrationCancellation.FRAME_CHANGED)

    result = lifecycle.accept_sample(
        token,
        RegistrationCaptureOutcome(
            succeeded=succeeded,
            message="coordinates unavailable",
        ),
    )

    assert result.accepted is False


def test_current_successful_callback_authorizes_the_converted_sample() -> None:
    lifecycle = DesignRegistrationLifecycle()
    token = lifecycle.begin_capture(_context())

    ready = lifecycle.accept_sample(
        token,
        RegistrationCaptureOutcome(succeeded=True),
    )
    captured = lifecycle.accept_sample(token, _sample())

    assert ready.accepted is True
    assert ready.reason is None
    assert captured.accepted is True


def test_current_failed_callback_returns_message_and_consumes_request() -> None:
    lifecycle = DesignRegistrationLifecycle()
    token = lifecycle.begin_capture(_context())

    failure = lifecycle.accept_sample(
        token,
        RegistrationCaptureOutcome(
            succeeded=False,
            message="coordinates unavailable",
        ),
    )
    repeated = lifecycle.accept_sample(token, _sample())

    assert failure.accepted is True
    assert failure.reason == "coordinates unavailable"
    assert repeated.accepted is False


def test_mixed_b_samples_are_normalized_about_captured_pivot() -> None:
    lifecycle = DesignRegistrationLifecycle()
    first = lifecycle.begin_capture(
        _context(frame_version=1, top_cell="TOP", pivot=(2.0, 3.0))
    )
    initial = lifecycle.accept_sample(first, _sample(x=12.0, y=3.0, b=0.0))
    second = lifecycle.begin_capture(
        _context(frame_version=1, top_cell="TOP", pivot=(2.0, 3.0))
    )

    result = lifecycle.accept_sample(second, _sample(x=2.0, y=13.0, b=90.0))

    assert initial.commit_requested is False
    assert result.commit_requested is True
    assert result.normalized_samples[0].reference_b_deg == pytest.approx(
        result.normalized_samples[1].reference_b_deg
    )
    assert result.normalized_samples[0].machine_xy == pytest.approx((12.0, 3.0))
    assert result.normalized_samples[1].machine_xy == pytest.approx((12.0, 3.0))


def test_exact_context_reuses_operation_but_only_latest_request_can_complete() -> None:
    lifecycle = DesignRegistrationLifecycle()

    superseded = lifecycle.begin_capture(_context())
    current = lifecycle.begin_capture(_context())

    assert current.operation_id == superseded.operation_id
    assert lifecycle.accept_sample(superseded, _sample()).accepted is False
    assert lifecycle.accept_sample(current, _sample()).accepted is True


def test_tentative_session_evidence_does_not_change_operation_identity() -> None:
    lifecycle = DesignRegistrationLifecycle()
    context = _context()
    first = lifecycle.begin_capture(context)
    lifecycle.accept_sample(first, _sample())

    second = lifecycle.begin_capture(
        replace(
            context,
            baseline_source_stage_marks=((1.0, 2.0),),
            baseline_registration=None,
            baseline_registration_status="Source mark capture is incomplete.",
        )
    )

    assert second.operation_id == first.operation_id
    assert second.superseded_effects.restore_baseline is False


def test_context_replacement_carries_old_baseline_effect_before_new_operation() -> None:
    lifecycle = DesignRegistrationLifecycle()
    old = lifecycle.begin_capture(
        _context(frame_version=1, baseline_registration="old-registration")
    )
    lifecycle.accept_sample(old, _sample())

    replacement = lifecycle.begin_capture(
        _context(frame_version=2, baseline_registration="new-registration")
    )

    assert replacement.operation_id != old.operation_id
    assert replacement.superseded_effects.restore_baseline is True
    assert replacement.superseded_effects.baseline_registration == "old-registration"
    assert lifecycle.accept_sample(old, _sample()).accepted is False
    lifecycle.accept_sample(replacement, _sample(x=3.0, y=4.0))
    replacement_cancel = lifecycle.cancel(RegistrationCancellation.FRAME_CHANGED)
    assert replacement_cancel.baseline_registration == "old-registration"


def test_capture_token_preserves_the_objective_offset_context() -> None:
    lifecycle = DesignRegistrationLifecycle()

    token = lifecycle.begin_capture(_context(objective_offset=(0.25, -0.5)))

    assert token.objective_xy_offset == (0.25, -0.5)


def test_sample_kind_must_match_the_capture_request() -> None:
    lifecycle = DesignRegistrationLifecycle()
    token = lifecycle.begin_capture(
        _context(
            mark_kind="check",
            check_design_marks=((0.5, 0.5),),
        )
    )

    result = lifecycle.accept_sample(token, _sample(mark_kind="source"))

    assert token.mark_kind == "check"
    assert result.accepted is False


def test_cancel_returns_saved_baseline_exactly_once() -> None:
    lifecycle = DesignRegistrationLifecycle()
    lifecycle.begin_capture(_context())

    first = lifecycle.cancel(RegistrationCancellation.FRAME_CHANGED)
    second = lifecycle.cancel(RegistrationCancellation.FRAME_CHANGED)

    assert first.restore_baseline is True
    assert first.baseline_session_identity == 17
    assert first.baseline_frame_id == "design-frame"
    assert second.restore_baseline is False


def test_commit_waits_for_every_selected_source_and_check_mark() -> None:
    lifecycle = DesignRegistrationLifecycle()
    context = _context(check_design_marks=((0.5, 0.5),))

    first = lifecycle.begin_capture(context)
    assert lifecycle.accept_sample(first, _sample(x=1.0)).commit_requested is False
    second = lifecycle.begin_capture(context)
    assert lifecycle.accept_sample(second, _sample(x=2.0)).commit_requested is False
    check = lifecycle.begin_capture(replace(context, mark_kind="check"))
    completed = lifecycle.accept_sample(
        check,
        _sample(x=1.5, y=0.5, mark_kind="check"),
    )

    assert completed.commit_requested is True
    assert [sample.mark_kind for sample in completed.normalized_samples] == [
        "source",
        "source",
        "check",
    ]


def test_completed_batch_carries_its_exact_failure_rollback_effect() -> None:
    lifecycle = DesignRegistrationLifecycle()
    baseline_registration = object()
    context = replace(
        _context(baseline_registration=baseline_registration),
        baseline_source_stage_marks=((8.0, 9.0),),
        baseline_check_stage_marks=((10.0, 11.0),),
        baseline_registration_status="Exact durable baseline.",
    )
    first = lifecycle.begin_capture(context)
    lifecycle.accept_sample(first, _sample(x=1.0))
    second = lifecycle.begin_capture(context)

    completed = lifecycle.accept_sample(second, _sample(x=2.0))

    rollback = completed.rollback_effects
    assert completed.commit_requested is True
    assert rollback is not None
    assert rollback.restore_baseline is True
    assert rollback.captured_sample is None
    assert rollback.baseline_session_identity == context.session_identity
    assert rollback.baseline_frame_id == context.frame_id
    assert rollback.baseline_source_stage_marks == ((8.0, 9.0),)
    assert rollback.baseline_check_stage_marks == ((10.0, 11.0),)
    assert rollback.baseline_registration is baseline_registration
    assert rollback.baseline_registration_status == "Exact durable baseline."


def test_design_package_exports_the_registration_lifecycle_interface() -> None:
    from probe_station_gui.design import (
        DesignRegistrationLifecycle as ExportedDesignRegistrationLifecycle,
    )

    assert ExportedDesignRegistrationLifecycle.__name__ == "DesignRegistrationLifecycle"
    assert ExportedDesignRegistrationLifecycle.__module__ == (
        "probe_station_gui.design.registration_lifecycle"
    )


def test_registration_lifecycle_exposes_exactly_three_operations() -> None:
    operations = {
        name
        for name, value in vars(DesignRegistrationLifecycle).items()
        if callable(value) and not name.startswith("_")
    }

    assert operations == {"begin_capture", "accept_sample", "cancel"}


def test_brief_context_fields_default_optional_adapter_metadata() -> None:
    context = RegistrationCaptureContext(
        session_identity=17,
        frame_id="design-frame",
        frame_version=1,
        source_identity=("C:/designs/device.gds", "load-1"),
        top_cell_name="TOP",
        rotation_quarter_turns=0,
        pivot_machine_xy=(0.0, 0.0),
        source_design_marks=((0.0, 0.0), (1.0, 0.0)),
        check_design_marks=(),
    )

    token = DesignRegistrationLifecycle().begin_capture(context)

    assert token.objective_xy_offset == (0.0, 0.0)
    assert token.mark_kind == "source"
    assert RegistrationSample(
        mark_kind="source",
        physical_machine_xy=(1.0, 2.0),
        physical_b_deg=3.0,
    ).stage_xy is None
