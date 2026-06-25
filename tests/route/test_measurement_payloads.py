import math
from types import SimpleNamespace

from probe_station_gui.route.contact_quality import (
    RouteContactQuality,
    RouteMeasurementSample,
)
from probe_station_gui.route.measurement import (
    RouteContactSeekResult,
    RouteMeasurementRecord,
)
from probe_station_gui.route.measurement_payloads import (
    focus_result_to_dict,
    json_ready,
    route_api_contact_seek_payload,
    route_api_measurement_record_payload,
    route_artifact_public_payload,
    route_artifact_record,
    route_contact_seek_payload,
    route_external_result_payload,
    route_measurement_record_payload,
)


def test_json_ready_preserves_json_scalars_and_drops_nonfinite_floats() -> None:
    assert json_ready(True) is True
    assert json_ready(None) is None
    assert json_ready("ok") == "ok"
    assert json_ready(3) == 3
    assert json_ready(1.5) == 1.5
    assert json_ready(float("nan")) is None
    assert json_ready(float("inf")) is None


def test_route_measurement_record_payload_matches_status_shape() -> None:
    sample = RouteMeasurementSample(
        sample_index=2,
        differential_resistance_ohm=float("inf"),
        compliance_hit=True,
        negative_current_a=0.001,
    )
    quality = RouteContactQuality(
        assessed=True,
        good=False,
        status="bad_contact",
        median_ohm=float("nan"),
        mad_sigma_ohm=12.5,
        p95_abs_step_ohm=33.0,
        span_ohm=100.0,
        compliance_hits=1,
        polarity_sign_mismatch_count=2,
        reasons=("unstable",),
        failure_criteria=("mad_sigma",),
    )
    record = RouteMeasurementRecord(
        timestamp="2026-06-25T01:23:45",
        structure_number=7,
        nplc="1",
        measurement_type="4wire",
        n_measurements=5,
        resistance_ohm=float("nan"),
        resistance_rms_ohm=0.2,
        relative_rms=0.01,
        status="bad_contact",
        contact_quality=quality,
        raw_samples=(sample,),
    )

    payload = route_measurement_record_payload(record)

    assert payload["timestamp"] == "2026-06-25T01:23:45"
    assert payload["structure_number"] == 7
    assert payload["resistance_ohm"] is None
    assert payload["contact_quality"]["median_ohm"] is None
    assert payload["contact_quality"]["reasons"] == ["unstable"]
    assert payload["contact_quality"]["failure_criteria"] == ["mad_sigma"]
    assert payload["raw_samples"] == [
        {
            "sample_index": 2,
            "differential_resistance_ohm": None,
            "compliance_hit": True,
            "negative_source_voltage_v": None,
            "negative_measured_voltage_v": None,
            "negative_current_a": 0.001,
            "negative_resistance_ohm": None,
            "positive_source_voltage_v": None,
            "positive_measured_voltage_v": None,
            "positive_current_a": None,
            "positive_resistance_ohm": None,
        }
    ]


def test_route_api_measurement_record_payload_matches_existing_api_shape() -> None:
    sample = RouteMeasurementSample(
        sample_index=2,
        differential_resistance_ohm=float("inf"),
        compliance_hit=True,
        negative_current_a=0.001,
    )
    quality = RouteContactQuality(
        assessed=True,
        good=False,
        status="bad_contact",
        median_ohm=float("nan"),
        mad_sigma_ohm=12.5,
        p95_abs_step_ohm=33.0,
        span_ohm=100.0,
        compliance_hits=1,
        polarity_sign_mismatch_count=2,
        reasons=("unstable",),
        failure_criteria=("mad_sigma",),
    )
    record = RouteMeasurementRecord(
        timestamp="2026-06-25T01:23:45",
        structure_number=7,
        nplc="1",
        measurement_type="4wire",
        n_measurements=5,
        resistance_ohm=float("nan"),
        resistance_rms_ohm=0.2,
        relative_rms=0.01,
        status="bad_contact",
        contact_quality=quality,
        raw_samples=(sample,),
    )

    payload = route_api_measurement_record_payload(record)

    assert "nplc" not in payload
    assert "measurement_type" not in payload
    assert payload["resistance_ohm"] is None
    assert payload["contact_quality"]["median_ohm"] is None
    assert payload["raw_samples"][0]["differential_resistance_ohm"] is None


