import tempfile
import unittest
from pathlib import Path

import numpy as np

from probe_station_gui.design_model import DesignDocument
from probe_station_gui.route.model import (
    MeasurementRoute,
    RouteModelError,
    structure_number_from_labels,
)

REPO_ROOT = Path(__file__).resolve().parents[2]


class MeasurementRouteTest(unittest.TestCase):
    def _make_document(self, *, top_cell_name: str = "TOP") -> DesignDocument:
        return DesignDocument(
            path=REPO_ROOT / "tests" / "fixtures" / "synthetic.gds",
            library=object(),
            top_cell=object(),
            top_cell_name=top_cell_name,
            cell_names=(top_cell_name,),
            dbu=1e-6,
            user_unit=1e-9,
            bounds=(0.0, 0.0, 100.0, 200.0),
            polygons_by_layer={
                (1, 0): (np.asarray([[0.0, 0.0], [10.0, 0.0], [0.0, 10.0]]),)
            },
            visible_layers=frozenset({(1, 0)}),
        )

    def test_save_load_and_validate_route(self) -> None:
        document = self._make_document()
        route = MeasurementRoute.default_for_document(document, name="Array A")
        route.set_needle_offsets(1.0, 2.0, -3.0, -4.0)
        point = route.add_point((10.0, 20.0))

        self.assertEqual(point.id, "p001")
        self.assertEqual(
            [hit for _offset, hit in route.needle_hits_for_point(point)],
            [(11.0, 22.0), (7.0, 16.0)],
        )

        with tempfile.TemporaryDirectory() as tmpdir:
            route_path = Path(tmpdir) / "array-a.probe-route.json"
            route.save(route_path)
            loaded = MeasurementRoute.load(route_path)

        loaded.validate_for_document(document)
        self.assertEqual(loaded.name, "Array A")
        self.assertEqual(len(loaded.points), 1)
        self.assertEqual(loaded.to_measurement_targets()[0].design_center, (10.0, 20.0))

    def test_route_rejects_other_top_cell(self) -> None:
        route = MeasurementRoute.default_for_document(self._make_document())

        with self.assertRaises(RouteModelError):
            route.validate_for_document(self._make_document(top_cell_name="OTHER"))

    def test_add_linear_points(self) -> None:
        route = MeasurementRoute.default_for_document(self._make_document())

        route.add_point((1.0, 1.0))
        added = route.add_linear_points(
            (10.0, 20.0),
            (2.5, -1.0),
            3,
            clear_existing=True,
        )

        self.assertEqual(len(added), 3)
        self.assertEqual(
            [point.camera_center for point in route.points],
            [(10.0, 20.0), (12.5, 19.0), (15.0, 18.0)],
        )
        self.assertEqual([point.label for point in route.points], ["P001", "P002", "P003"])

    def test_linear_points_reject_zero_step_for_multiple_points(self) -> None:
        route = MeasurementRoute.default_for_document(self._make_document())

        with self.assertRaises(RouteModelError):
            route.add_linear_points((0.0, 0.0), (0.0, 0.0), 2)

    def test_add_grid_points_serpentine(self) -> None:
        route = MeasurementRoute.default_for_document(self._make_document())

        added = route.add_grid_points(
            (10.0, 20.0),
            (2.0, 0.0),
            3,
            (0.0, -5.0),
            2,
            serpentine=True,
        )

        self.assertEqual(len(added), 6)
        self.assertEqual(
            [point.camera_center for point in route.points],
            [
                (10.0, 20.0),
                (12.0, 20.0),
                (14.0, 20.0),
                (14.0, 15.0),
                (12.0, 15.0),
                (10.0, 15.0),
            ],
        )

    def test_grid_points_reject_zero_step_for_multiple_points(self) -> None:
        route = MeasurementRoute.default_for_document(self._make_document())

        with self.assertRaises(RouteModelError):
            route.add_grid_points((0.0, 0.0), (0.0, 0.0), 2, (0.0, 1.0), 1)
        with self.assertRaises(RouteModelError):
            route.add_grid_points((0.0, 0.0), (1.0, 0.0), 1, (0.0, 0.0), 2)

    def test_structure_number_from_labels_uses_trailing_number_then_default(
        self,
    ) -> None:
        self.assertEqual(
            structure_number_from_labels("pad 007", "p001", default=3),
            7,
        )
        self.assertEqual(
            structure_number_from_labels("pad", "p042", default=3),
            42,
        )
        self.assertEqual(
            structure_number_from_labels("pad", "", default=3),
            3,
        )


if __name__ == "__main__":
    unittest.main()
