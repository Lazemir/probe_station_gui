from __future__ import annotations

import json
import subprocess
import sys


def test_probe_station_measure_package_import_does_not_load_keithley_stack() -> None:
    code = """
import json
import sys

import probe_station_measure
from probe_station_measure import OHMMETER_RANGE_MANUAL

print(json.dumps({
    "range_mode": OHMMETER_RANGE_MANUAL,
    "qcodes": "qcodes" in sys.modules,
    "pyvisa": "pyvisa" in sys.modules,
    "keithley_pair": (
        "probe_station_measure.instrument_drivers.Keithley.Keithley_2400_2182A"
        in sys.modules
    ),
}))
"""
    result = subprocess.run(
        [sys.executable, "-c", code],
        check=True,
        capture_output=True,
        text=True,
    )

    imported = json.loads(result.stdout)
    assert imported == {
        "range_mode": "manual",
        "qcodes": False,
        "pyvisa": False,
        "keithley_pair": False,
    }


def test_probe_station_measure_keithley_export_stays_importable() -> None:
    code = """
from probe_station_measure import Keithley2400With2182A

print(Keithley2400With2182A.__name__)
"""
    result = subprocess.run(
        [sys.executable, "-c", code],
        check=True,
        capture_output=True,
        text=True,
    )

    assert result.stdout.strip() == "Keithley2400With2182A"
