from __future__ import annotations

import importlib
import inspect
import os
import subprocess
import sys


def test_dialog_delegates_route_run_state_to_focused_owner() -> None:
    run_controls_module = importlib.import_module(
        "probe_station_gui.dialogs.route_measurement_run_controls"
    )
    dialog_module = importlib.import_module(
        "probe_station_gui.dialogs.route_measurement_dialog"
    )

    run_controls_source = inspect.getsource(
        run_controls_module.RouteMeasurementRunControls
    )
    dialog_source = inspect.getsource(dialog_module.RouteMeasurementDialog)

    assert run_controls_source.count("RouteRunControlState(") == 1
    assert "RouteMeasurementRunControls(" in dialog_source
    assert "RouteRunControlState" not in dialog_source
    assert "_route_run_control_state" not in dialog_source
    assert "_emit_pause_requested" not in dialog_source
    assert "_interrupt_button" not in dialog_source


def test_dialog_delegates_meter_configuration_to_focused_owner() -> None:
    meter_module = importlib.import_module(
        "probe_station_gui.dialogs.route_measurement_meter"
    )
    dialog_module = importlib.import_module(
        "probe_station_gui.dialogs.route_measurement_dialog"
    )
    setup_module = importlib.import_module(
        "probe_station_gui.dialogs.route_measurement_setup"
    )

    meter_source = inspect.getsource(meter_module.RouteMeasurementMeterEditor)
    dialog_source = inspect.getsource(dialog_module.RouteMeasurementDialog)
    setup_source = inspect.getsource(setup_module.RouteMeasurementSetupEditor)

    assert "RouteMeasurementMeterEditor(" in setup_source
    assert "def configuration(" in meter_source
    assert "_build_gwinstek_page" not in dialog_source
    assert "_build_keithley_page" not in dialog_source
    assert "_meter_configuration" not in dialog_source


def test_dialog_delegates_measurement_setup_to_focused_owner() -> None:
    setup_module = importlib.import_module(
        "probe_station_gui.dialogs.route_measurement_setup"
    )
    dialog_module = importlib.import_module(
        "probe_station_gui.dialogs.route_measurement_dialog"
    )

    setup_source = inspect.getsource(setup_module.RouteMeasurementSetupEditor)
    dialog_source = inspect.getsource(dialog_module.RouteMeasurementDialog)

    assert "RouteMeasurementSetupEditor(" in dialog_source
    assert "def configuration(" in setup_source
    assert 'QGroupBox("Measurement"' not in dialog_source
    assert "_choose_csv_path" not in dialog_source
    assert "_update_operation_state" not in dialog_source
    assert "_emit_measure_requested" not in dialog_source


def test_dialog_composes_explicit_profile_controller_without_mixin() -> None:
    profile_module = importlib.import_module(
        "probe_station_gui.dialogs.route_measurement_profile"
    )
    dialog_module = importlib.import_module(
        "probe_station_gui.dialogs.route_measurement_dialog"
    )

    controller_source = inspect.getsource(
        profile_module.RouteMeasurementProfileController
    )
    dialog_source = inspect.getsource(dialog_module.RouteMeasurementDialog)

    assert "RouteMeasurementProfileController(" in dialog_source
    assert "def data(" in controller_source
    assert "def apply_data(" in controller_source
    assert "RouteMeasurementProfileMixin" not in dialog_source
    assert not hasattr(profile_module, "RouteMeasurementProfileMixin")


def test_dialog_delegates_runtime_and_results_to_focused_view() -> None:
    runtime_module = importlib.import_module(
        "probe_station_gui.dialogs.route_measurement_runtime"
    )
    dialog_module = importlib.import_module(
        "probe_station_gui.dialogs.route_measurement_dialog"
    )

    runtime_source = inspect.getsource(runtime_module.RouteMeasurementRuntimeView)
    dialog_source = inspect.getsource(dialog_module.RouteMeasurementDialog)

    assert "RouteMeasurementRuntimeView(" in dialog_source
    assert "def set_progress(" in runtime_source
    assert "def set_result(" in runtime_source
    assert "_build_runtime_view" not in dialog_source
    assert "_progress_eta_text" not in dialog_source
    assert "_show_raw_data" not in dialog_source


