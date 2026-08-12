from __future__ import annotations

import ast
from importlib import import_module
from pathlib import Path

from probe_station_measure import Keithley2400With2182AConfig
from tests.instruments.test_probe_station_measure_keithley import (
    _FakeHandle,
    _FakeResourceManager,
)


def test_session_owner_executes_software_measurement_and_closes_handles() -> None:
    from probe_station_measure.instrument_drivers.Keithley.Keithley_2400_2182A_session import (
        KeithleySession,
    )

    source = _FakeHandle(
        {
            "*IDN?": ["KEITHLEY INSTRUMENTS INC.,MODEL 2400"],
            "FETC?": ["-0.03,-0.001", "0.03,0.001"],
        }
    )
    voltmeter = _FakeHandle(
        {
            "*IDN?": ["KEITHLEY INSTRUMENTS INC.,MODEL 2182A"],
            "READ?": ["-0.029", "0.031"],
        }
    )
    session = KeithleySession(
        "GPIB0::1::INSTR",
        "GPIB0::2::INSTR",
        timeout_ms=10_000,
        resource_manager=_FakeResourceManager(source, voltmeter),
    )

    assert session.identify() == (
        "2400 KEITHLEY INSTRUMENTS INC.,MODEL 2400; "
        "2182A KEITHLEY INSTRUMENTS INC.,MODEL 2182A"
    )
    readings = session.measure_software(
        [-0.03, 0.03],
        Keithley2400With2182AConfig(
            use_buffer=False,
            use_trigger_link=False,
            compliance_current_a=0.5,
            current_range_a=0.5,
        ),
        lambda **values: (
            values["source_voltage_v"],
            values["measured_voltage_v"],
            values["current_a"],
        ),
    )

    assert readings == [
        (-0.03, -0.029, -0.001),
        (0.03, 0.031, 0.001),
    ]
    assert source.writes[-2:] == [":SOUR:VOLT 0", ":OUTP ON"]

    session.close()

    assert source.closed
    assert voltmeter.closed


def test_trigger_link_owner_executes_buffered_pair_and_callback() -> None:
    from probe_station_measure.instrument_drivers.Keithley.Keithley_2400_2182A_session import (
        KeithleySession,
    )
    from probe_station_measure.instrument_drivers.Keithley.Keithley_2400_2182A_trigger_link import (
        TriggerLinkBatch,
    )

    source = _FakeHandle(
        {
            "*OPC?": ["1"],
            "TRAC:DATA?": ["-0.03,-0.001,0.03,0.001"],
        }
    )
    voltmeter = _FakeHandle({"TRAC:DATA?": ["-0.029,0.031"]})
    session = KeithleySession(
        "GPIB0::1::INSTR",
        "GPIB0::2::INSTR",
        timeout_ms=10_000,
        resource_manager=_FakeResourceManager(source, voltmeter),
    )
    batch = TriggerLinkBatch(
        session,
        max_source_list_points=2500,
        max_source_list_points_per_command=100,
        max_trace_points=1024,
        default_timeout_ms=10_000,
        max_timeout_ms=600_000,
        base_timeout_s=10.0,
        point_overhead_s=0.08,
        nplc_point_s=0.05,
    )
    callbacks: list[bool] = []

    readings = batch.measure(
        [-0.03, 0.03],
        Keithley2400With2182AConfig(
            use_buffer=True,
            use_trigger_link=True,
            compliance_current_a=0.5,
            current_range_a=0.5,
        ),
        lambda **values: (
            values["source_voltage_v"],
            values["measured_voltage_v"],
            values["current_a"],
        ),
        after_measurement=lambda: callbacks.append(True),
    )

    assert readings == [
        (-0.03, -0.029, -0.001),
        (0.03, 0.031, 0.001),
    ]
    assert callbacks == [True]
    assert ":TRIG:SOUR TLIN" in source.writes
    assert "TRIG:SOUR EXT" in voltmeter.writes
    cleanup_start = source.writes.index(":SOUR:VOLT:MODE FIX", -6)
    assert source.writes[cleanup_start : cleanup_start + 3] == [
        ":SOUR:VOLT:MODE FIX",
        ":SOUR:VOLT 0",
        ":OUTP ON",
    ]


