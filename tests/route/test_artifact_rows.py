import unittest
from pathlib import Path

from probe_station_gui.route.artifact_rows import (
    ROUTE_CONTACT_HEIGHT_MAP_FIELDS,
    ROUTE_PHOTO_FOCUS_MAP_FIELDS,
    route_contact_height_map_path,
    route_contact_height_map_row,
    route_photo_focus_map_path,
    route_photo_focus_map_row,
)
from probe_station_gui.route.contact_quality import RouteContactQuality
from probe_station_gui.route.measurement_records import (
    RouteContactHeightRecord,
    RouteContactSeekResult,
    RoutePhotoRecord,
)


class RouteArtifactRowsTest(unittest.TestCase):
    def test_photo_focus_map_row_preserves_route_and_focus_fields(self) -> None:
        record = RoutePhotoRecord(
            timestamp="2026-06-26T10:20:30Z",
            path="C:/data/routes/photos/point-001.png",
            structure_number=17,
            point_index=2,
            point_id="p-002",
            label="Pad 2",
            design_center=(12.5, -3.25),
            stage_xy=(100.125, 200.5),
            focus={
                "objective_name": "10X",
                "focus_start_z_mm": 1.1,
                "focus_best_z_mm": 1.125,
                "focus_delta_um": 25.0,
                "focus_score": 0.875,
                "focus_sample_count": 7,
                "focus_edge_peak": 321.0,
                "autofocus_range_mm": 0.4,
                "autofocus_fine_step_mm": 0.005,
                "autofocus_lower_z_mm": 0.9,
                "autofocus_upper_z_mm": 1.3,
            },
        )

        row = route_photo_focus_map_row(record, 3, 9, route_name="route-a")

        self.assertEqual(tuple(row.keys()), ROUTE_PHOTO_FOCUS_MAP_FIELDS)
        self.assertEqual(
            route_photo_focus_map_path(record),
            Path("C:/data/routes/photos/route-photo-focus-map.csv").resolve(),
        )
        self.assertEqual(row["route_name"], "route-a")
        self.assertEqual(row["route_position"], 3)
        self.assertEqual(row["route_total"], 9)
        self.assertEqual(row["structure_number"], 17)
        self.assertEqual(row["point_index"], 2)
        self.assertEqual(row["point_id"], "p-002")
        self.assertEqual(row["label"], "Pad 2")
        self.assertEqual(row["design_x"], 12.5)
        self.assertEqual(row["design_y"], -3.25)
        self.assertEqual(row["stage_x"], 100.125)
        self.assertEqual(row["stage_y"], 200.5)
        self.assertEqual(row["photo_path"], "C:/data/routes/photos/point-001.png")
        self.assertEqual(row["objective_name"], "10X")
        self.assertEqual(row["focus_start_z_mm"], 1.1)
        self.assertEqual(row["focus_best_z_mm"], 1.125)
        self.assertEqual(row["focus_delta_um"], 25.0)
        self.assertEqual(row["focus_score"], 0.875)
        self.assertEqual(row["focus_sample_count"], 7)
        self.assertEqual(row["focus_edge_peak"], 321.0)
        self.assertEqual(row["autofocus_range_mm"], 0.4)
        self.assertEqual(row["autofocus_fine_step_mm"], 0.005)
        self.assertEqual(row["autofocus_lower_z_mm"], 0.9)
        self.assertEqual(row["autofocus_upper_z_mm"], 1.3)

    def test_contact_height_row_preserves_existing_formatted_values(self) -> None:
        record = RouteContactHeightRecord(
            timestamp="2026-06-26T11:00:00Z",
            structure_number=42,
            point_index=5,
            point_id="pad-42",
            label="Pad 42",
            design_center=(10.0, 20.0),
            stage_xy=(1.0, 2.0),
            measurement_status="ok",
            resistance_ohm=1000.5,
            resistance_rms_ohm=2.5,
            relative_rms=0.0025,
            contact_quality=RouteContactQuality(
                assessed=True,
                good=True,
                status="good",
                median_ohm=999.5,
                mad_sigma_ohm=1.2,
                p95_abs_step_ohm=2.0,
                span_ohm=3.0,
                compliance_hits=4,
            ),
            contact_found=True,
            contact_depth_below_down_mm=0.002,
            contact_axis_a_lowering_mm=1.002,
            contact_seek=RouteContactSeekResult(
                found=True,
                status="found",
                attempts=3,
                initial_status="bad_contact",
                final_status="good",
                depth_below_down_mm=0.002,
                axis_a_lowering_mm=1.002,
                step_mm=0.001,
                max_depth_mm=0.002,
            ),
        )

        row = route_contact_height_map_row(record, 1, 3, route_name="route-b")

        self.assertEqual(tuple(row.keys()), ROUTE_CONTACT_HEIGHT_MAP_FIELDS)
        self.assertEqual(
            route_contact_height_map_path("C:/data/routes/route-measurement.csv"),
            Path("C:/data/routes/route-contact-height-map.csv").resolve(),
        )
        self.assertEqual(row["route_name"], "route-b")
        self.assertEqual(row["design_x"], 10.0)
        self.assertEqual(row["stage_y"], 2.0)
        self.assertEqual(row["measurement_status"], "ok")
        self.assertEqual(row["resistance_ohm"], "1000.5")
        self.assertEqual(row["resistance_rms_ohm"], "2.5")
        self.assertEqual(row["relative_rms"], "0.0025")
        self.assertEqual(row["contact_quality"], "good")
        self.assertEqual(row["contact_median_ohm"], "999.5")
        self.assertEqual(row["contact_mad_sigma_ohm"], "1.2")
        self.assertEqual(row["contact_p95_abs_step_ohm"], "2")
        self.assertEqual(row["contact_span_ohm"], "3")
        self.assertEqual(row["contact_compliance_hits"], 4)
        self.assertEqual(row["contact_found"], "true")
        self.assertEqual(row["contact_depth_below_down_mm"], "0.002")
        self.assertEqual(row["contact_axis_a_lowering_mm"], "1.002")
        self.assertEqual(row["contact_seek_used"], "true")
        self.assertEqual(row["contact_seek_found"], "true")
        self.assertEqual(row["contact_seek_status"], "found")
        self.assertEqual(row["contact_seek_attempts"], 3)
        self.assertEqual(row["contact_seek_initial_status"], "bad_contact")
        self.assertEqual(row["contact_seek_final_status"], "good")
        self.assertEqual(row["contact_seek_depth_below_down_mm"], "0.002")
        self.assertEqual(row["contact_seek_axis_a_lowering_mm"], "1.002")
        self.assertEqual(row["contact_seek_step_mm"], "0.001")
        self.assertEqual(row["contact_seek_max_depth_mm"], "0.002")

    def test_contact_height_row_leaves_optional_fields_empty_without_quality_or_seek(
        self,
    ) -> None:
        record = RouteContactHeightRecord(
            timestamp="2026-06-26T12:00:00Z",
            structure_number=7,
            point_index=1,
            point_id="pad-7",
            label="Pad 7",
            design_center=(0.0, 1.0),
            stage_xy=(2.0, 3.0),
            measurement_status="open",
            resistance_ohm=float("nan"),
            resistance_rms_ohm=float("nan"),
            relative_rms=float("nan"),
            contact_quality=None,
            contact_found=False,
            contact_depth_below_down_mm=float("nan"),
            contact_axis_a_lowering_mm=float("nan"),
            contact_seek=None,
        )

        row = route_contact_height_map_row(record, 4, 10, route_name="")

        self.assertEqual(row["route_name"], "")
        self.assertEqual(row["resistance_ohm"], "")
        self.assertEqual(row["resistance_rms_ohm"], "")
        self.assertEqual(row["relative_rms"], "")
        self.assertEqual(row["contact_quality"], "")
        self.assertEqual(row["contact_median_ohm"], "")
        self.assertEqual(row["contact_mad_sigma_ohm"], "")
        self.assertEqual(row["contact_p95_abs_step_ohm"], "")
        self.assertEqual(row["contact_span_ohm"], "")
        self.assertEqual(row["contact_compliance_hits"], "")
        self.assertEqual(row["contact_found"], "false")
        self.assertEqual(row["contact_depth_below_down_mm"], "")
        self.assertEqual(row["contact_axis_a_lowering_mm"], "")
        self.assertEqual(row["contact_seek_used"], "false")
        self.assertEqual(row["contact_seek_found"], "")
        self.assertEqual(row["contact_seek_status"], "")
        self.assertEqual(row["contact_seek_attempts"], "")
        self.assertEqual(row["contact_seek_initial_status"], "")
        self.assertEqual(row["contact_seek_final_status"], "")
        self.assertEqual(row["contact_seek_depth_below_down_mm"], "")
        self.assertEqual(row["contact_seek_axis_a_lowering_mm"], "")
        self.assertEqual(row["contact_seek_step_mm"], "")
        self.assertEqual(row["contact_seek_max_depth_mm"], "")


if __name__ == "__main__":
    unittest.main()
