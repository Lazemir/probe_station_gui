"""Immutable transient state for Design Window drawing tools."""

from __future__ import annotations

from dataclasses import dataclass, replace
from enum import Enum
import math
from typing import TypeAlias

from probe_station_gui.design.model import Point2D
from probe_station_gui.design.navigation_geometry import (
    format_point,
    route_pick_label_text,
    vector_from_length_angle,
    vector_length_angle,
)
from probe_station_gui.design.selection_geometry import (
    SegmentGeometry,
    constrain_vector_endpoint,
)
from probe_station_gui.design.selection_model import MixedArrayRequest
from probe_station_gui.design.tool_context import DesignToolContext


DesignToolName: TypeAlias = str
RulerSegment: TypeAlias = tuple[Point2D, Point2D]
GuideGeometry: TypeAlias = SegmentGeometry

_VALID_TOOLS = frozenset(
    {"select", "move", "point", "guide", "ruler", "array", "align"}
)
_ARRAY_PICK_MODES = frozenset({"array_dir1", "array_dir2"})


@dataclass(frozen=True, slots=True)
class ArrayToolConfiguration:
    """Canonical values behind the array controls."""

    direction_1_length: float = 100.0
    direction_1_angle_degrees: float = 0.0
    count_1: int = 8
    direction_2_length: float = 100.0
    direction_2_angle_degrees: float = 90.0
    count_2: int = 1
    serpentine: bool = False

    @property
    def direction_1(self) -> Point2D:
        return vector_from_length_angle(
            self.direction_1_length,
            self.direction_1_angle_degrees,
        )

    @property
    def direction_2(self) -> Point2D:
        return vector_from_length_angle(
            self.direction_2_length,
            self.direction_2_angle_degrees,
        )

    def with_direction(self, mode: str, vector: Point2D) -> ArrayToolConfiguration:
        length, angle = vector_length_angle(vector)
        if mode == "array_dir1":
            return replace(
                self,
                direction_1_length=length,
                direction_1_angle_degrees=angle,
            )
        if mode == "array_dir2":
            return replace(
                self,
                direction_2_length=length,
                direction_2_angle_degrees=angle,
            )
        return self

    def request(
        self,
        source_ids: frozenset[str],
        *,
        direction_1: Point2D | None = None,
        direction_2: Point2D | None = None,
    ) -> MixedArrayRequest:
        return MixedArrayRequest(
            direction_1=direction_1 or self.direction_1,
            count_1=max(1, int(self.count_1)),
            direction_2=direction_2 or self.direction_2,
            count_2=max(1, int(self.count_2)),
            serpentine=bool(self.serpentine),
            source_ids=source_ids,
        )


@dataclass(frozen=True, slots=True)
class MixedArrayPreview:
    """Immutable geometry emitted to the plot adapter."""

    route_points: tuple[Point2D, ...] = ()
    guide_segments: tuple[GuideGeometry, ...] = ()

    @property
    def is_empty(self) -> bool:
        return not self.route_points and not self.guide_segments


@dataclass(frozen=True, slots=True)
class RulerReadout:
    start_text: str
    end_text: str
    delta_text: str
    length_text: str


class DesignToolEffectKind(str, Enum):
    ACTIVE_TOOL_CHANGED = "active_tool_changed"
    PICK_MODE_CHANGED = "pick_mode_changed"
    ROUTE_PREVIEW_CHANGED = "route_preview_changed"
    MEASURE_PREVIEW_CHANGED = "measure_preview_changed"
    MEASUREMENTS_CHANGED = "measurements_changed"
    ALIGNMENT_CHANGED = "alignment_changed"
    ALIGNMENT_ACCEPTED = "alignment_accepted"
    ALIGNMENT_DISCARDED = "alignment_discarded"
    MIXED_ARRAY_REQUESTED = "mixed_array_requested"
    MIXED_ARRAY_PREVIEW_CHANGED = "mixed_array_preview_changed"


@dataclass(frozen=True, slots=True)
class DesignToolEffect:
    kind: DesignToolEffectKind
    value: object = None


@dataclass(frozen=True, slots=True)
class _DesignToolStage:
    session: DesignToolSession
    effects: tuple[DesignToolEffect, ...] = ()


