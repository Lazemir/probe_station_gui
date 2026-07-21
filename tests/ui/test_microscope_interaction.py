from __future__ import annotations

from collections.abc import Callable
import os
import subprocess
import sys
import textwrap
import pytest
from PySide6.QtWidgets import QApplication

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import QPointF, QRect, QSize, Qt, QThread
from PySide6.QtTest import QTest

from probe_station_gui.views import microscope_interaction as interaction_module
from probe_station_gui.views.microscope_interaction import (
    ClickMoveBindings,
    ClickMoveConfig,
    ImageGeometry,
    MicroscopeInteraction,
)


_QT_APP = QApplication.instance() or QApplication([])


def test_busy_stage_queues_once_then_starts_once_without_clearing_target() -> None:
    accepted = False
    requests: list[tuple[float, float]] = []
    marked: list[set[str]] = []
    pending_states: list[bool] = []

    def request_move(dx: float, dy: float) -> bool:
        requests.append((dx, dy))
        return accepted

    interaction = MicroscopeInteraction(
        ClickMoveBindings(
            request_move=request_move,
            stage_connected=lambda: True,
            motion_blocked=lambda: False,
            mark_motion_axes=lambda axes: marked.append(set(axes)),
            show_status=lambda _message, _timeout_ms=0: None,
            repaint=lambda: None,
            preview_hover=lambda _dx, _dy: None,
            present_coordinates=lambda **_coordinates: None,
            manual_alignment_active=lambda: False,
            capture_manual_alignment=lambda _dx, _dy: None,
            pending_state_changed=pending_states.append,
        ),
        ClickMoveConfig(pending_timeout_s=lambda: 2.0),
    )

    interaction.try_start_move(20.0, -5.0, 0.7, 0.4)

    assert interaction.pending_move is not None
    assert interaction.target_rel == (0.7, 0.4)
    assert requests == [(20.0, -5.0)]
    assert marked == []

    accepted = True
    interaction.retry_pending()

    assert requests == [(20.0, -5.0), (20.0, -5.0)]
    assert marked == [{"X", "Y"}]
    assert interaction.pending_move is None
    assert interaction.target_rel == (0.7, 0.4)
    assert pending_states == [True, False]


def _interaction(
    *,
    request_move=lambda _dx, _dy: True,
    timeout=lambda: 2.0,
    preview_hover=lambda _dx, _dy: None,
    present_coordinates=lambda **_coordinates: None,
    manual_alignment_active=lambda: False,
    capture_manual_alignment=lambda _dx, _dy: None,
    pending_state_changed=lambda _pending: None,
    retry_scheduler=None,
) -> MicroscopeInteraction:
    return MicroscopeInteraction(
        ClickMoveBindings(
            request_move=request_move,
            stage_connected=lambda: True,
            motion_blocked=lambda: False,
            mark_motion_axes=lambda _axes: None,
            show_status=lambda _message, _timeout_ms=0: None,
            repaint=lambda: None,
            preview_hover=preview_hover,
            present_coordinates=present_coordinates,
            manual_alignment_active=manual_alignment_active,
            capture_manual_alignment=capture_manual_alignment,
            pending_state_changed=pending_state_changed,
        ),
        ClickMoveConfig(pending_timeout_s=timeout),
        retry_scheduler=retry_scheduler,
    )


def _geometry() -> ImageGeometry:
    return ImageGeometry(QRect(10, 20, 200, 160), QSize(100, 80))


def test_pointer_mapping_is_y_up_and_click_dispatches_only_on_release() -> None:
    interaction = _interaction()

    pressed = interaction.press(QPointF(150.0, 60.0), _geometry())
    released = interaction.release(QPointF(170.0, 140.0), _geometry())

    assert pressed.click is None
    assert released.click == (30.0, -20.0, 0.8, 0.75)


def test_drag_outside_cancels_release_and_target() -> None:
    interaction = _interaction()
    interaction.press(QPointF(150.0, 100.0), _geometry())

    moved = interaction.move(
        QPointF(5.0, 100.0),
        _geometry(),
        left_button=True,
        modifiers=Qt.NoModifier,
    )
    released = interaction.release(QPointF(150.0, 100.0), _geometry())

    assert moved.hover_left is True
    assert released.click is None
    assert interaction.target_rel is None


def test_measurement_and_alignment_modes_suppress_click_move() -> None:
    interaction = _interaction()
    interaction.set_measure_mode("ruler")

    first = interaction.press(QPointF(50.0, 60.0), _geometry())
    hover = interaction.move(
        QPointF(170.0, 140.0),
        _geometry(),
        left_button=False,
        modifiers=Qt.ControlModifier,
    )

    assert first.click is None
    assert hover.hover is None
    assert interaction.measurement_hover == (30.0, 20.0)
    assert interaction.pending_move is None

    interaction.set_measure_mode(None)
    interaction.set_alignment_mode(True)
    aligned = interaction.press(QPointF(150.0, 100.0), _geometry())

    assert aligned.click == (20.0, 0.0, 0.7, 0.5)
    assert interaction.pending_move is None


