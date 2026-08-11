"""Qt-free orchestration for Design snap requests and ordered results."""

from __future__ import annotations

from collections import deque
from collections.abc import Callable, Iterable
from dataclasses import dataclass, replace
import math

from .klayout_geometry import plan_snap_search
from .klayout_types import KLayoutConfig, SnapFailure, SnapRequest, SnapResponse
from .model import Point2D, SnapResult
from .plot_interaction import SnapClickIntent, SnapHoverIntent, is_transient_action


from .snap_protocol import (
    AttachWorker,
    CancelHover,
    CancelPending,
    ClickPublication,
    DetachWorker,
    HoverPublication,
    ReplaceWorker,
    SnapCommand,
    SnapNotice,
    SnapPublication,
    SnapTransition,
    SnapWorkerEvent,
    SourceKey,
    SubmitClick,
    SubmitHover,
    WorkerFailureEvent,
    WorkerLifecycleFailedEvent,
    WorkerResponseEvent,
)


ScreenDistance = Callable[[Point2D, Point2D], float]


@dataclass(frozen=True)
class _PendingHover:
    intent: SnapHoverIntent
    config_generation: int
    document_generation: int
    worker_token: int
    markup_result: SnapResult | None


@dataclass(frozen=True)
class _PendingClick:
    request_id: int
    intent: SnapClickIntent
    config_generation: int
    document_generation: int
    worker_token: int
    markup_generation: int
    markup_result: SnapResult | None


