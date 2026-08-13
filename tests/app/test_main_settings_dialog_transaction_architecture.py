from __future__ import annotations

import ast
import inspect
from pathlib import Path
from types import SimpleNamespace

from main import Main
from probe_station_gui.settings.dialog_transaction import (
    SettingsDialogNotice,
    SettingsDialogOutcome,
    SettingsDialogTransaction,
)
from probe_station_gui.settings.manager import Settings


ROOT = Path(__file__).resolve().parents[2]
MAIN_PATH = ROOT / "main.py"
OWNER_PATH = ROOT / "probe_station_gui" / "settings" / "dialog_transaction.py"


def test_transaction_has_one_public_operation_and_no_qt_or_main_dependency() -> None:
    public = {
        name
        for name, value in SettingsDialogTransaction.__dict__.items()
        if callable(value) and not name.startswith("_")
    }
    assert public == {"apply"}
    imports = {
        alias.name
        for node in ast.walk(ast.parse(OWNER_PATH.read_text(encoding="utf-8")))
        if isinstance(node, ast.Import)
        for alias in node.names
    } | {
        node.module or ""
        for node in ast.walk(ast.parse(OWNER_PATH.read_text(encoding="utf-8")))
        if isinstance(node, ast.ImportFrom)
    }
    assert not any(
        name == "main" or name.startswith("PySide6") or ".views" in name
        for name in imports
    )


def test_main_composes_owner_once_and_keeps_only_thin_qt_adapter() -> None:
    module = ast.parse(MAIN_PATH.read_text(encoding="utf-8"))
    constructions = [
        node
        for node in ast.walk(module)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Name)
        and node.func.id == "SettingsDialogTransaction"
    ]
    assert len(constructions) == 1
    source = inspect.getsource(Main._apply_settings_from_dialog)
    assert "_settings_dialog_transaction.apply" in source
    for forbidden in (
        "custom_frames_changed",
        "pivot_changed",
        "active_objective_update_rejected",
        "objective_authority_changed",
        "CustomSystemsRequest",
        "rotation_geometry_snapshot",
        "design_calibration_fingerprints",
        "replace_and_save",
    ):
        assert forbidden not in source
    assert "_reconcile_design_calibration_fingerprints" not in Main.__dict__


def test_main_adapter_applies_outcome_in_presentation_order(
    monkeypatch,
) -> None:
    events: list[object] = []
    monkeypatch.setattr(
        "main.stage_position_panel_adapter.refresh_coordinate_frame_display",
        lambda owner: events.append(("refresh", owner)),
    )
    monkeypatch.setattr(
        "main.coordinate_flow.observe_coordinate_authority",
        lambda owner: events.append(("authority", owner)),
    )

    class _Transaction:
        def apply(self, submitted: object, context: object) -> object:
            events.append(("transaction", submitted, context))
            return SettingsDialogOutcome(
                accepted=True,
                apply_objective_runtime=False,
                refresh_coordinate_frame_display=True,
                observe_coordinate_authority=True,
                post_apply_notices=(SettingsDialogNotice("restored", 4000),),
            )

    owner = SimpleNamespace(
        stage_controller=SimpleNamespace(
            is_busy=lambda: False,
            latest_machine_coordinate_snapshot=lambda: "snapshot",
        ),
        _objective_mutation_busy=lambda: True,
        _settings_dialog_transaction=_Transaction(),
        _stage_position_panel=None,
        _stage_axis_display_values={},
        _active_objective_xy_offset=lambda: (0.0, 0.0),
        _apply_settings=lambda **kwargs: events.append(("apply", kwargs)),
        _show_status=lambda message, timeout: events.append(
            ("notice", message, timeout)
        ),
    )

    Main._apply_settings_from_dialog(owner, Settings())

    assert events[0][0] == "transaction"
    assert events[0][2].machine_snapshot == "snapshot"
    assert events[0][2].objective_mutation_busy is True
    assert events[1:] == [
        ("refresh", owner),
        ("apply", {"apply_objective_runtime": False}),
        ("authority", owner),
        ("notice", "restored", 4000),
    ]


def test_main_adapter_ignores_invalid_submission_before_context_queries() -> None:
    events: list[str] = []
    owner = SimpleNamespace(
        stage_controller=SimpleNamespace(
            is_busy=lambda: events.append("busy") or False,
        ),
        _settings_dialog_transaction=SimpleNamespace(
            apply=lambda *_args: events.append("transaction")
        ),
    )

    Main._apply_settings_from_dialog(owner, object())

    assert events == []
