import json
import subprocess
import sys
import textwrap
import unittest

import numpy as np
from PySide6.QtGui import QColor, QImage

from probe_station_gui.camera.flat_field_processing import (
    apply_compiled_flat_field_correction,
    apply_flat_field_correction,
    apply_self_flat_field_correction,
    build_flat_field_profile,
    build_median_flat_field_profile,
    compile_flat_field_correction,
    qimage_to_rgb_array,
    rgb_array_to_qimage,
)


def test_qimage_array_conversion_preserves_padded_rows_and_detaches_memory() -> None:
    width = 3
    height = 2
    stride = 12
    rows = np.full((height, stride), 239, dtype=np.uint8)
    pixels = np.asarray(
        [
            [[1, 2, 3], [4, 5, 6], [7, 8, 9]],
            [[11, 12, 13], [14, 15, 16], [17, 18, 19]],
        ],
        dtype=np.uint8,
    )
    rows[:, : width * 3] = pixels.reshape((height, width * 3))
    source = QImage(
        rows.data,
        width,
        height,
        stride,
        QImage.Format_RGB888,
    )

    converted = qimage_to_rgb_array(source)
    rows[:, : width * 3] = 0

    assert np.array_equal(converted, pixels)
    restored = rgb_array_to_qimage(converted)
    converted[:] = 255
    assert restored.format() == QImage.Format_RGB32
    assert restored.pixelColor(0, 0) == QColor(1, 2, 3)
    assert restored.pixelColor(2, 1) == QColor(17, 18, 19)


def test_flat_field_interfaces_keep_profile_compilation_and_median_behavior() -> None:
    frames: list[QImage] = []
    for dark_x in (2, 8, 14):
        image = QImage(17, 11, QImage.Format_RGB32)
        for y in range(image.height()):
            for x in range(image.width()):
                shade = 80 + 4 * x
                if x == dark_x:
                    shade = 5
                image.setPixelColor(x, y, QColor(shade, shade + 1, shade + 2))
        frames.append(image)

    profile = build_median_flat_field_profile(
        frames,
        blur_radius_px=9,
        max_gain=5.0,
    )
    compiled = compile_flat_field_correction(profile)
    direct = apply_flat_field_correction(frames[0], profile)
    cached = apply_compiled_flat_field_correction(frames[0], compiled)
    self_corrected = apply_self_flat_field_correction(
        frames[0],
        blur_radius_px=9,
        max_gain=5.0,
    )

    assert profile.image_size_px == (17, 11)
    assert compiled.image_size_px == profile.image_size_px
    assert direct == cached
    assert self_corrected.size() == frames[0].size()
    assert build_flat_field_profile(frames[0], blur_radius_px=8).blur_radius_px == 9