@dataclass(frozen=True, slots=True)
class DesignToolTransition:
    session: DesignToolSession
    effects: tuple[DesignToolEffect, ...] = ()
    stages: tuple[_DesignToolStage, ...] = ()


@dataclass(frozen=True, slots=True)
class DesignToolSession:
    """Own one Design Window's transient tool workflow."""

    active_tool: DesignToolName = "select"
    context: DesignToolContext = DesignToolContext()
    context_generation: int = 0
    pick_mode: str | None = None
    pick_anchor: Point2D | None = None
    ruler_anchor: Point2D | None = None
    ruler_hover: Point2D | None = None
    ruler_segments: tuple[RulerSegment, ...] = ()
    alignment_draft: tuple[Point2D, ...] = ()
    array_configuration: ArrayToolConfiguration = ArrayToolConfiguration()
    measure_preview: tuple[Point2D, ...] | None = None
    mixed_array_preview: MixedArrayPreview = MixedArrayPreview()
    status_message: str = ""

    @property
    def alignment_valid(self) -> bool:
        return len(self.alignment_draft) >= 2 and len(set(self.alignment_draft)) >= 2

    @property
    def alignment_text(self) -> str:
        if not self.alignment_draft:
            return "Click geometry to add D1, D2, and more."
        return "\n".join(
            f"D{index}: {format_point(point)}"
            for index, point in enumerate(self.alignment_draft, start=1)
        )

    @property
    def ruler_readout(self) -> RulerReadout:
        end = self.ruler_hover
        start_text = (
            "Start: not set"
            if self.ruler_anchor is None
            else f"Start: {format_point(self.ruler_anchor)}"
        )
        end_text = (
            "End: not set" if end is None else f"End: {format_point(end)}"
        )
        if self.ruler_anchor is None or end is None:
            length_text = "Length=0.000, Angle=0.000 deg"
            if self.ruler_segments:
                length_text = f"{len(self.ruler_segments)} measurements"
            return RulerReadout(
                start_text,
                end_text,
                "dX=0.000, dY=0.000",
                length_text,
            )
        dx = end[0] - self.ruler_anchor[0]
        dy = end[1] - self.ruler_anchor[1]
        length = math.hypot(dx, dy)
        angle = math.degrees(math.atan2(dy, dx)) if length > 0.0 else 0.0
        return RulerReadout(
            start_text,
            end_text,
            f"dX={dx:.3f}, dY={dy:.3f}",
            f"Length={length:.3f}, Angle={angle:.3f} deg",
        )

    def rebase_context_from(self, current: DesignToolSession) -> DesignToolSession:
        """Adopt context changes made by a synchronous effect observer."""
        if (
            self.context == current.context
            and self.context_generation == current.context_generation
        ):
            return self
        rebased = replace(
            self,
            context=current.context,
            context_generation=current.context_generation,
        )
        return rebased._with_current_array_preview()

    def replace_context(self, context: DesignToolContext) -> DesignToolTransition:
        if context == self.context:
            return DesignToolTransition(self)
        effects: list[DesignToolEffect] = []
        document_replaced = context.document_token != self.context.document_token
        if document_replaced and self.alignment_draft:
            effects.append(_effect(DesignToolEffectKind.ALIGNMENT_CHANGED, ()))
        updated = replace(
            self,
            context=context,
            context_generation=self.context_generation + 1,
            alignment_draft=() if document_replaced else self.alignment_draft,
        )
        updated = updated._with_current_array_preview()
        if updated.mixed_array_preview != self.mixed_array_preview:
            effects.append(
                _effect(
                    DesignToolEffectKind.MIXED_ARRAY_PREVIEW_CHANGED,
                    updated.mixed_array_preview,
                )
            )
        return DesignToolTransition(updated, tuple(effects))

    def activate(self, tool: str) -> DesignToolTransition:
        selected = tool if tool in _VALID_TOOLS else "select"
        effects: list[DesignToolEffect] = []
        alignment_stage: _DesignToolStage | None = None
        state = self
        if state.active_tool == "align" and selected != "align" and state.alignment_draft:
            state = replace(state, alignment_draft=())
            alignment_stage = _DesignToolStage(
                state,
                (
                    _effect(DesignToolEffectKind.ALIGNMENT_CHANGED, ()),
                    _effect(DesignToolEffectKind.ALIGNMENT_DISCARDED),
                ),
            )
        if state.pick_mode is not None:
            effects.append(_effect(DesignToolEffectKind.PICK_MODE_CHANGED, None))
        effects.append(_effect(DesignToolEffectKind.MEASURE_PREVIEW_CHANGED, None))
        state = replace(
            state,
            active_tool=selected,
            pick_mode=None,
            pick_anchor=None,
            ruler_anchor=None if selected == "ruler" else state.ruler_anchor,
            ruler_hover=None,
            measure_preview=None,
            status_message=_tool_status(selected),
        )
        effects.append(_effect(DesignToolEffectKind.ACTIVE_TOOL_CHANGED, selected))
        if selected == "array":
            effects.append(
                _effect(DesignToolEffectKind.MEASURE_PREVIEW_CHANGED, None)
            )
            state = state._with_current_array_preview()
            if state.mixed_array_preview.is_empty:
                effects.append(_effect(DesignToolEffectKind.ROUTE_PREVIEW_CHANGED, None))
            effects.append(
                _effect(
                    DesignToolEffectKind.MIXED_ARRAY_PREVIEW_CHANGED,
                    state.mixed_array_preview,
                )
            )
            return _finish_transition(state, tuple(effects), alignment_stage)
        state = replace(state, mixed_array_preview=MixedArrayPreview())
        effects.extend(
            (
                _effect(DesignToolEffectKind.ROUTE_PREVIEW_CHANGED, None),
                _effect(
                    DesignToolEffectKind.MIXED_ARRAY_PREVIEW_CHANGED,
                    state.mixed_array_preview,
                ),
            )
        )
        if selected == "ruler":
            state = replace(state, pick_mode="ruler")
            effects.append(_effect(DesignToolEffectKind.PICK_MODE_CHANGED, "ruler"))
        else:
            effects.append(_effect(DesignToolEffectKind.MEASURE_PREVIEW_CHANGED, None))
        return _finish_transition(state, tuple(effects), alignment_stage)

    def cancel(self) -> DesignToolTransition:
        state = self
        effects: list[DesignToolEffect] = []
        if state.active_tool == "select":
            if state.pick_mode is not None:
                effects.append(_effect(DesignToolEffectKind.PICK_MODE_CHANGED, None))
            effects.append(_effect(DesignToolEffectKind.MEASURE_PREVIEW_CHANGED, None))
            return DesignToolTransition(
                replace(
                    state,
                    pick_mode=None,
                    pick_anchor=None,
                    measure_preview=None,
                    status_message="",
                ),
                tuple(effects),
            )
        if state.active_tool == "ruler":
            state = replace(
                state,
                ruler_anchor=None,
                ruler_hover=None,
                measure_preview=None,
            )
            effects.append(_effect(DesignToolEffectKind.MEASURE_PREVIEW_CHANGED, None))
        if state.active_tool == "align":
            if state.alignment_draft:
                state = replace(state, alignment_draft=())
                effects.append(_effect(DesignToolEffectKind.ALIGNMENT_CHANGED, ()))
            effects.append(_effect(DesignToolEffectKind.ALIGNMENT_DISCARDED))
        switched = state.activate("select")
        if not effects:
            return switched
        return _staged_transition(
            _DesignToolStage(state, tuple(effects)),
            *_transition_stages(switched),
        )

    def clear_ruler(self) -> DesignToolTransition:
        state = replace(
            self,
            ruler_anchor=None,
            ruler_hover=None,
            ruler_segments=(),
            measure_preview=None,
            status_message="" if self.active_tool == "ruler" else self.status_message,
        )
        return DesignToolTransition(
            state,
            (
                _effect(DesignToolEffectKind.MEASURE_PREVIEW_CHANGED, None),
                _effect(DesignToolEffectKind.MEASUREMENTS_CHANGED, ()),
            ),
        )

    def append_alignment_point(self, point: Point2D) -> DesignToolTransition:
        if self.active_tool != "align":
            return DesignToolTransition(self)
        points = self.alignment_draft + (_point(point),)
        state = replace(self, alignment_draft=points)
        return DesignToolTransition(
            state,
            (_effect(DesignToolEffectKind.ALIGNMENT_CHANGED, points),),
        )

    def undo_alignment_point(self) -> DesignToolTransition:
        if not self.alignment_draft:
            return DesignToolTransition(self)
        points = self.alignment_draft[:-1]
        return DesignToolTransition(
            replace(self, alignment_draft=points),
            (_effect(DesignToolEffectKind.ALIGNMENT_CHANGED, points),),
        )

    def clear_alignment(self, *, discarded: bool = False) -> DesignToolTransition:
        effects: list[DesignToolEffect] = []
        if self.alignment_draft:
            effects.append(_effect(DesignToolEffectKind.ALIGNMENT_CHANGED, ()))
        if discarded:
            effects.append(_effect(DesignToolEffectKind.ALIGNMENT_DISCARDED))
        return DesignToolTransition(
            replace(self, alignment_draft=()),
            tuple(effects),
        )

    def accept_alignment(self) -> DesignToolTransition:
        if self.active_tool != "align" or not self.alignment_valid:
            return DesignToolTransition(self)
        points = self.alignment_draft
        cleared = replace(self, alignment_draft=())
        switched = cleared.activate("select")
        return _staged_transition(
            _DesignToolStage(
                cleared,
                (_effect(DesignToolEffectKind.ALIGNMENT_CHANGED, ()),),
            ),
            *_transition_stages(switched),
            _DesignToolStage(
                switched.session,
                (_effect(DesignToolEffectKind.ALIGNMENT_ACCEPTED, points),),
            ),
        )

    def begin_array_direction(self, mode: str) -> DesignToolTransition:
        if not self.context.has_document:
            return DesignToolTransition(
                replace(self, status_message="Load a design to pick geometry.")
            )
        selected_mode = mode if mode in _ARRAY_PICK_MODES else ""
        if not selected_mode:
            return DesignToolTransition(
                replace(self, status_message="Unknown route pick mode.")
            )
        switched: DesignToolTransition | None = None
        state = self
        if state.active_tool != "array":
            switched = state.activate("array")
            state = switched.session
        state = replace(
            state,
            pick_mode=selected_mode,
            pick_anchor=None,
            measure_preview=None,
            status_message=(
                f"Click design for {route_pick_label_text(selected_mode)}."
            ),
        )
        effects = (
                _effect(DesignToolEffectKind.MEASURE_PREVIEW_CHANGED, None),
                _effect(DesignToolEffectKind.PICK_MODE_CHANGED, selected_mode),
        )
        if switched is None:
            return DesignToolTransition(state, effects)
        return _staged_transition(
            *_transition_stages(switched),
            _DesignToolStage(state, effects),
        )

    def pick(
        self,
        point: Point2D,
        *,
        mode: str | None = None,
        generation: int | None = None,
        shift: bool = False,
        control: bool = False,
    ) -> DesignToolTransition:
        if generation is not None and int(generation) != self.context_generation:
            return DesignToolTransition(self)
        selected_mode = str(mode or self.pick_mode or "")
        selected_point = _point(point)
        if selected_mode == "ruler":
            return self._ruler_pick(
                selected_point,
                shift=bool(shift),
                control=bool(control),
            )
        if selected_mode in _ARRAY_PICK_MODES:
            return self._array_pick(
                selected_mode,
                selected_point,
                shift=bool(shift),
                control=bool(control),
            )
        return DesignToolTransition(
            replace(self, status_message="Unknown route pick mode.")
        )

    def hover(
        self,
        point: Point2D,
        *,
        generation: int,
        shift: bool = False,
        control: bool = False,
    ) -> DesignToolTransition:
        if int(generation) != self.context_generation:
            return DesignToolTransition(self)
        proposed = _point(point)
        if self.active_tool == "ruler" and self.ruler_anchor is not None:
            endpoint = constrain_vector_endpoint(
                self.ruler_anchor,
                proposed,
                shift=bool(shift),
                control=bool(control),
            )
            preview = (self.ruler_anchor, endpoint)
            return DesignToolTransition(
                replace(self, ruler_hover=endpoint, measure_preview=preview),
                (_effect(DesignToolEffectKind.MEASURE_PREVIEW_CHANGED, preview),),
            )
        if (
            self.active_tool != "array"
            or self.pick_mode not in _ARRAY_PICK_MODES
            or self.pick_anchor is None
        ):
            return DesignToolTransition(self)
        endpoint = constrain_vector_endpoint(
            self.pick_anchor,
            proposed,
            shift=bool(shift),
            control=bool(control),
        )
        vector = (
            endpoint[0] - self.pick_anchor[0],
            endpoint[1] - self.pick_anchor[1],
        )
        preview = (self.pick_anchor, endpoint)
        state = replace(self, measure_preview=preview)
        state = state._with_current_array_preview(
            direction_1=vector if self.pick_mode == "array_dir1" else None,
            direction_2=vector if self.pick_mode == "array_dir2" else None,
        )
        return DesignToolTransition(
            state,
            (
                _effect(DesignToolEffectKind.MEASURE_PREVIEW_CHANGED, preview),
                _effect(
                    DesignToolEffectKind.MIXED_ARRAY_PREVIEW_CHANGED,
                    state.mixed_array_preview,
                ),
            ),
        )

    def configure_array(
        self,
        configuration: ArrayToolConfiguration,
    ) -> DesignToolTransition:
        state = replace(self, array_configuration=configuration)
        state = state._with_current_array_preview()
        return DesignToolTransition(
            state,
            (
                _effect(
                    DesignToolEffectKind.MIXED_ARRAY_PREVIEW_CHANGED,
                    state.mixed_array_preview,
                ),
            ),
        )

    def create_array(self) -> DesignToolTransition:
        if not self.context.selection_ids:
            return DesignToolTransition(self)
        request = self.array_configuration.request(self.context.selection_ids)
        switched = self.activate("select")
        return _staged_transition(
            _DesignToolStage(
                self,
                (_effect(DesignToolEffectKind.MIXED_ARRAY_REQUESTED, request),),
            ),
            *_transition_stages(switched),
        )

    def cancel_array(self) -> DesignToolTransition:
        cleared = replace(self, mixed_array_preview=MixedArrayPreview())
        switched = cleared.activate("select")
        return _staged_transition(
            _DesignToolStage(
                cleared,
                (
                _effect(DesignToolEffectKind.ROUTE_PREVIEW_CHANGED, None),
                _effect(
                    DesignToolEffectKind.MIXED_ARRAY_PREVIEW_CHANGED,
                    MixedArrayPreview(),
                ),
                ),
            ),
            *_transition_stages(switched),
        )

    def refresh_array_preview(self) -> DesignToolTransition:
        state = self._with_current_array_preview()
        effects: tuple[DesignToolEffect, ...]
        if state.mixed_array_preview.is_empty:
            effects = (
                _effect(DesignToolEffectKind.ROUTE_PREVIEW_CHANGED, None),
                _effect(
                    DesignToolEffectKind.MIXED_ARRAY_PREVIEW_CHANGED,
                    state.mixed_array_preview,
                ),
            )
        else:
            effects = (
                _effect(
                    DesignToolEffectKind.MIXED_ARRAY_PREVIEW_CHANGED,
                    state.mixed_array_preview,
                ),
            )
        return DesignToolTransition(state, effects)

    def _ruler_pick(
        self,
        point: Point2D,
        *,
        shift: bool,
        control: bool,
    ) -> DesignToolTransition:
        if self.ruler_anchor is None:
            preview = (point,)
            state = replace(
                self,
                pick_mode="ruler",
                ruler_anchor=point,
                ruler_hover=None,
                measure_preview=preview,
            )
            return DesignToolTransition(
                state,
                (
                    _effect(DesignToolEffectKind.MEASURE_PREVIEW_CHANGED, preview),
                    _effect(DesignToolEffectKind.PICK_MODE_CHANGED, "ruler"),
                ),
            )
        endpoint = constrain_vector_endpoint(
            self.ruler_anchor,
            point,
            shift=shift,
            control=control,
        )
        segments = self.ruler_segments + ((self.ruler_anchor, endpoint),)
        state = replace(
            self,
            pick_mode="ruler",
            ruler_anchor=None,
            ruler_hover=None,
            ruler_segments=segments,
            measure_preview=None,
        )
        return DesignToolTransition(
            state,
            (
                _effect(DesignToolEffectKind.MEASUREMENTS_CHANGED, segments),
                _effect(DesignToolEffectKind.MEASURE_PREVIEW_CHANGED, None),
                _effect(DesignToolEffectKind.PICK_MODE_CHANGED, "ruler"),
            ),
        )

    def _array_pick(
        self,
        mode: str,
        point: Point2D,
        *,
        shift: bool,
        control: bool,
    ) -> DesignToolTransition:
        if self.pick_mode != mode or self.pick_anchor is None:
            preview = (point,)
            label = "Direction 1" if mode == "array_dir1" else "Direction 2"
            state = replace(
                self,
                pick_mode=mode,
                pick_anchor=point,
                measure_preview=preview,
                status_message=(
                    f"{label} vector starts at {format_point(point)}; click endpoint."
                ),
            )
            return DesignToolTransition(
                state,
                (_effect(DesignToolEffectKind.MEASURE_PREVIEW_CHANGED, preview),),
            )
        endpoint = constrain_vector_endpoint(
            self.pick_anchor,
            point,
            shift=shift,
            control=control,
        )
        vector = (
            endpoint[0] - self.pick_anchor[0],
            endpoint[1] - self.pick_anchor[1],
        )
        configuration = self.array_configuration.with_direction(mode, vector)
        length, angle = vector_length_angle(vector)
        label = "Direction 1" if mode == "array_dir1" else "Direction 2"
        state = replace(
            self,
            array_configuration=configuration,
            pick_mode=None,
            pick_anchor=None,
            measure_preview=None,
            status_message=(
                f"{label} set to length={length:.3f}, angle={angle:.3f} deg."
            ),
        )
        state = state._with_current_array_preview()
        return DesignToolTransition(
            state,
            (
                _effect(DesignToolEffectKind.PICK_MODE_CHANGED, None),
                _effect(DesignToolEffectKind.MEASURE_PREVIEW_CHANGED, None),
                _effect(
                    DesignToolEffectKind.MIXED_ARRAY_PREVIEW_CHANGED,
                    state.mixed_array_preview,
                ),
            ),
        )

    def _with_current_array_preview(
        self,
        *,
        direction_1: Point2D | None = None,
        direction_2: Point2D | None = None,
    ) -> DesignToolSession:
        if (
            not self.context.has_document
            or self.active_tool != "array"
            or not self.context.selection_ids
        ):
            return replace(self, mixed_array_preview=MixedArrayPreview())
        request = self.array_configuration.request(
            self.context.selection_ids,
            direction_1=direction_1,
            direction_2=direction_2,
        )
        plan = self.context.plan_array(request)
        if not plan.accepted:
            return replace(self, mixed_array_preview=MixedArrayPreview())
        preview = MixedArrayPreview(
            route_points=tuple(point.camera_center for point in plan.route_copies),
            guide_segments=tuple(guide.geometry() for guide in plan.guide_copies),
        )
        return replace(self, mixed_array_preview=preview)