def test_route_contact_seek_payload_normalizes_nonfinite_fields() -> None:
    seek = RouteContactSeekResult(
        found=True,
        status="found",
        attempts=2,
        initial_status="bad_contact",
        final_status="good",
        depth_below_down_mm=0.002,
        axis_a_lowering_mm=1.25,
        step_mm=0.001,
        max_depth_mm=math.nan,
    )

    payload = route_contact_seek_payload(seek)

    assert payload == {
        "found": True,
        "status": "found",
        "attempts": 2,
        "initial_status": "bad_contact",
        "final_status": "good",
        "depth_below_down_mm": 0.002,
        "axis_a_lowering_mm": 1.25,
        "step_mm": 0.001,
        "max_depth_mm": None,
    }
    assert route_contact_seek_payload(None) is None


def test_route_api_contact_seek_payload_accepts_missing_fields() -> None:
    payload = route_api_contact_seek_payload(SimpleNamespace(found=True))

    assert payload == {
        "found": True,
        "status": "",
        "attempts": 0,
        "initial_status": "",
        "final_status": "",
        "depth_below_down_mm": None,
        "axis_a_lowering_mm": None,
        "step_mm": None,
        "max_depth_mm": None,
    }


def test_focus_result_to_dict_accepts_existing_shapes() -> None:
    source = {"focus_best_z_mm": 1.02}
    copied = focus_result_to_dict(source)
    assert copied == source
    assert copied is not source

    class FocusWithToDict:
        def to_dict(self) -> dict[str, object]:
            return {"focus_score": 42.0}

    assert focus_result_to_dict(FocusWithToDict()) == {"focus_score": 42.0}

    focus = SimpleNamespace(
        objective_name="10x",
        mode="local",
        start_z_mm=1.0,
        best_z_mm=1.02,
        delta_um=20.0,
        best_score=11.0,
        sample_count=9,
        edge_peak=3.5,
        range_mm=0.2,
        fine_step_mm=0.01,
        lower_z_mm=0.9,
        upper_z_mm=1.1,
    )

    assert focus_result_to_dict(focus) == {
        "objective_name": "10x",
        "mode": "local",
        "focus_start_z_mm": 1.0,
        "focus_best_z_mm": 1.02,
        "focus_delta_um": 20.0,
        "focus_score": 11.0,
        "focus_sample_count": 9,
        "focus_edge_peak": 3.5,
        "autofocus_range_mm": 0.2,
        "autofocus_fine_step_mm": 0.01,
        "autofocus_lower_z_mm": 0.9,
        "autofocus_upper_z_mm": 1.1,
    }


def test_route_external_result_payload_preserves_api_shape() -> None:
    payload = route_external_result_payload(
        {
            "status": " OK ",
            "summary": {"r": 42},
            "files": ["a.csv"],
            "message": " done ",
            "request_id": 7,
            "external_measurement_request_id": 8,
        },
        timestamp_utc="2026-06-25T01:23:45Z",
    )

    assert payload == {
        "status": "ok",
        "summary": {"r": 42},
        "files": ["a.csv"],
        "message": "done",
        "timestamp_utc": "2026-06-25T01:23:45Z",
        "external_measurement_request_id": 8,
    }


def test_route_external_result_payload_defaults_invalid_shapes() -> None:
    payload = route_external_result_payload(
        {
            "status": "",
            "summary": "not a dict",
            "files": "not a list",
        },
        timestamp_utc="t1",
    )

    assert payload == {
        "status": "ok",
        "summary": {},
        "files": [],
        "message": "",
        "timestamp_utc": "t1",
    }


def test_route_artifact_record_copies_data_and_metadata() -> None:
    metadata = {"point": 7}
    source_data = bytearray(b"abc")

    artifact = route_artifact_record(
        artifact_id="id1",
        data=source_data,
        filename="point.png",
        content_type="image/png",
        kind="photo",
        metadata=metadata,
        created_at_utc="t2",
    )

    metadata["point"] = 8
    source_data[:] = b"changed"

    assert artifact == {
        "artifact_id": "id1",
        "filename": "point.png",
        "content_type": "image/png",
        "kind": "photo",
        "metadata": {"point": 7},
        "created_at_utc": "t2",
        "size_bytes": 3,
        "data": b"abc",
    }


def test_route_artifact_public_payload_removes_binary_data() -> None:
    public = route_artifact_public_payload(
        {
            "artifact_id": "id1",
            "filename": "point.png",
            "data": b"abc",
            "size_bytes": 3,
        }
    )

    assert public == {
        "artifact_id": "id1",
        "filename": "point.png",
        "size_bytes": 3,
    }