class SnapCoordinator:
    """Own request identity, invalidation, arbitration, and click FIFO policy."""

    def __init__(self, *, screen_distance: ScreenDistance | None = None) -> None:
        self._screen_distance = screen_distance or _euclidean_distance
        self._enabled = True
        self._config: KLayoutConfig | None = None
        self._source_key: SourceKey | None = None
        self._worker_token = 0
        self._worker_counter = 0
        self._request_id = 0
        self._document_generation = 0
        self._interaction_generation: int | None = None
        self._markup_generation = 0
        self._markup_candidates: tuple[SnapResult, ...] = ()
        self._latest_hover_request_id = 0
        self._pending_hovers: dict[int, _PendingHover] = {}
        self._pending_clicks: dict[int, _PendingClick] = {}
        self._click_order: deque[int] = deque()
        self._click_completions: dict[int, tuple[SnapResult | None, float | None]] = {}
        self._closed = False

    @property
    def enabled(self) -> bool:
        return self._enabled

    @property
    def config(self) -> KLayoutConfig | None:
        return self._config

    @property
    def worker_token(self) -> int:
        return self._worker_token

    @property
    def document_generation(self) -> int:
        return self._document_generation

    def geometry_is_current(self, generation: int) -> bool:
        return not self._closed and int(generation) == self._document_generation

    def resolve_local(
        self,
        raw_point: Point2D,
        *,
        radius: float,
        geometry_result: SnapResult | None,
    ) -> SnapResult:
        free = SnapResult(raw_point, "free", 0.0)
        accepted_geometry = geometry_result
        if geometry_result is not None and geometry_result.distance > max(
            0.0, float(radius)
        ):
            accepted_geometry = free
        return self._nearest(
            raw_point,
            free,
            accepted_geometry,
            self._best_markup(raw_point, radius),
        )

    def configure(self, config: KLayoutConfig | None) -> SnapTransition:
        if self._closed:
            return SnapTransition()
        if config is None:
            return self._detach()
        source_key: SourceKey = (config.path, config.source_load_id)
        previous_source = self._source_key
        self._config = config
        self._source_key = source_key
        self._discard_requests()
        if self._worker_token and source_key == previous_source:
            return SnapTransition()
        self._worker_counter += 1
        previous_token = self._worker_token
        self._worker_token = self._worker_counter
        if previous_token:
            command: SnapCommand = ReplaceWorker(
                self._worker_token,
                source_key,
                0.0,
            )
        else:
            command = AttachWorker(self._worker_token, source_key)
        return SnapTransition(commands=(command,))

    def reset_document(self) -> SnapTransition:
        if self._closed:
            return SnapTransition()
        self._document_generation += 1
        self._discard_requests()
        commands: tuple[SnapCommand, ...] = ()
        if self._worker_token:
            commands = (CancelPending(self._worker_token),)
        return SnapTransition(
            commands=commands,
            publications=(HoverPublication(None, False, False, None, None),),
        )

    def set_markup_candidates(self, candidates: Iterable[object]) -> SnapTransition:
        if self._closed:
            return SnapTransition()
        self._markup_generation += 1
        self._markup_candidates = tuple(
            result
            for candidate in candidates
            if (result := _candidate_result(candidate)) is not None
        )
        self._pending_hovers = {
            request_id: _PendingHover(
                intent=pending.intent,
                config_generation=pending.config_generation,
                document_generation=pending.document_generation,
                worker_token=pending.worker_token,
                markup_result=None,
            )
            for request_id, pending in self._pending_hovers.items()
        }
        return SnapTransition()

    def set_enabled(self, enabled: bool) -> SnapTransition:
        if self._closed:
            return SnapTransition()
        normalized = bool(enabled)
        if normalized == self._enabled:
            return SnapTransition()
        self._enabled = normalized
        self._markup_generation += 1
        self._discard_requests()
        commands: tuple[SnapCommand, ...] = ()
        if not normalized and self._worker_token:
            commands = (CancelPending(self._worker_token),)
        publications: tuple[SnapPublication, ...] = ()
        if not normalized:
            publications = (HoverPublication(None, False, False, None, None),)
        return SnapTransition(commands=commands, publications=publications)

    def set_interaction_generation(self, generation: int) -> SnapTransition:
        if self._closed:
            return SnapTransition()
        normalized = int(generation)
        if normalized == self._interaction_generation:
            return SnapTransition()
        self._interaction_generation = normalized
        self._latest_hover_request_id = 0
        self._pending_hovers.clear()
        publications = self._cancel_clicks(
            lambda pending: is_transient_action(pending.intent.action)
        )
        commands: tuple[SnapCommand, ...] = ()
        if self._worker_token:
            commands = (CancelHover(self._worker_token),)
        return SnapTransition(commands=commands, publications=publications)

    def submit_hover(
        self,
        intent: SnapHoverIntent,
        *,
        radius: float,
    ) -> SnapTransition:
        if not self._accept_generation(intent.generation):
            return SnapTransition()
        if not self._enabled:
            return SnapTransition(
                publications=(HoverPublication(None, False, False, None, None),)
            )
        config = self._config
        if config is None:
            return SnapTransition(
                publications=(HoverPublication(None, False, False, None, None),)
            )
        plan = plan_snap_search(
            intent.raw_point, max(0.0, float(radius)), config.display_bounds
        )
        if not plan.accepted:
            self._latest_hover_request_id = 0
            self._pending_hovers.clear()
            result = self._fallback(intent.raw_point, radius)
            return SnapTransition(
                commands=(CancelHover(self._worker_token),),
                publications=(
                    HoverPublication(
                        result,
                        intent.shift,
                        intent.control,
                        intent.raw_point,
                        None,
                        intent.generation,
                    ),
                ),
            )
        attach = self._ensure_worker()
        request = self._new_request(intent.raw_point, radius, purpose="hover")
        self._latest_hover_request_id = request.request_id
        self._pending_hovers = {
            request.request_id: _PendingHover(
                intent=intent,
                config_generation=config.generation,
                document_generation=self._document_generation,
                worker_token=self._worker_token,
                markup_result=self._best_markup(intent.raw_point, radius),
            )
        }
        return SnapTransition(
            commands=(*attach, SubmitHover(self._worker_token, request))
        )

    def submit_click(
        self,
        intent: SnapClickIntent,
        *,
        radius: float,
    ) -> SnapTransition:
        if not self._accept_generation(intent.generation):
            return SnapTransition()
        free = SnapResult(intent.raw_point, "free", 0.0)
        if not self._enabled:
            return SnapTransition(publications=(ClickPublication(intent, free, None),))
        config = self._config
        if config is None:
            return SnapTransition()
        request = self._new_request(intent.raw_point, radius, purpose="click")
        plan = plan_snap_search(
            intent.raw_point, max(0.0, float(radius)), config.display_bounds
        )
        attach = self._ensure_worker() if plan.accepted else ()
        pending = _PendingClick(
            request_id=request.request_id,
            intent=intent,
            config_generation=config.generation,
            document_generation=self._document_generation,
            worker_token=self._worker_token,
            markup_generation=self._markup_generation,
            markup_result=(
                self._best_markup(intent.raw_point, radius) if plan.accepted else None
            ),
        )
        self._pending_clicks[request.request_id] = pending
        self._click_order.append(request.request_id)
        if plan.accepted:
            return SnapTransition(
                commands=(*attach, SubmitClick(self._worker_token, request))
            )
        hover = self.cancel_hover()
        fallback = self._fallback(intent.raw_point, radius)
        publications = self._complete_click(request.request_id, fallback, None)
        return SnapTransition(
            commands=hover.commands,
            publications=publications,
        )

    def cancel_hover(self) -> SnapTransition:
        if self._closed:
            return SnapTransition()
        self._latest_hover_request_id = 0
        self._pending_hovers.clear()
        commands: tuple[SnapCommand, ...] = ()
        if self._worker_token:
            commands = (CancelHover(self._worker_token),)
        return SnapTransition(commands=commands)

    def cancel_transient_clicks(self) -> SnapTransition:
        if self._closed:
            return SnapTransition()
        publications = self._cancel_clicks(
            lambda pending: is_transient_action(pending.intent.action)
        )
        return SnapTransition(publications=publications)

    def continue_ready_clicks(self) -> SnapTransition:
        """Publish at most one ready click after the adapter rechecks context."""

        if self._closed:
            return SnapTransition()
        return SnapTransition(publications=self._drain_clicks())

    def worker_event(self, event: SnapWorkerEvent) -> SnapTransition:
        if isinstance(event, WorkerResponseEvent):
            return self.worker_response(event.response, worker_token=event.worker_token)
        if isinstance(event, WorkerFailureEvent):
            return self.worker_failure(event.failure, worker_token=event.worker_token)
        return self.worker_lifecycle_failed(event.worker_token, event.message)

    def worker_response(
        self,
        response: SnapResponse,
        *,
        worker_token: int | None = None,
    ) -> SnapTransition:
        token = self._worker_token if worker_token is None else int(worker_token)
        if self._closed or token != self._worker_token:
            return SnapTransition()
        config = self._config
        if config is None or response.config_generation != config.generation:
            return SnapTransition()
        if response.purpose == "hover":
            return self._complete_hover_response(response, token)
        if response.purpose != "click":
            return SnapTransition()
        pending = self._pending_clicks.get(response.request_id)
        if pending is None:
            return SnapTransition()
        if not self._pending_click_is_current(pending, token):
            return SnapTransition(
                publications=self._complete_click(response.request_id, None, None)
            )
        result = self._nearest(
            pending.intent.raw_point,
            response.result,
            pending.markup_result,
        )
        publications = self._complete_click(
            response.request_id,
            result,
            response.elapsed_ms,
        )
        return SnapTransition(publications=publications)

    def worker_failure(
        self,
        failure: SnapFailure,
        *,
        worker_token: int | None = None,
    ) -> SnapTransition:
        token = self._worker_token if worker_token is None else int(worker_token)
        if self._closed or token != self._worker_token:
            return SnapTransition()
        config = self._config
        if config is None or failure.config_generation != config.generation:
            return SnapTransition()
        if failure.purpose == "hover":
            if failure.request_id != self._latest_hover_request_id:
                return SnapTransition()
            pending = self._pending_hovers.pop(failure.request_id, None)
            self._latest_hover_request_id = 0
            if pending is None:
                return SnapTransition()
            return SnapTransition(
                publications=(
                    HoverPublication(
                        None,
                        pending.intent.shift,
                        pending.intent.control,
                        pending.intent.raw_point,
                        None,
                        pending.intent.generation,
                    ),
                )
            )
        if failure.purpose != "click":
            return SnapTransition()
        pending = self._pending_clicks.get(failure.request_id)
        if pending is None:
            return SnapTransition()
        if not self._pending_click_is_current(pending, token):
            return SnapTransition(
                publications=self._complete_click(failure.request_id, None, None)
            )
        publications = self._complete_click(failure.request_id, None, None)
        return SnapTransition(
            publications=publications,
            notices=(SnapNotice("click_failed", failure.message),),
        )

    def worker_lifecycle_failed(
        self,
        worker_token: int,
        message: str,
    ) -> SnapTransition:
        if self._closed or int(worker_token) != self._worker_token:
            return SnapTransition()
        token = self._worker_token
        self._worker_token = 0
        self._discard_requests()
        return SnapTransition(
            commands=(DetachWorker(token, 0.0),),
            publications=(HoverPublication(None, False, False, None, None),),
            notices=(SnapNotice("lifecycle_failed", str(message)),),
        )

    def close(self) -> SnapTransition:
        if self._closed:
            return SnapTransition()
        token = self._worker_token
        self._closed = True
        self._discard_requests()
        self._config = None
        self._source_key = None
        self._worker_token = 0
        if not token:
            return SnapTransition()
        return SnapTransition(
            commands=(CancelPending(token), DetachWorker(token, 0.0)),
        )

    def _complete_hover_response(
        self,
        response: SnapResponse,
        token: int,
    ) -> SnapTransition:
        if response.request_id != self._latest_hover_request_id:
            return SnapTransition()
        pending = self._pending_hovers.pop(response.request_id, None)
        self._latest_hover_request_id = 0
        if pending is None or not self._pending_hover_is_current(pending, token):
            return SnapTransition()
        result = self._nearest(
            pending.intent.raw_point,
            response.result,
            pending.markup_result,
        )
        publication = HoverPublication(
            result,
            pending.intent.shift,
            pending.intent.control,
            pending.intent.raw_point,
            response.elapsed_ms,
            pending.intent.generation,
        )
        return SnapTransition(publications=(publication,))

    def _pending_hover_is_current(
        self,
        pending: _PendingHover,
        token: int,
    ) -> bool:
        config = self._config
        return bool(
            config is not None
            and pending.config_generation == config.generation
            and pending.document_generation == self._document_generation
            and pending.worker_token == token
            and self._accept_generation(pending.intent.generation)
        )

    def _pending_click_is_current(
        self,
        pending: _PendingClick,
        token: int,
    ) -> bool:
        config = self._config
        return bool(
            config is not None
            and pending.config_generation == config.generation
            and pending.document_generation == self._document_generation
            and pending.worker_token == token
            and pending.markup_generation == self._markup_generation
            and (
                not is_transient_action(pending.intent.action)
                or self._accept_generation(pending.intent.generation)
            )
        )

    def _complete_click(
        self,
        request_id: int,
        result: SnapResult | None,
        elapsed_ms: float | None,
    ) -> tuple[SnapPublication, ...]:
        if (
            request_id not in self._pending_clicks
            or request_id in self._click_completions
        ):
            return ()
        self._click_completions[request_id] = (result, elapsed_ms)
        return self._drain_clicks()

    def _drain_clicks(self) -> tuple[SnapPublication, ...]:
        while self._click_order and self._click_order[0] in self._click_completions:
            request_id = self._click_order.popleft()
            result, elapsed_ms = self._click_completions.pop(request_id)
            pending = self._pending_clicks.pop(request_id, None)
            if pending is None or result is None:
                continue
            if not self._pending_click_is_current(pending, pending.worker_token):
                continue
            return (ClickPublication(pending.intent, result, elapsed_ms),)
        return ()

    def _cancel_clicks(
        self,
        predicate: Callable[[_PendingClick], bool],
    ) -> tuple[SnapPublication, ...]:
        ordered = set(self._click_order)
        for request_id, pending in tuple(self._pending_clicks.items()):
            if not predicate(pending):
                continue
            if request_id in ordered:
                self._click_completions[request_id] = (None, None)
            else:
                self._pending_clicks.pop(request_id, None)
        return self._drain_clicks()

    def _new_request(
        self,
        point: Point2D,
        radius: float,
        *,
        purpose: str,
    ) -> SnapRequest:
        config = self._config
        if config is None:
            raise RuntimeError("Snap request requires an active KLayout configuration.")
        self._request_id += 1
        return SnapRequest(
            request_id=self._request_id,
            config=config,
            point=point,
            radius=max(0.0, float(radius)),
            purpose=purpose,
        )

    def _ensure_worker(self) -> tuple[AttachWorker, ...]:
        if self._worker_token or self._source_key is None or self._closed:
            return ()
        self._worker_counter += 1
        self._worker_token = self._worker_counter
        return (AttachWorker(self._worker_token, self._source_key),)

    def _best_markup(self, raw_point: Point2D, radius: float) -> SnapResult | None:
        available = tuple(
            result
            for result in self._markup_candidates
            if _euclidean_distance(raw_point, result.point) <= max(0.0, float(radius))
        )
        if not available:
            return None
        result = min(
            available,
            key=lambda result: _euclidean_distance(raw_point, result.point),
        )
        return replace(
            result,
            distance=_euclidean_distance(raw_point, result.point),
        )

    def _fallback(self, raw_point: Point2D, radius: float) -> SnapResult:
        free = SnapResult(raw_point, "free", 0.0)
        return self._nearest(raw_point, free, self._best_markup(raw_point, radius))

    def _nearest(
        self,
        raw_point: Point2D,
        *results: SnapResult | None,
    ) -> SnapResult:
        available = tuple(result for result in results if result is not None)
        snapped = tuple(result for result in available if result.mode != "free")
        candidates = snapped or available
        if not candidates:
            return SnapResult(raw_point, "free", 0.0)
        return min(
            candidates,
            key=lambda result: self._screen_distance(raw_point, result.point),
        )

    def _accept_generation(self, generation: int) -> bool:
        return bool(
            not self._closed
            and (
                self._interaction_generation is None
                or int(generation) == self._interaction_generation
            )
        )

    def _discard_requests(self) -> None:
        self._latest_hover_request_id = 0
        self._pending_hovers.clear()
        self._pending_clicks.clear()
        self._click_order.clear()
        self._click_completions.clear()

    def _detach(self) -> SnapTransition:
        token = self._worker_token
        self._discard_requests()
        self._config = None
        self._source_key = None
        self._worker_token = 0
        if not token:
            return SnapTransition()
        return SnapTransition(commands=(DetachWorker(token, 0.0),))


def _candidate_result(candidate: object) -> SnapResult | None:
    if isinstance(candidate, SnapResult):
        return candidate
    point = getattr(candidate, "point", None)
    mode = getattr(candidate, "mode", None)
    if not isinstance(point, tuple) or len(point) != 2 or not isinstance(mode, str):
        return None
    return SnapResult(
        point=(float(point[0]), float(point[1])),
        mode=mode,
        distance=0.0,
        segment_start=getattr(candidate, "segment_start", None),
        segment_end=getattr(candidate, "segment_end", None),
    )


def _euclidean_distance(first: Point2D, second: Point2D) -> float:
    return math.hypot(second[0] - first[0], second[1] - first[1])


__all__ = [
    "AttachWorker",
    "CancelHover",
    "CancelPending",
    "ClickPublication",
    "DetachWorker",
    "HoverPublication",
    "ReplaceWorker",
    "SnapCommand",
    "SnapCoordinator",
    "SnapNotice",
    "SnapPublication",
    "SnapTransition",
    "SnapWorkerEvent",
    "SubmitClick",
    "SubmitHover",
    "WorkerFailureEvent",
    "WorkerLifecycleFailedEvent",
    "WorkerResponseEvent",
]