class FlatFieldProcessingCharacterizationTest(unittest.TestCase):
    def test_self_flat_field_correction_reduces_low_frequency_gradient(self) -> None:
        script = textwrap.dedent(
            """
            import json
            from PySide6.QtGui import QColor, QImage
            from probe_station_gui.camera.flat_field_processing import (
                apply_self_flat_field_correction,
            )

            width = 81
            image = QImage(width, 21, QImage.Format_RGB32)
            for y in range(image.height()):
                for x in range(width):
                    shade = 55 + int(90 * x / (width - 1))
                    image.setPixelColor(x, y, QColor(shade, shade, shade))

            corrected = apply_self_flat_field_correction(
                image,
                blur_radius_px=17,
                max_gain=4.0,
            )
            left = corrected.pixelColor(4, 10).red()
            right = corrected.pixelColor(width - 5, 10).red()
            center = corrected.pixelColor(width // 2, 10).red()
            print(json.dumps({"left": left, "center": center, "right": right}))
            """
        )
        completed = subprocess.run(
            [sys.executable, "-c", script],
            cwd=".",
            text=True,
            capture_output=True,
            check=False,
        )
        self.assertEqual(completed.returncode, 0, completed.stderr)
        result = json.loads(completed.stdout.strip().splitlines()[-1])

        self.assertLess(abs(result["left"] - result["right"]), 16)
        self.assertGreater(result["center"], 80)

    def test_flat_field_profile_rejects_frame_size_mismatch(self) -> None:
        script = textwrap.dedent(
            """
            from PySide6.QtGui import QColor, QImage
            from probe_station_gui.camera.flat_field_processing import (
                apply_flat_field_correction,
                build_flat_field_profile,
            )

            reference = QImage(20, 20, QImage.Format_RGB32)
            reference.fill(QColor(120, 120, 120))
            frame = QImage(21, 20, QImage.Format_RGB32)
            frame.fill(QColor(120, 120, 120))
            profile = build_flat_field_profile(reference, blur_radius_px=9)
            try:
                apply_flat_field_correction(frame, profile)
            except ValueError as exc:
                print(str(exc))
            else:
                raise AssertionError("expected ValueError")
            """
        )
        completed = subprocess.run(
            [sys.executable, "-c", script],
            cwd=".",
            text=True,
            capture_output=True,
            check=False,
        )
        self.assertEqual(completed.returncode, 0, completed.stderr)
        self.assertIn("frame size", completed.stdout)

    def test_compiled_flat_field_matches_profile_application(self) -> None:
        script = textwrap.dedent(
            """
            import json
            from PySide6.QtGui import QColor, QImage
            from probe_station_gui.camera.flat_field_processing import (
                apply_compiled_flat_field_correction,
                apply_flat_field_correction,
                build_flat_field_profile,
                compile_flat_field_correction,
            )

            reference = QImage(31, 17, QImage.Format_RGB32)
            frame = QImage(31, 17, QImage.Format_RGB32)
            for y in range(reference.height()):
                for x in range(reference.width()):
                    shade = 45 + 4 * x
                    reference.setPixelColor(x, y, QColor(shade, shade + 3, shade // 2))
                    frame.setPixelColor(x, y, QColor(100, 110, 70))

            profile = build_flat_field_profile(reference, blur_radius_px=9, max_gain=5.0)
            expected = apply_flat_field_correction(frame, profile)
            compiled = compile_flat_field_correction(profile)
            actual = apply_compiled_flat_field_correction(frame, compiled)
            same = all(
                expected.pixelColor(x, y) == actual.pixelColor(x, y)
                for y in range(frame.height())
                for x in range(frame.width())
            )
            print(json.dumps({"same": same, "size": list(compiled.image_size_px)}))
            """
        )
        completed = subprocess.run(
            [sys.executable, "-c", script],
            cwd=".",
            text=True,
            capture_output=True,
            check=False,
        )
        self.assertEqual(completed.returncode, 0, completed.stderr)
        result = json.loads(completed.stdout.strip().splitlines()[-1])
        self.assertTrue(result["same"])
        self.assertEqual(result["size"], [31, 17])

    def test_median_flat_field_profile_ignores_moving_dark_features(self) -> None:
        script = textwrap.dedent(
            """
            import json
            from PySide6.QtGui import QColor, QImage
            from probe_station_gui.camera.flat_field_processing import (
                apply_flat_field_correction,
                build_median_flat_field_profile,
            )

            width = 81
            frames = []
            for dark_x in (12, 40, 68):
                image = QImage(width, 21, QImage.Format_RGB32)
                for y in range(image.height()):
                    for x in range(width):
                        shade = 55 + int(90 * x / (width - 1))
                        if abs(x - dark_x) <= 2:
                            shade = max(0, shade - 45)
                        image.setPixelColor(x, y, QColor(shade, shade, shade))
                frames.append(image)

            profile = build_median_flat_field_profile(
                frames,
                blur_radius_px=17,
                max_gain=4.0,
            )
            corrected = apply_flat_field_correction(frames[0], profile)
            left = corrected.pixelColor(4, 10).red()
            right = corrected.pixelColor(width - 5, 10).red()
            dark = corrected.pixelColor(12, 10).red()
            print(json.dumps({"left": left, "right": right, "dark": dark}))
            """
        )
        completed = subprocess.run(
            [sys.executable, "-c", script],
            cwd=".",
            text=True,
            capture_output=True,
            check=False,
        )
        self.assertEqual(completed.returncode, 0, completed.stderr)
        result = json.loads(completed.stdout.strip().splitlines()[-1])

        self.assertLess(abs(result["left"] - result["right"]), 18)
        self.assertLess(result["dark"], result["left"] - 20)
