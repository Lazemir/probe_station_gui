"""Qt-free Design plot input and gesture policy."""

from __future__ import annotations

from dataclasses import dataclass, replace
from enum import Enum

from .model import Point2D, SnapResult
from .selection_geometry import SelectionRect, constrain_vector_endpoint
from .selection_model import SelectableDesignEntity, entities_in_rect


class MouseButton(str, Enum):
    LEFT = "left"
    RIGHT = "right"
    OTHER = "other"


class PlotAction(str, Enum):
    ALIGNMENT_POINT = "alignment_point"
    CALIBRATION = "calibration"
    GUIDE_POINT = "guide_point"
    MOVE = "move"
    POINT = "point"
    ROUTE_PICK = "route_pick"
    ROUTE_POINT = "route_point"
    SELECT = "select"


@dataclass(frozen=True)
class PointerModifiers:
    shift: bool = False
    control: bool = False
    other: bool = False


@dataclass(frozen=True)
class PlotClick:
    button: MouseButton
    point: Point2D
    modifiers: PointerModifiers = PointerModifiers()
    double: bool = False


@dataclass(frozen=True)
class SnapClickIntent:
    action: PlotAction
    raw_point: Point2D
    payload: tuple[object, ...]
    shift: bool
    control: bool
    generation: int


@dataclass(frozen=True)
class SnapHoverIntent:
    raw_point: Point2D
    shift: bool
    control: bool
    generation: int


@dataclass(frozen=True)
class ClickEffect:
    action: PlotAction
    point: Point2D
    payload: tuple[object, ...] = ()
    shift: bool = False
    control: bool = False
    points: tuple[Point2D, ...] = ()


@dataclass(frozen=True)
class HoverEffect:
    result: SnapResult | None
    shift: bool = False
    control: bool = False


@dataclass(frozen=True)
class GuidePreview:
    points: tuple[Point2D, ...]


@dataclass(frozen=True)
class SelectionClick:
    point: Point2D
    modifiers: PointerModifiers


@dataclass(frozen=True)
class SelectionPreview:
    start: Point2D | None
    end: Point2D | None
    crossing: bool


@dataclass(frozen=True)
class SelectionRequest:
    entity_ids: frozenset[str]
    mode: str


@dataclass(frozen=True)
class ScheduleMoveSuppressionClear:
    generation: int


PlotEffect = (
    ClickEffect
    | GuidePreview
    | HoverEffect
    | ScheduleMoveSuppressionClear
    | SelectionClick
    | SelectionPreview
    | SelectionRequest
    | SnapClickIntent
)


@dataclass(frozen=True)
class InteractionTransition:
    generation: int
    effects: tuple[PlotEffect, ...] = ()
    invalidate_transient_snaps: bool = False


@dataclass(frozen=True)
class _InteractionState:
    active_tool: str = "legacy"
    document_present: bool = False
    preview_active: bool = False
    document_generation: int = 0
    navigation_enabled: bool = False
    route_edit_enabled: bool = False
    route_pick_mode: str | None = None
    guide_anchor: Point2D | None = None
    generation: int = 0
    selection_scene_start: Point2D | None = None
    selection_design_start: Point2D | None = None
    selection_entity_id: str | None = None
    selection_modifiers: PointerModifiers = PointerModifiers()
    selection_dragging: bool = False
    move_scene_start: Point2D | None = None
    move_dragging: bool = False
    suppress_move_click: bool = False


_TOOLS = frozenset(
    {"select", "move", "point", "guide", "ruler", "array", "align", "legacy"}
)
_TRANSIENT_ACTIONS = frozenset(
    {
        PlotAction.ALIGNMENT_POINT,
        PlotAction.GUIDE_POINT,
        PlotAction.MOVE,
        PlotAction.POINT,
        PlotAction.ROUTE_PICK,
    }
)


def is_transient_action(action: PlotAction) -> bool:
    return action in _TRANSIENT_ACTIONS


