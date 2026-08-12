import ast
import inspect
from importlib.util import find_spec
from pathlib import Path
import textwrap


def test_route_run_coordinator_module_exists() -> None:
    assert find_spec("probe_station_gui.route.run_coordinator") is not None


def test_route_run_coordinator_is_defined_by_its_private_owner() -> None:
    from probe_station_gui.route import run_coordinator

    owner = run_coordinator._RouteRunCoordinator

    assert owner.__module__ == "probe_station_gui.route.run_coordinator"
    assert owner.__name__ == "_RouteRunCoordinator"


def test_route_measurement_runner_directly_composes_run_coordinator() -> None:
    from probe_station_gui.route import run_coordinator
    from probe_station_gui.route.measurement import RouteMeasurementRunner

    assert RouteMeasurementRunner.__bases__ == (run_coordinator._RouteRunCoordinator,)
    assert RouteMeasurementRunner.__module__ == "probe_station_gui.route.measurement"


def test_route_run_types_have_one_canonical_private_owner() -> None:
    from probe_station_gui.route import measurement, run_coordinator

    names = {
        "_RouteMeasurementStopped",
        "_RoutePointFlowResult",
        "_RoutePointLoopDecision",
        "_RouteRunProgress",
    }

    for name in names:
        owned_type = vars(run_coordinator)[name]
        assert owned_type.__module__ == "probe_station_gui.route.run_coordinator"
        assert name not in vars(measurement)


def test_public_run_is_a_direct_one_line_coordinator_call() -> None:
    from probe_station_gui.route.measurement import RouteMeasurementRunner

    method = RouteMeasurementRunner.__dict__["run"]
    node = ast.parse(textwrap.dedent(inspect.getsource(method))).body[0]

    assert method.__module__ == "probe_station_gui.route.measurement"
    assert str(inspect.signature(method)) == "(self) -> 'tuple[bool, str]'"
    assert isinstance(node, ast.FunctionDef)
    assert len(node.body) == 1
    statement = node.body[0]
    assert isinstance(statement, ast.Return)
    assert isinstance(statement.value, ast.Call)
    assert isinstance(statement.value.func, ast.Attribute)
    assert statement.value.func.attr == "_run_route"


def test_route_orchestration_methods_have_one_private_owner() -> None:
    from probe_station_gui.route import run_coordinator
    from probe_station_gui.route.measurement import RouteMeasurementRunner

    expected = {
        "_finish_accepted_point_result",
        "_finish_point_execution",
        "_finish_rejected_point_result",
        "_finish_route_run",
        "_initial_route_point_loop_decision",
        "_interrupted_route_point_loop_decision",
        "_jump_target_index",
        "_measure_manual_contact_and_advance",
        "_open_route_meter_if_needed",
        "_prepare_saved_route_point_wait",
        "_route_completion_message",
        "_route_point_auto_next",
        "_route_point_confirmation_loop_decision",
        "_route_start_index",
        "_run_route",
        "_run_route_point",
        "_saved_route_point_confirmation_result",
        "_start_route_run",
        "_validate_route_run_configuration",
        "_wait_after_interrupted_point",
        "_wait_after_rejected_result",
        "_wait_before_first_route_point",
        "_wait_for_valid_confirmation",
    }
    owned = {
        name
        for name, value in vars(run_coordinator._RouteRunCoordinator).items()
        if inspect.isfunction(value)
    }

    assert owned == expected
    assert expected.isdisjoint(vars(RouteMeasurementRunner))


def test_route_run_coordinator_has_one_way_neutral_imports() -> None:
    from probe_station_gui.route import run_coordinator

    source = Path(run_coordinator.__file__).read_text(encoding="utf-8-sig")
    tree = ast.parse(source)
    imported_modules = {
        node.module
        for node in ast.walk(tree)
        if isinstance(node, ast.ImportFrom) and node.module is not None
    }
    imported_names = {
        alias.name
        for node in ast.walk(tree)
        if isinstance(node, ast.Import)
        for alias in node.names
    }
    route_imports = {
        alias.name
        for node in ast.walk(tree)
        if isinstance(node, ast.ImportFrom) and node.module == "probe_station_gui.route"
        for alias in node.names
    }

    assert imported_modules == {
        "__future__",
        "dataclasses",
        "probe_station_gui.route",
        "probe_station_gui.route.formatting",
        "probe_station_gui.route.measurement_records",
        "probe_station_gui.route.point_execution",
    }
    assert route_imports == {"measurement_recording"}
    assert imported_names == {"math", "time"}
    forbidden = {
        "main",
        "probe_station_gui.route.external_session",
        "probe_station_gui.route.measurement",
        "probe_station_gui.serial",
        "probe_station_gui.stage",
        "PySide6",
    }
    assert all(
        not any(
            module == blocked or module.startswith(f"{blocked}.")
            for blocked in forbidden
        )
        for module in imported_modules | imported_names
    )
