from __future__ import annotations

from types import SimpleNamespace

import pytest


pytest.importorskip("PySide6")

from PySide6.QtWidgets import QApplication

from probe_station_gui.dialogs.route_measurement_result_views import (
    RouteMeasurementHistogram,
    RouteMeasurementRawDataDialog,
)
from probe_station_gui.dialogs.route_measurement_widgets import SIPrefixSpinBox


@pytest.fixture(scope="module")
def qt_app() -> QApplication:
    app = QApplication.instance()
    if app is None:
        app = QApplication([])
    return app


def test_si_prefix_spinbox_stores_base_units(qt_app: QApplication) -> None:
    _ = qt_app
    spinbox = SIPrefixSpinBox(
        prefixes=(("mV", 1e-3), ("V", 1.0)),
        base_minimum=0.0,
        base_maximum=10.0,
        base_value=0.002,
    )

    assert spinbox.base_value() == pytest.approx(0.002)

    spinbox.set_base_value(20.0)

    assert spinbox.base_value() == pytest.approx(10.0)

    spinbox.set_base_value("not numeric")

    assert spinbox.base_value() == pytest.approx(10.0)


def test_histogram_series_follow_mode(qt_app: QApplication) -> None:
    _ = qt_app
    sample = SimpleNamespace(
        negative_resistance_ohm=10.0,
        positive_resistance_ohm=12.0,
        differential_resistance_ohm=2.0,
    )
    histogram = RouteMeasurementHistogram()
    histogram.set_samples((sample,))

    histogram.set_mode("polarity")
    polarity_series = histogram._series()

    assert [label for label, _color, _values in polarity_series] == [
        "negative",
        "positive",
    ]
    assert [values for _label, _color, values in polarity_series] == [[10.0], [12.0]]

    histogram.set_mode("differential")
    differential_series = histogram._series()

    assert [(label, values) for label, _color, values in differential_series] == [
        ("differential", [2.0])
    ]


def test_raw_data_dialog_populates_and_copies_rows(qt_app: QApplication) -> None:
    sample = SimpleNamespace(
        sample_index=3,
        negative_source_voltage_v=-0.03,
        negative_measured_voltage_v=-0.02,
        negative_current_a=-2e-6,
        negative_resistance_ohm=10_000.0,
        positive_source_voltage_v=0.03,
        positive_measured_voltage_v=0.02,
        positive_current_a=2e-6,
        positive_resistance_ohm=10_000.0,
        differential_resistance_ohm=10_000.0,
        compliance_hit=True,
    )
    dialog = RouteMeasurementRawDataDialog((sample,))

    assert dialog._table.rowCount() == 2
    assert dialog._table.columnCount() == len(dialog.HEADERS)
    assert dialog._table.item(0, 0).text() == "3"
    assert dialog._table.item(0, 1).text() == "negative"
    assert dialog._table.item(1, 1).text() == "positive"

    dialog._copy_all()

    clipboard_text = qt_app.clipboard().text()
    assert clipboard_text.startswith("sample\tpolarity\tsource_v")
    assert "\n3\tnegative\t-0.03\t-0.02\t-2e-06\t10000\t10000\tyes" in clipboard_text
    assert "\n3\tpositive\t0.03\t0.02\t2e-06\t10000\t10000\tyes" in clipboard_text
