from __future__ import annotations

import ast
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]


def _main_tree() -> ast.Module:
    return ast.parse((ROOT / "main.py").read_text(encoding="utf-8-sig"))


def _imported_modules(path: Path) -> set[str]:
    tree = ast.parse(path.read_text(encoding="utf-8-sig"))
    imported: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            imported.add(node.module)
    return imported


def test_main_has_no_telegram_policy_lifecycle_or_route_state_facades() -> None:
    tree = _main_tree()
    methods = {
        node.name
        for node in ast.walk(tree)
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
    }
    self_attributes = {
        node.attr
        for node in ast.walk(tree)
        if isinstance(node, ast.Attribute)
        and isinstance(node.value, ast.Name)
        and node.value.id == "self"
    }
    forbidden_methods = {
        "_configure_telegram_bot_from_settings",
        "_stop_telegram_bot_service",
        "_submit_telegram_bot_request",
        "_on_telegram_bot_request_received",
        "_handle_telegram_bot_request",
        "_handle_telegram_callback",
        "_telegram_response_for_command_route",
        "_telegram_status_response",
        "_telegram_request_next_route_photo_response",
        "_telegram_request_next_contact_photo_response",
        "_telegram_route_action_response",
        "_telegram_text_response",
        "_telegram_default_markup",
        "_telegram_route_actions_markup",
        "_route_telegram_adapter",
        "_telegram_status_text",
        "_send_telegram_alert",
        "_send_telegram_bot_message",
        "_take_pending_telegram_contact_photos",
        "_latest_route_contact_failure_telegram_photos",
        "_telegram_contact_photo_payload",
        "_combine_telegram_contact_photos",
    }

    assert not forbidden_methods.intersection(methods)
    assert not {
        "_telegram_bot_service",
        "_telegram_bot_signature",
        "_route_telegram",
    }.intersection(self_attributes)


def test_main_signal_and_shutdown_connect_directly_to_runtime() -> None:
    source = (ROOT / "main.py").read_text(encoding="utf-8-sig")
    tree = ast.parse(source)
    shutdown = (ROOT / "probe_station_gui/views/main_window_shutdown.py").read_text(
        encoding="utf-8"
    )

    assert "telegram_bot_request_received: Signal = Signal(object)" in source
    assert any(
        isinstance(node, ast.Call)
        and ast.unparse(node.func) == "self.telegram_bot_request_received.connect"
        and [ast.unparse(argument) for argument in node.args]
        == ["self._telegram_runtime.handle_on_gui"]
        for node in ast.walk(tree)
    )
    assert "owner._telegram_runtime.stop()" in shutdown
    assert "_stop_telegram_bot_service" not in shutdown


def test_route_callers_use_the_one_runtime_photo_state_directly() -> None:
    paths = [ROOT / "main.py", *(ROOT / "probe_station_gui/application").glob("*.py")]
    sources = [path.read_text(encoding="utf-8-sig") for path in paths]
    combined_source = "\n".join(sources)

    assert "_route_telegram_adapter" not in combined_source
    assert "_route_telegram" not in combined_source
    assert "self._telegram_runtime.route_photos" in combined_source


def test_runtime_public_surface_and_owner_dag_are_exact() -> None:
    runtime_path = ROOT / "probe_station_gui/notifications/telegram_runtime.py"
    runtime_tree = ast.parse(runtime_path.read_text(encoding="utf-8"))
    runtime_class = next(
        node
        for node in runtime_tree.body
        if isinstance(node, ast.ClassDef) and node.name == "TelegramCommandRuntime"
    )
    public_methods = {
        node.name
        for node in runtime_class.body
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
        and not node.name.startswith("_")
    }

    assert [ast.unparse(base) for base in runtime_class.bases] == ["QObject"]
    assert public_methods == {
        "configure",
        "default_markup",
        "handle_on_gui",
        "route_actions_markup",
        "send_alert",
        "send_bot_message",
        "stop",
        "submit_from_worker",
    }
    assert not any(
        isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
        and node.name == "__getattr__"
        for node in runtime_class.body
    )

    main_imports = _imported_modules(ROOT / "main.py")
    runtime_imports = _imported_modules(runtime_path)
    command_imports = _imported_modules(
        ROOT / "probe_station_gui/notifications/telegram_commands.py"
    )
    route_imports = _imported_modules(
        ROOT / "probe_station_gui/route/telegram_adapter.py"
    )
    notifications_package = (
        ROOT / "probe_station_gui/notifications/__init__.py"
    ).read_text(encoding="utf-8")

    assert "probe_station_gui.notifications.telegram_runtime" in main_imports
    assert not {
        "main",
        "probe_station_gui.api.server",
        "probe_station_gui.settings.manager",
    }.intersection(runtime_imports)
    assert not any(
        module.startswith("probe_station_gui.views") for module in runtime_imports
    )
    assert not any(
        module in {"main", "probe_station_gui.notifications.telegram_runtime"}
        or module.startswith("probe_station_gui.views")
        for module in command_imports | route_imports
    )
    assert "telegram_runtime" not in notifications_package
