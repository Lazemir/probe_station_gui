import csv
from types import SimpleNamespace

from probe_station_gui.route.measurement_csv import (
    CSV_FIELDS,
    RouteMeasurementCsvWriter,
    route_measurement_record_csv_row,
)


def test_route_measurement_record_csv_row_formats_contact_metrics() -> None:
    record = SimpleNamespace(
        timestamp="2026-06-25T12:00:00",
        structure_number=7,
        nplc="1",
        measurement_type="2wire",
        n_measurements=4,
        resistance_ohm=123.456789,
        resistance_rms_ohm=0.0123,
        relative_rms=0.0001,
        status="bad_contact",
        contact_quality=SimpleNamespace(
            status="bad_contact",
            median_ohm=123.0,
            mad_sigma_ohm=4.5,
            p95_abs_step_ohm=6.7,
            span_ohm=8.9,
            compliance_hits=2,
            polarity_sign_mismatch_count=1,
        ),
    )

    row = route_measurement_record_csv_row(record)

    assert list(row) == CSV_FIELDS
    assert row["structure_number"] == "7"
    assert row["resistance_ohm"] == "123.456789"
    assert row["contact_quality"] == "bad_contact"
    assert row["contact_mad_sigma_ohm"] == "4.5"
    assert row["contact_compliance_hits"] == "2"
    assert row["contact_polarity_sign_mismatches"] == "1"


def test_route_measurement_csv_writer_preserves_header_and_appends(tmp_path) -> None:
    path = tmp_path / "route.csv"
    writer = RouteMeasurementCsvWriter(path)
    record = SimpleNamespace(
        timestamp="now",
        structure_number=1,
        nplc="fast",
        measurement_type="resistance",
        n_measurements=1,
        resistance_ohm=10.0,
        resistance_rms_ohm=0.0,
        relative_rms=0.0,
        status="ok",
        contact_quality=None,
    )

    writer.append(record)
    writer.append(record)

    with path.open("r", encoding="utf-8", newline="") as handle:
        rows = list(csv.DictReader(handle))

    assert list(rows[0]) == CSV_FIELDS
    assert [row["status"] for row in rows] == ["ok", "ok"]