class PlotInteraction:
    """Own mutable input-session state behind immutable transitions."""

    def __init__(self) -> None:
        self._state = _InteractionState()

    @property
    def active_tool(self) -> str:
        return self._state.active_tool

    @property
    def generation(self) -> int:
        return self._state.generation

    @property
    def guide_anchor(self) -> Point2D | None:
        return self._state.guide_anchor

    @property
    def selection_active(self) -> bool:
        return self._state.selection_scene_start is not None

    @property
    def move_active(self) -> bool:
        return self._state.move_scene_start is not None

    def set_context(
        self,
        *,
        document_present: bool,
        preview_active: bool,
        document_generation: int | None = None,
    ) -> InteractionTransition:
        generation = (
            self._state.document_generation
            if document_generation is None
            else int(document_generation)
        )
        reset_session = (
            generation != self._state.document_generation
            or not document_present
            or preview_active
        )
        values = {
            "document_present": bool(document_present),
            "preview_active": bool(preview_active),
            "document_generation": generation,
        }
        effects: tuple[PlotEffect, ...] = ()
        if reset_session:
            effects = self._reset_preview_effects()
            values.update(
                guide_anchor=None,
                selection_scene_start=None,
                selection_design_start=None,
                selection_entity_id=None,
                selection_dragging=False,
                move_scene_start=None,
                move_dragging=False,
                suppress_move_click=False,
            )
        return self._replace_context(effects=effects, **values)

    def set_tool(self, tool: str) -> InteractionTransition:
        normalized = str(tool).strip().lower()
        if normalized not in _TOOLS:
            raise ValueError(f"Unknown design tool {tool!r}.")
        if normalized == self._state.active_tool:
            return self._transition()
        effects = tuple(
            effect
            for effect in self._reset_preview_effects()
            if not isinstance(effect, GuidePreview) or normalized != "guide"
        )
        return self._replace_context(
            active_tool=normalized,
            guide_anchor=None if normalized != "guide" else self._state.guide_anchor,
            selection_scene_start=None,
            selection_design_start=None,
            selection_entity_id=None,
            selection_dragging=False,
            move_scene_start=None,
            move_dragging=False,
            suppress_move_click=False,
            effects=effects,
        )

    def set_navigation_enabled(self, enabled: bool) -> InteractionTransition:
        return self._replace_context(navigation_enabled=bool(enabled))

    def set_route_edit_enabled(self, enabled: bool) -> InteractionTransition:
        return self._replace_context(route_edit_enabled=bool(enabled))

    def set_route_pick_mode(self, mode: object) -> InteractionTransition:
        value = str(mode) if mode else None
        return self._replace_context(route_pick_mode=value)

    def click(self, click: PlotClick) -> InteractionTransition:
        if not self._input_available():
            return self._transition()
        action, payload = self._classify_click(click)
        if action is None:
            return self._transition()
        if action is PlotAction.SELECT:
            return self._transition(
                effects=(SelectionClick(click.point, click.modifiers),)
            )
        intent = SnapClickIntent(
            action=action,
            raw_point=click.point,
            payload=payload,
            shift=click.modifiers.shift,
            control=click.modifiers.control,
            generation=self._state.generation,
        )
        return self._transition(effects=(intent,))

    def hover(
        self,
        result: SnapResult | None,
        *,
        shift: bool = False,
        control: bool = False,
    ) -> InteractionTransition:
        return self.complete_hover(
            SnapHoverIntent(
                raw_point=(0.0, 0.0) if result is None else result.point,
                shift=bool(shift),
                control=bool(control),
                generation=self._state.generation,
            ),
            result,
        )

    def complete_hover(
        self,
        intent: SnapHoverIntent,
        result: SnapResult | None,
    ) -> InteractionTransition:
        if intent.generation != self._state.generation or not self._input_available():
            return self._transition()
        effects: list[PlotEffect] = [HoverEffect(result, intent.shift, intent.control)]
        anchor = self._state.guide_anchor
        if self._state.active_tool == "guide" and anchor is not None:
            points = (anchor,)
            if result is not None:
                endpoint = constrain_vector_endpoint(
                    anchor,
                    result.point,
                    shift=intent.shift,
                    control=intent.control,
                )
                points = (anchor, endpoint)
            effects.append(GuidePreview(points))
        return self._transition(effects=tuple(effects))

    def complete_click(
        self,
        intent: SnapClickIntent,
        result: SnapResult,
    ) -> InteractionTransition:
        if not self._input_available() or (
            intent.generation != self._state.generation
            and is_transient_action(intent.action)
        ):
            return self._transition()
        if intent.action is PlotAction.GUIDE_POINT:
            return self._complete_guide(intent, result)
        effect = ClickEffect(
            action=intent.action,
            point=result.point,
            payload=intent.payload,
            shift=intent.shift,
            control=intent.control,
            points=(result.point,),
        )
        return self._transition(effects=(effect,))

    def cancel(self) -> InteractionTransition:
        effects = self._reset_preview_effects()
        self._state = replace(
            self._state,
            guide_anchor=None,
            selection_scene_start=None,
            selection_design_start=None,
            selection_entity_id=None,
            selection_dragging=False,
            move_scene_start=None,
            move_dragging=False,
            suppress_move_click=False,
            generation=self._state.generation + 1,
        )
        return self._transition(effects=effects, invalidate=True)

    def selection_press(
        self,
        *,
        scene_point: Point2D,
        design_point: Point2D,
        hit_entity_id: str | None,
        modifiers: PointerModifiers,
    ) -> InteractionTransition:
        if self._state.active_tool != "select" or not self._input_available():
            return self._transition()
        self._state = replace(
            self._state,
            selection_scene_start=scene_point,
            selection_design_start=design_point,
            selection_entity_id=hit_entity_id,
            selection_modifiers=modifiers,
            selection_dragging=False,
        )
        return self._transition()

    def selection_motion(
        self,
        *,
        scene_point: Point2D,
        design_point: Point2D,
        drag_threshold: float,
    ) -> InteractionTransition:
        start = self._state.selection_scene_start
        design_start = self._state.selection_design_start
        if start is None or design_start is None or self._state.selection_entity_id:
            return self._transition()
        distance = abs(scene_point[0] - start[0]) + abs(scene_point[1] - start[1])
        dragging = self._state.selection_dragging or distance >= float(drag_threshold)
        self._state = replace(self._state, selection_dragging=dragging)
        if not dragging:
            return self._transition()
        return self._transition(
            effects=(
                SelectionPreview(
                    design_start,
                    design_point,
                    design_point[0] < design_start[0],
                ),
            )
        )

    def selection_release(
        self,
        design_point: Point2D,
        entities: tuple[SelectableDesignEntity, ...],
    ) -> InteractionTransition:
        start = self._state.selection_design_start
        hit_id = self._state.selection_entity_id
        modifiers = self._state.selection_modifiers
        dragging = self._state.selection_dragging
        if self._state.selection_scene_start is None:
            return self._transition()
        matched: set[str]
        if dragging and start is not None:
            matched = entities_in_rect(
                entities,
                SelectionRect.from_drag(start, design_point),
                crossing=design_point[0] < start[0],
            )
        elif hit_id is not None:
            matched = {hit_id}
        else:
            matched = set()
        self._state = replace(
            self._state,
            selection_scene_start=None,
            selection_design_start=None,
            selection_entity_id=None,
            selection_dragging=False,
        )
        return self._transition(
            effects=(
                SelectionPreview(None, None, False),
                SelectionRequest(frozenset(matched), self._selection_mode(modifiers)),
            )
        )

    def move_press(self, scene_point: Point2D) -> InteractionTransition:
        if self._state.active_tool != "move" or not self._input_available():
            return self._transition()
        self._state = replace(
            self._state,
            move_scene_start=scene_point,
            move_dragging=False,
        )
        return self._transition()

    def move_motion(
        self,
        scene_point: Point2D,
        *,
        drag_threshold: float,
    ) -> InteractionTransition:
        start = self._state.move_scene_start
        if start is None:
            return self._transition()
        distance = abs(scene_point[0] - start[0]) + abs(scene_point[1] - start[1])
        self._state = replace(
            self._state,
            move_dragging=(
                self._state.move_dragging or distance >= float(drag_threshold)
            ),
        )
        return self._transition()

    def move_release(self) -> InteractionTransition:
        if self._state.move_scene_start is None:
            return self._transition()
        suppressed = self._state.move_dragging
        self._state = replace(
            self._state,
            move_scene_start=None,
            move_dragging=False,
            suppress_move_click=suppressed,
        )
        effects: tuple[PlotEffect, ...] = ()
        if suppressed:
            effects = (ScheduleMoveSuppressionClear(self._state.generation),)
        return self._transition(effects=effects)

    def clear_move_suppression(self, generation: int | None = None) -> None:
        if generation is not None and generation != self._state.generation:
            return
        self._state = replace(self._state, suppress_move_click=False)

    def _classify_click(
        self,
        click: PlotClick,
    ) -> tuple[PlotAction | None, tuple[object, ...]]:
        state = self._state
        left = click.button is MouseButton.LEFT
        if state.active_tool == "move":
            accepted = (
                state.navigation_enabled
                and left
                and not click.double
                and not click.modifiers.shift
                and not click.modifiers.control
                and not click.modifiers.other
                and not state.suppress_move_click
            )
            return (PlotAction.MOVE, ()) if accepted else (None, ())
        if state.active_tool == "align":
            return (PlotAction.ALIGNMENT_POINT, ()) if left else (None, ())
        if state.route_pick_mode is not None and left:
            return PlotAction.ROUTE_PICK, (state.route_pick_mode,)
        if state.active_tool == "guide" and state.route_edit_enabled and left:
            return PlotAction.GUIDE_POINT, ()
        if (
            state.route_edit_enabled
            and left
            and state.active_tool in {"legacy", "point"}
        ):
            action = (
                PlotAction.POINT
                if state.active_tool == "point"
                else PlotAction.ROUTE_POINT
            )
            return action, ()
        if state.active_tool == "select" and left:
            return PlotAction.SELECT, ()
        if state.active_tool == "legacy" and left:
            return PlotAction.CALIBRATION, (0,)
        if state.active_tool == "legacy" and click.button is MouseButton.RIGHT:
            return PlotAction.CALIBRATION, (1,)
        return None, ()

    def _complete_guide(
        self,
        intent: SnapClickIntent,
        result: SnapResult,
    ) -> InteractionTransition:
        if self._state.active_tool != "guide" or not self._state.route_edit_enabled:
            return self._transition()
        anchor = self._state.guide_anchor
        if anchor is None:
            self._state = replace(self._state, guide_anchor=result.point)
            return self._transition(effects=(GuidePreview((result.point,)),))
        endpoint = constrain_vector_endpoint(
            anchor,
            result.point,
            shift=intent.shift,
            control=intent.control,
        )
        self._state = replace(self._state, guide_anchor=None)
        effects: list[PlotEffect] = [GuidePreview(())]
        if anchor != endpoint:
            effects.append(
                ClickEffect(
                    action=PlotAction.GUIDE_POINT,
                    point=endpoint,
                    shift=intent.shift,
                    control=intent.control,
                    points=(anchor, endpoint),
                )
            )
        return self._transition(effects=tuple(effects))

    def _replace_context(
        self,
        *,
        effects: tuple[PlotEffect, ...] = (),
        **changes: object,
    ) -> InteractionTransition:
        unchanged = all(
            getattr(self._state, name) == value for name, value in changes.items()
        )
        if unchanged:
            return self._transition(effects=effects)
        self._state = replace(
            self._state,
            **changes,
            generation=self._state.generation + 1,
        )
        return self._transition(effects=effects, invalidate=True)

    def _transition(
        self,
        *,
        effects: tuple[PlotEffect, ...] = (),
        invalidate: bool = False,
    ) -> InteractionTransition:
        return InteractionTransition(
            generation=self._state.generation,
            effects=effects,
            invalidate_transient_snaps=bool(invalidate),
        )

    def _input_available(self) -> bool:
        return self._state.document_present and not self._state.preview_active

    def _reset_preview_effects(self) -> tuple[PlotEffect, ...]:
        effects: list[PlotEffect] = []
        if self._state.guide_anchor is not None:
            effects.append(GuidePreview(()))
        if self._state.selection_scene_start is not None:
            effects.append(SelectionPreview(None, None, False))
        return tuple(effects)

    @staticmethod
    def _selection_mode(modifiers: PointerModifiers) -> str:
        if modifiers.control:
            return "invert"
        if modifiers.shift:
            return "add"
        return "replace"


__all__ = [
    "ClickEffect",
    "GuidePreview",
    "HoverEffect",
    "InteractionTransition",
    "is_transient_action",
    "MouseButton",
    "PlotAction",
    "PlotClick",
    "PlotInteraction",
    "PointerModifiers",
    "ScheduleMoveSuppressionClear",
    "SelectionClick",
    "SelectionPreview",
    "SelectionRequest",
    "SnapClickIntent",
    "SnapHoverIntent",
]
