import math
import unittest

from probe_station_gui.surface_mapping import (
    SurfaceMapConfig,
    build_surface_route_plan,
    build_surface_route,
    route_length_mm,
    safe_radius_limits,
    safe_approach_waypoints,
    segment_is_safe,
    validate_surface_route,
)


class SurfaceMappingRouteTest(unittest.TestCase):
    def test_circle_route_uses_requested_radius_and_turn_count(self) -> None:
        config = SurfaceMapConfig(
            route_mode="circle",
            inner_diameter_mm=20.0,
            outer_diameter_mm=40.0,
            circle_count=3,
            point_spacing_mm=2.0,
        )

        route = build_surface_route(config, (20.0, 0.0))

        _table_radius, _inner_guard, inner_route, outer_guard = safe_radius_limits(config)
        expected_radii = {
            round(inner_route, 6),
            round((inner_route + outer_guard) / 2.0, 6),
            round(outer_guard, 6),
        }
        radii = [math.hypot(x, y) for x, y in route]
        rounded_radii = {round(radius, 6) for radius in radii}
        self.assertEqual(rounded_radii, expected_radii)
        self.assertAlmostEqual(
            route_length_mm(route),
            2.0 * math.pi * sum(expected_radii) + outer_guard - inner_route,
            delta=1.0,
        )

    def test_spiral_route_uses_explicit_inner_and_outer_radii(self) -> None:
        config = SurfaceMapConfig(
            route_mode="spiral",
            inner_diameter_mm=8.0,
            outer_diameter_mm=36.0,
            radial_pitch_mm=2.0,
            point_spacing_mm=1.5,
        )

        route = build_surface_route(config, (18.0, 0.0))
        radii = [math.hypot(x, y) for x, y in route]
        _table_radius, _inner_guard, inner_route, outer_guard = safe_radius_limits(config)

        self.assertAlmostEqual(radii[0], inner_route, places=6)
        self.assertAlmostEqual(radii[-1], outer_guard, places=6)
        self.assertTrue(all(inner_route <= radius <= outer_guard for radius in radii))

    def test_route_building_ignores_unsafe_current_position(self) -> None:
        config = SurfaceMapConfig(
            route_mode="circle",
            inner_diameter_mm=2.0,
            outer_diameter_mm=40.0,
            circle_count=1,
            point_spacing_mm=2.0,
        )

        route = build_surface_route(config, (0.0, 0.0))

        self.assertGreater(len(route), 0)
        self.assertTrue(all(math.hypot(x, y) >= 1.0 for x, y in route))

    def test_safe_approach_adds_waypoints_around_center(self) -> None:
        config = SurfaceMapConfig(
            route_mode="circle",
            inner_diameter_mm=20.0,
            outer_diameter_mm=40.0,
            point_spacing_mm=2.0,
        )
        start = (20.0, 0.0)
        target = (-20.0, 0.0)

        self.assertFalse(
            segment_is_safe(
                start,
                target,
                inner_guard_mm=10.0,
                outer_guard_mm=20.0,
                validate_step_mm=config.validate_step_mm,
            )
        )
        waypoints = safe_approach_waypoints(config, start, target)

        self.assertGreater(len(waypoints), 0)
        validate_surface_route(config, [*waypoints, target], start_xy=start)

    def test_route_plan_rejects_unsafe_current_position(self) -> None:
        config = SurfaceMapConfig(
            route_mode="circle",
            inner_diameter_mm=2.0,
            outer_diameter_mm=40.0,
            circle_count=1,
            point_spacing_mm=2.0,
        )

        with self.assertRaises(ValueError):
            build_surface_route_plan(config, (0.0, 0.0))

    def test_single_circle_uses_outer_diameter(self) -> None:
        config = SurfaceMapConfig(
            route_mode="circle",
            inner_diameter_mm=20.0,
            outer_diameter_mm=40.0,
            circle_count=1,
            point_spacing_mm=2.0,
        )

        route = build_surface_route(config, (20.0, 0.0))
        radii = [math.hypot(x, y) for x, y in route]

        self.assertTrue(all(abs(radius - 20.0) < 1e-6 for radius in radii))

    def test_grid_route_uses_common_inner_and_outer_diameters(self) -> None:
        config = SurfaceMapConfig(
            route_mode="grid",
            inner_diameter_mm=4.0,
            outer_diameter_mm=12.0,
            grid_step_mm=3.0,
        )

        route = build_surface_route(config, (6.0, 0.0))
        radii = [math.hypot(x, y) for x, y in route]

        self.assertGreater(len(route), 0)
        self.assertTrue(all(2.0 <= radius <= 6.0 for radius in radii))

    def test_safe_radius_limits_use_common_diameters(self) -> None:
        config = SurfaceMapConfig(inner_diameter_mm=2.0, outer_diameter_mm=56.0)

        table_radius, _inner_guard, _inner_route, outer_guard = safe_radius_limits(config)

        self.assertEqual(table_radius, 28.0)
        self.assertEqual(outer_guard, 28.0)


if __name__ == "__main__":
    unittest.main()