def test_public_identities_remain_canonical_in_original_module() -> None:
    root = import_module("probe_station_measure")
    package = import_module("probe_station_measure.instrument_drivers.Keithley")
    original = import_module(
        "probe_station_measure.instrument_drivers.Keithley.Keithley_2400_2182A"
    )
    root_names = (
        "ContactQuality",
        "ContactQualityCheck",
        "ContactQualityCriteria",
        "DifferentialReading",
        "Keithley2400SourceMeter",
        "Keithley2400With2182A",
        "Keithley2400With2182AConfig",
        "PolarityReading",
        "TraceBufferStatus",
        "VoltageListReading",
        "evaluate_contact_quality",
        "summarize_contact_quality",
    )

    for name in root_names:
        canonical = getattr(original, name)
        assert getattr(root, name) is canonical
        assert canonical.__module__ == original.__name__
    for name in (
        "Keithley2400With2182A",
        "Keithley2400With2182AConfig",
        "VoltageListReading",
    ):
        assert getattr(package, name) is getattr(original, name)


def test_owner_deletion_gate_keeps_moved_scpi_policy_canonical_once() -> None:
    root = Path(__file__).parents[2]
    driver_dir = root / "probe_station_measure" / "instrument_drivers" / "Keithley"
    paths = {
        "original": driver_dir / "Keithley_2400_2182A.py",
        "session": driver_dir / "Keithley_2400_2182A_session.py",
        "trigger": driver_dir / "Keithley_2400_2182A_trigger_link.py",
    }
    trees = {
        name: ast.parse(path.read_text(encoding="utf-8"))
        for name, path in paths.items()
    }
    original_driver = next(
        node
        for node in trees["original"].body
        if isinstance(node, ast.ClassDef) and node.name == "Keithley2400With2182A"
    )
    original_methods = {
        node.name for node in original_driver.body if isinstance(node, ast.FunctionDef)
    }
    moved_methods = {
        "_open_resource",
        "_configure_common",
        "_measure_voltage_list_software",
        "_measure_voltage_list_buffered_trigger_link",
        "_prepare_voltage_list_buffered_trigger_link",
        "_prepared_voltage_list_matches",
        "_source_list_covers",
        "_read_trace_buffers_parallel",
        "_write_source_voltage_list",
        "_source_output_context_enabled",
        "_prime_output_context_source_voltage",
        "_set_source_output_enabled",
        "_require_source",
        "_require_voltmeter",
        "_write",
        "_query",
        "_try_write",
        "_try_clear",
        "_safe_query",
        "_trace_status",
        "_raise_scpi_errors",
        "_read_scpi_errors",
    }

    assert original_methods.isdisjoint(moved_methods)
    expected_owner = {
        ":ARM:SOUR IMM": "session",
        "SENS:VOLT:DFIL:STAT OFF": "session",
        "TRAC:FREE?": "session",
        ":TRIG:SOUR TLIN": "trigger",
        ":SOUR:LIST:VOLT:APPend": "trigger",
    }
    for value, owner in expected_owner.items():
        occurrences = {
            name: sum(
                isinstance(node, ast.Constant) and node.value == value
                for node in ast.walk(tree)
            )
            for name, tree in trees.items()
        }
        assert occurrences[owner] == 1
        assert sum(occurrences.values()) == 1

    session_imports = {
        node.module
        for node in ast.walk(trees["session"])
        if isinstance(node, ast.ImportFrom)
    }
    trigger_imports = {
        node.module
        for node in ast.walk(trees["trigger"])
        if isinstance(node, ast.ImportFrom)
    }
    assert "Keithley_2400_2182A" not in session_imports
    assert "Keithley_2400_2182A" not in trigger_imports
    assert "Keithley_2400_2182A_session" in trigger_imports
