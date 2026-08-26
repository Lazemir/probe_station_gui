"""Pure ownership of the next untracked Stage motion completion."""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum, auto


class MotionCompletionKind(Enum):
    ORDINARY = auto()
    CLICK = auto()
    ALIGNMENT = auto()


@dataclass(frozen=True)
class MotionCompletionClaim:
    kind: MotionCompletionKind
    success: bool
    message: str
    correlation: object | None = None


class MotionCompletionState:
    def __init__(self) -> None:
        self._kind: MotionCompletionKind | None = None
        self._correlation: object | None = None

    def arm_click(self) -> None:
        self._kind = MotionCompletionKind.CLICK
        self._correlation = None

    def arm_alignment(self, correlation: object) -> None:
        self._kind = MotionCompletionKind.ALIGNMENT
        self._correlation = correlation

    def clear(self) -> None:
        self._kind = None
        self._correlation = None

    def discard_alignment(self) -> bool:
        if self._kind is not MotionCompletionKind.ALIGNMENT:
            return False
        self.clear()
        return True

    def alignment_pending(self) -> bool:
        return self._kind is MotionCompletionKind.ALIGNMENT

    def claim_untracked(self, success: bool, message: str) -> MotionCompletionClaim:
        kind = self._kind or MotionCompletionKind.ORDINARY
        correlation = self._correlation
        self.clear()
        return MotionCompletionClaim(
            kind=kind,
            success=bool(success),
            message=str(message),
            correlation=correlation,
        )
