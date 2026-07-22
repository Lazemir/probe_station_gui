from __future__ import annotations

from uuid import uuid4

from probe_station_gui.coordinates.model import (
    AxisReadiness,
    CoordinateFrameRecord,
    ReadinessStatus,
    VISIBLE_STAGE_AXES,
)
from probe_station_gui.coordinates.registry import CoordinateFrameRegistry
from probe_station_gui.coordinates.transforms import BFrameTransform
from probe_station_gui.design.frame_registration import (
    ContactReferenceToken,
    RegistrationFocusToken,
    commit_contact_reference,
    commit_focus_reference,
    reset_focus_reference,
    set_contact_reference,
    set_focus_reference,
)


def _registered_xyb() -> CoordinateFrameRecord:
    return CoordinateFrameRecord.create_design(
        frame_id=str(uuid4()),
        name="chip",
        transform=BFrameTransform.identity(),
        readiness={
            axis: AxisReadiness(
                ReadinessStatus.READY
                if axis in {"X", "Y", "B"}
                else ReadinessStatus.MISSING
            )
            for axis in VISIBLE_STAGE_AXES
        },
        metadata={},
    )


def test_focus_completes_registration_and_reset_always_clears_contact() -> None:
    focused = set_focus_reference(_registered_xyb(), physical_machine_z_mm=6.0)
    contacted = set_contact_reference(focused, physical_machine_a_mm=8.0)

    reset = reset_focus_reference(contacted, reason="Focus reference changed.")

    assert not reset.readiness["Z"].available
    assert not reset.readiness["A"].available
    assert reset.transform is not None
    assert reset.transform.z_zero_machine_mm is None
    assert reset.transform.a_zero_machine_mm is None


def test_changing_focus_always_invalidates_existing_contact() -> None:
    first_focus = set_focus_reference(_registered_xyb(), physical_machine_z_mm=6.0)
    contacted = set_contact_reference(first_focus, physical_machine_a_mm=8.0)

    changed = set_focus_reference(contacted, physical_machine_z_mm=7.0)

    assert changed.readiness["Z"].available
    assert not changed.readiness["A"].available
    assert changed.transform is not None
    assert changed.transform.z_zero_machine_mm == 7.0
    assert changed.transform.a_zero_machine_mm is None


def test_contact_requires_a_valid_focus_reference() -> None:
    record = _registered_xyb()

    try:
        set_contact_reference(record, physical_machine_a_mm=8.0)
    except ValueError as exc:
        assert "focus" in str(exc).lower()
    else:  # pragma: no cover - assertion makes the intended contract explicit
        raise AssertionError("Contact reference was accepted without focus.")


def test_failed_and_stale_operations_do_not_capture_references() -> None:
    registry = CoordinateFrameRegistry()
    initial = registry.add(_registered_xyb())
    focus_token = RegistrationFocusToken(initial.frame_id, initial.version)

    assert (
        commit_focus_reference(
            registry,
            focus_token,
            success=False,
            physical_machine_z_mm=2.0,
        )
        is None
    )
    assert registry.get(initial.frame_id).transform.z_zero_machine_mm is None

    focused = commit_focus_reference(
        registry,
        focus_token,
        success=True,
        physical_machine_z_mm=2.0,
    )
    assert focused is not None
    contact_token = ContactReferenceToken(focused.frame_id, focused.version)

    assert (
        commit_contact_reference(
            registry,
            contact_token,
            success=False,
            physical_machine_a_mm=3.0,
        )
        is None
    )
    assert registry.get(initial.frame_id).transform.a_zero_machine_mm is None
    assert (
        commit_contact_reference(
            registry,
            ContactReferenceToken(focused.frame_id, focused.version - 1),
            success=True,
            physical_machine_a_mm=3.0,
        )
        is None
    )
    assert registry.get(initial.frame_id).transform.a_zero_machine_mm is None


def test_only_matching_explicit_focus_token_can_capture_z() -> None:
    registry = CoordinateFrameRegistry()
    initial = registry.add(_registered_xyb())
    stale = RegistrationFocusToken(initial.frame_id, initial.version + 1)

    assert (
        commit_focus_reference(
            registry,
            stale,
            success=True,
            physical_machine_z_mm=4.0,
        )
        is None
    )
    assert registry.get(initial.frame_id).transform.z_zero_machine_mm is None