def _finish_transition(
    session: DesignToolSession,
    effects: tuple[DesignToolEffect, ...],
    first_stage: _DesignToolStage | None,
) -> DesignToolTransition:
    if first_stage is None:
        return DesignToolTransition(session, effects)
    return _staged_transition(
        first_stage,
        _DesignToolStage(session, effects),
    )


def _transition_stages(
    transition: DesignToolTransition,
) -> tuple[_DesignToolStage, ...]:
    if transition.stages:
        return transition.stages
    return (_DesignToolStage(transition.session, transition.effects),)


def _staged_transition(*stages: _DesignToolStage) -> DesignToolTransition:
    effects = tuple(effect for stage in stages for effect in stage.effects)
    return DesignToolTransition(
        session=stages[-1].session,
        effects=effects,
        stages=tuple(stages),
    )


def _effect(kind: DesignToolEffectKind, value: object = None) -> DesignToolEffect:
    return DesignToolEffect(kind, value)


def _point(value: Point2D) -> Point2D:
    return (float(value[0]), float(value[1]))


def _tool_status(tool: str) -> str:
    if tool == "guide":
        return "Click two points."
    if tool == "array":
        return "Adjust array directions and counts."
    return ""


__all__ = [
    "ArrayToolConfiguration",
    "DesignToolContext",
    "DesignToolEffect",
    "DesignToolEffectKind",
    "DesignToolSession",
    "DesignToolTransition",
    "MixedArrayPreview",
    "RulerReadout",
]