def test_hover_is_cached_and_cleared_on_leave() -> None:
    presentations: list[dict[str, object]] = []
    interaction = _interaction(
        preview_hover=lambda _dx, _dy: ((1.0, 2.0), (3.0, 4.0)),
        present_coordinates=lambda **coordinates: presentations.append(coordinates),
    )

    moved = interaction.move(
        QPointF(150.0, 100.0),
        _geometry(),
        left_button=False,
        modifiers=Qt.NoModifier,
    )

    assert moved.hover == (20.0, 0.0, 0.7, 0.5)
    assert interaction.hover == moved.hover
    assert presentations == [{"center_xy": (1.0, 2.0), "cursor_xy": (3.0, 4.0)}]

    left = interaction.leave()

    assert left.hover_left is True
    assert interaction.hover is None
    assert presentations[-1] == {"center_xy": None, "cursor_xy": None}


def test_manual_alignment_intercepts_click_before_stage_request() -> None:
    requests: list[tuple[float, float]] = []
    captures: list[tuple[float, float]] = []
    interaction = _interaction(
        request_move=lambda dx, dy: requests.append((dx, dy)) or True,
        manual_alignment_active=lambda: True,
        capture_manual_alignment=lambda dx, dy: captures.append((dx, dy)),
    )

    interaction.handle_click(4.0, -3.0, 0.6, 0.4)

    assert captures == [(4.0, -3.0)]
    assert requests == []
    assert interaction.pending_move is None


def test_pending_state_callback_reports_only_boolean_transitions() -> None:
    pending_states: list[bool] = []
    interaction = _interaction(
        request_move=lambda _dx, _dy: False,
        pending_state_changed=pending_states.append,
    )

    interaction.try_start_move(1.0, 2.0, 0.1, 0.2)
    interaction.try_start_move(3.0, 4.0, 0.3, 0.4)
    interaction.cancel_pending(clear_target=True)
    interaction.cancel_pending(clear_target=True)
    interaction.try_start_move(5.0, 6.0, 0.5, 0.6)
    interaction.shutdown()

    assert pending_states == [True, False, True, False]


def test_timeout_uses_configured_deadline_and_clears_target(monkeypatch) -> None:
    now = [10.0]
    statuses: list[tuple[str, int]] = []
    monkeypatch.setattr(interaction_module, "monotonic", lambda: now[0])
    interaction = MicroscopeInteraction(
        ClickMoveBindings(
            request_move=lambda _dx, _dy: False,
            stage_connected=lambda: True,
            motion_blocked=lambda: False,
            mark_motion_axes=lambda _axes: None,
            show_status=lambda message, timeout_ms=0: statuses.append(
                (message, timeout_ms)
            ),
            repaint=lambda: None,
            preview_hover=lambda _dx, _dy: None,
            present_coordinates=lambda **_coordinates: None,
            manual_alignment_active=lambda: False,
            capture_manual_alignment=lambda _dx, _dy: None,
            pending_state_changed=lambda _pending: None,
        ),
        ClickMoveConfig(pending_timeout_s=lambda: 0.5),
    )
    interaction.try_start_move(1.0, 2.0, 0.25, 0.75)

    now[0] = 10.5
    interaction.retry_pending()

    assert interaction.pending_move is None
    assert interaction.target_rel is None
    assert statuses[-1] == (
        "Click-to-move timed out waiting for the stage.",
        5000,
    )


def test_latest_click_replaces_pending_and_retry_starts_only_latest() -> None:
    accepted = False
    requests: list[tuple[float, float]] = []

    def request_move(dx: float, dy: float) -> bool:
        requests.append((dx, dy))
        return accepted

    interaction = _interaction(request_move=request_move)
    interaction.try_start_move(1.0, 2.0, 0.1, 0.2)
    interaction.try_start_move(3.0, 4.0, 0.3, 0.4)

    accepted = True
    interaction.retry_pending()

    assert requests == [(1.0, 2.0), (3.0, 4.0), (3.0, 4.0)]
    assert interaction.pending_move is None
    assert interaction.target_rel == (0.3, 0.4)


