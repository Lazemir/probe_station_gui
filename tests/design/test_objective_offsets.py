import unittest

from probe_station_gui.design.objective_offsets import (
    ObjectiveOffsetReference,
    base_objective_name,
    calibrated_objective_offset,
    camera_stage_to_raw_stage,
    objective_xy_offset,
    objective_xy_offset_is_configured,
    raw_stage_to_camera_stage,
)
from probe_station_gui.settings_manager import ObjectiveCalibrationSettings


class ObjectiveOffsetTest(unittest.TestCase):
    def test_base_objective_is_lowest_magnification(self) -> None:
        profiles = {
            "X20": ObjectiveCalibrationSettings(name="X20", magnification=20.0),
            "X2": ObjectiveCalibrationSettings(name="X2", magnification=2.0),
            "X5": ObjectiveCalibrationSettings(name="X5", magnification=5.0),
        }

        self.assertEqual(base_objective_name(profiles), "X2")

    def test_base_objective_offset_is_zero_even_when_not_saved(self) -> None:
        profiles = {
            "X5": ObjectiveCalibrationSettings(name="X5", magnification=5.0),
            "X20": ObjectiveCalibrationSettings(
                name="X20",
                magnification=20.0,
                xy_offset_x_mm=0.12,
                xy_offset_y_mm=-0.08,
                xy_offset_configured=True,
            ),
        }

        self.assertEqual(objective_xy_offset(profiles, "X5"), (0.0, 0.0))
        self.assertTrue(objective_xy_offset_is_configured(profiles, "X5"))
        self.assertEqual(objective_xy_offset(profiles, "X20"), (0.12, -0.08))

    def test_calibration_uses_same_centered_feature_delta(self) -> None:
        reference = ObjectiveOffsetReference(
            objective_name="X5",
            stage_xy=(10.0, 20.0),
            offset_xy=(0.0, 0.0),
        )

        offset = calibrated_objective_offset(reference, (10.125, 19.75))

        self.assertEqual(offset, (0.125, -0.25))
        self.assertEqual(
            raw_stage_to_camera_stage((10.125, 19.75), offset),
            reference.stage_xy,
        )
        self.assertEqual(
            camera_stage_to_raw_stage(reference.stage_xy, offset),
            (10.125, 19.75),
        )


if __name__ == "__main__":
    unittest.main()
