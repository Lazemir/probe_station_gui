from probe_station_gui.camera import imaging
from probe_station_gui.camera import microscope_artifacts
from probe_station_gui.camera import scan_scale_refinement
from probe_station_gui.camera import scan_stitching


def test_imaging_scan_types_are_the_canonical_cross_owner_identities() -> None:
    assert (
        microscope_artifacts.MicroscopeScaleCalibration
        is imaging.MicroscopeScaleCalibration
    )
    assert (
        scan_scale_refinement.MicroscopeScaleCalibration
        is imaging.MicroscopeScaleCalibration
    )
    assert scan_stitching.MicroscopeScanPlan is imaging.MicroscopeScanPlan
    assert scan_stitching.MicroscopeScanTile is imaging.MicroscopeScanTile


def test_imaging_does_not_retain_moved_domain_interfaces() -> None:
    moved_names = {
        "CompiledFlatFieldCorrection",
        "FlatFieldProfile",
        "MicroscopeCaptureResult",
        "MicroscopeImageMetadata",
        "apply_compiled_flat_field_correction",
        "apply_flat_field_correction",
        "apply_self_flat_field_correction",
        "build_flat_field_profile",
        "build_median_flat_field_profile",
        "compile_flat_field_correction",
        "median_flat_field_reference",
        "qimage_to_rgb_array",
        "refine_scan_scale_from_tile_overlaps",
        "render_microscope_overlay",
        "rgb_array_to_qimage",
        "route_photo_filename",
        "safe_filename_component",
        "save_microscope_image",
        "scan_tile_filename",
        "stitch_scan_tiles",
        "_positive_float",
        "_qimage_to_rgb_array",
        "_rgb_array_to_qimage",
    }

    assert moved_names.isdisjoint(vars(imaging))