def test_scheduled_retry_captures_generation_and_stale_callback_is_noop() -> None:
    accepted = False
    requests: list[tuple[float, float]] = []
    callbacks: list[Callable[[], None]] = []

    def request_move(dx: float, dy: float) -> bool:
        requests.append((dx, dy))
        return accepted

    interaction = _interaction(
        request_move=request_move,
        retry_scheduler=lambda _delay_ms, callback: callbacks.append(callback),
    )
    interaction.try_start_move(1.0, 2.0, 0.1, 0.2)
    interaction.try_start_move(3.0, 4.0, 0.3, 0.4)

    accepted = True
    callbacks[0]()
    assert requests == [(1.0, 2.0), (3.0, 4.0)]
    assert interaction.pending_move is not None

    callbacks[1]()
    assert requests == [(1.0, 2.0), (3.0, 4.0), (3.0, 4.0)]
    assert interaction.pending_move is None


def test_default_qt_retry_waits_for_interval_and_runs_on_gui_thread() -> None:
    accepted = False
    requests: list[tuple[float, float]] = []
    request_threads: list[QThread] = []

    def request_move(dx: float, dy: float) -> bool:
        requests.append((dx, dy))
        request_threads.append(QThread.currentThread())
        return accepted

    interaction = _interaction(request_move=request_move)
    interaction.try_start_move(1.0, 2.0, 0.1, 0.2)
    accepted = True

    QTest.qWait(100)
    assert requests == [(1.0, 2.0)]
    assert interaction.pending_move is not None

    QTest.qWait(100)
    assert requests == [(1.0, 2.0), (1.0, 2.0)]
    assert interaction.pending_move is None
    assert request_threads == [_QT_APP.thread(), _QT_APP.thread()]


def test_public_interaction_state_is_read_only() -> None:
    assert isinstance(MicroscopeInteraction.pending_move, property)
    assert MicroscopeInteraction.pending_move.fset is None
    assert isinstance(MicroscopeInteraction.target_rel, property)
    assert MicroscopeInteraction.target_rel.fset is None
    assert isinstance(MicroscopeInteraction.hover, property)
    assert MicroscopeInteraction.hover.fset is None


def test_active_retry_is_safe_when_real_qt_view_is_destroyed_in_subprocess() -> None:
    code = textwrap.dedent(
        """
        import os
        os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
        from PySide6.QtCore import QTimer
        from PySide6.QtWidgets import QApplication
        from probe_station_gui.views.microscope_interaction import ClickMoveBindings, ClickMoveConfig
        from probe_station_gui.views.microscope_view import MicroscopeView

        app = QApplication.instance() or QApplication([])
        destroyed = []
        view = MicroscopeView(
            click_move_bindings=ClickMoveBindings(
                request_move=lambda _dx, _dy: False,
                stage_connected=lambda: True,
                motion_blocked=lambda: False,
                mark_motion_axes=lambda _axes: None,
                show_status=lambda _message, _timeout_ms=0: None,
                repaint=lambda: None,
                preview_hover=lambda _dx, _dy: None,
                present_coordinates=lambda **_coordinates: None,
                manual_alignment_active=lambda: False,
                capture_manual_alignment=lambda _dx, _dy: None,
                pending_state_changed=lambda _pending: None,
            ),
            click_move_config=ClickMoveConfig(
                pending_timeout_s=lambda: 2.0,
                pending_retry_ms=20,
            ),
        )
        view.destroyed.connect(lambda: destroyed.append(True))
        view.interaction.try_start_move(1.0, 2.0, 0.2, 0.3)
        view.deleteLater()
        QTimer.singleShot(100, app.quit)
        app.exec()
        assert destroyed == [True]
        print("destroyed-cleanly")
        """
    )
    env = dict(os.environ)
    env["QT_QPA_PLATFORM"] = "offscreen"

    result = subprocess.run(
        [sys.executable, "-c", code],
        capture_output=True,
        text=True,
        timeout=10,
        env=env,
    )

    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "destroyed-cleanly"


def test_api_start_never_queues_or_creates_target() -> None:
    interaction = _interaction(request_move=lambda _dx, _dy: False)

    accepted = interaction.try_start_api_move(5.0, -6.0)

    assert accepted is False
    assert interaction.pending_move is None
    assert interaction.target_rel is None


def test_target_motion_duration_matches_distance_and_feedrate() -> None:
    interaction = _interaction()
    interaction.try_start_move(3.0, 4.0, 0.9, 0.1)

    interaction.start_target_motion(3.0, 4.0, 60.0)

    assert interaction.target_motion_duration_s == pytest.approx(5.03)
    interaction.shutdown()


def test_finish_move_preserves_target_while_a_new_click_is_pending() -> None:
    interaction = _interaction(request_move=lambda _dx, _dy: False)
    interaction.try_start_move(1.0, 2.0, 0.2, 0.3)

    interaction.finish_move(success=True)

    assert interaction.pending_move is not None
    assert interaction.target_rel == (0.2, 0.3)
    interaction.shutdown()