def test_dialog_import_does_not_load_route_runner_or_lcr_implementation() -> None:
    result = subprocess.run(
        [
            sys.executable,
            "-c",
            (
                "import sys; "
                "import probe_station_gui.dialogs.route_measurement_dialog; "
                "assert 'probe_station_gui.route.measurement' not in sys.modules; "
                "assert 'probe_station_gui.instruments.meters.lcr' not in sys.modules"
            ),
        ],
        capture_output=True,
        text=True,
        env={**os.environ, "QT_QPA_PLATFORM": "offscreen"},
        check=False,
    )

    assert result.returncode == 0, result.stderr


def test_route_dialog_preserves_exact_published_contract() -> None:
    dialog_module = importlib.import_module(
        "probe_station_gui.dialogs.route_measurement_dialog"
    )
    dialog_type = dialog_module.RouteMeasurementDialog

    signal_names = {
        name
        for name, value in vars(dialog_type).items()
        if type(value).__name__ == "Signal"
    }
    assert signal_names == {
        "measure_requested",
        "start_session_requested",
        "cancel_session_requested",
        "next_requested",
        "remeasure_requested",
        "measure_current_requested",
        "skip_requested",
        "save_shift_requested",
        "interrupt_requested",
        "pause_requested",
        "stop_requested",
        "jump_requested",
        "move_requested",
        "current_point_changed",
    }
    assert str(inspect.signature(dialog_type.__init__)) == (
        "(self, *, route_name: 'str', route_point_count: 'int', "
        "default_csv_path: 'str', default_photo_dir: 'str | None' = None, "
        "default_meter_type: 'str' = 'keithley_2400_2182a', "
        "settings_path: 'str | Path | None' = None, "
        "parent: 'QWidget | None' = None) -> 'None'"
    )
    public_functions = {
        name
        for name, value in vars(dialog_type).items()
        if inspect.isfunction(value) and not name.startswith("_")
    }
    assert public_functions == {
        "closeEvent",
        "set_status",
        "reset_progress",
        "set_progress",
        "finish_progress",
        "set_result",
        "set_current_point",
        "set_measurement_session_active",
        "set_route",
        "set_running",
        "set_waiting",
        "set_pause_request_pending",
        "set_interrupt_request_pending",
        "current_configuration",
    }


def test_route_dialog_child_dag_has_no_reverse_or_implicit_dependencies() -> None:
    child_names = (
        "route_measurement_meter",
        "route_measurement_profile",
        "route_measurement_run_controls",
        "route_measurement_runtime",
        "route_measurement_setup",
    )
    sources = {
        name: inspect.getsource(
            importlib.import_module(f"probe_station_gui.dialogs.{name}")
        )
        for name in child_names
    }
    forbidden = (
        "route_measurement_dialog",
        "route.dialog_adapter",
        "route.runtime_presenter",
        "route.gui_measurement_adapter",
        "route.measurement import",
        "from main import",
        "import main",
        "getattr(",
    )

    for source in sources.values():
        assert all(token not in source for token in forbidden)
    assert (
        sum(source.count("RouteRunControlState(") for source in sources.values()) == 1
    )
    assert "RouteMeasurementProfileMixin" not in "".join(sources.values())


def test_main_presenter_and_adapter_keep_dialog_import_lazy() -> None:
    for module_name in (
        "main",
        "probe_station_gui.route.runtime_presenter",
        "probe_station_gui.route.dialog_adapter",
    ):
        result = subprocess.run(
            [
                sys.executable,
                "-c",
                (
                    f"import {module_name}; import sys; "
                    "assert 'probe_station_gui.dialogs.route_measurement_dialog' "
                    "not in sys.modules"
                ),
            ],
            capture_output=True,
            text=True,
            env={**os.environ, "QT_QPA_PLATFORM": "offscreen"},
            check=False,
        )

        assert result.returncode == 0, (module_name, result.stderr)
