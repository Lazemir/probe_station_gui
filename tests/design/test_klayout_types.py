from __future__ import annotations

from dataclasses import FrozenInstanceError, fields
from pathlib import Path

import pytest

from probe_station_gui.design.klayout_types import (
    KLayoutConfig,
    PendingClick,
    RenderFailure,
    RenderFrame,
    RenderRequest,
    SnapRequest,
    SnapResponse,
    SnapFailure,
)
from probe_station_gui.design.model import SnapResult


def _config(*, rotation_quarter_turns: int = 0) -> KLayoutConfig:
    return KLayoutConfig(
        path=Path("layout.gds"),
        top_cell_name="TOP",
        visible_layers=frozenset({(1, 0), (2, 1)}),
        source_bounds=(0.0, 0.0, 10.0, 20.0),
        display_bounds=(-5.0, 5.0, 15.0, 15.0),
        rotation_quarter_turns=rotation_quarter_turns,
        generation=7,
        source_load_id="load-7",
    )


def test_klayout_config_normalizes_quarter_turns_and_is_frozen() -> None:
    config = _config(rotation_quarter_turns=5)

    assert config.rotation_quarter_turns == 1
    with pytest.raises(FrozenInstanceError):
        config.generation = 8  # type: ignore[misc]


def test_render_contracts_are_immutable_generation_tagged_values() -> None:
    config = _config()
    image = object()
    request = RenderRequest(
        request_id=11,
        config=config,
        world_box=(1.0, 2.0, 3.0, 4.0),
        pixel_width=800,
        pixel_height=600,
        viewport_generation=13,
        density=4.0,
    )
    frame = RenderFrame(
        request_id=request.request_id,
        config_generation=config.generation,
        viewport_generation=request.viewport_generation,
        world_box=request.world_box,
        pixel_width=request.pixel_width,
        pixel_height=request.pixel_height,
        density=request.density,
        image=image,
        elapsed_ms=12.5,
    )

    assert request.purpose == "viewport"
    assert frame.purpose == "viewport"
    assert frame.image is image
    assert {item.name for item in fields(RenderFrame)} == {
        "request_id",
        "config_generation",
        "viewport_generation",
        "world_box",
        "pixel_width",
        "pixel_height",
        "density",
        "image",
        "elapsed_ms",
        "purpose",
    }
    with pytest.raises(FrozenInstanceError):
        request.pixel_width = 400  # type: ignore[misc]
    with pytest.raises(FrozenInstanceError):
        frame.elapsed_ms = 1.0  # type: ignore[misc]


def test_snap_and_pending_click_contracts_preserve_correlation_and_payload() -> None:
    config = _config()
    request = SnapRequest(
        request_id=17,
        config=config,
        point=(3.0, 4.0),
        radius=0.25,
        purpose="click",
    )
    result = SnapResult(
        point=(3.0, 3.75),
        mode="segment",
        distance=0.25,
        segment_start=(0.0, 3.75),
        segment_end=(5.0, 3.75),
    )
    response = SnapResponse(
        request_id=request.request_id,
        config_generation=config.generation,
        raw_point=request.point,
        result=result,
        elapsed_ms=2.5,
        shapes_inspected=4,
        purpose=request.purpose,
    )
    pending = PendingClick(
        request_id=request.request_id,
        config_generation=config.generation,
        action="navigate",
        raw_point=request.point,
        payload=("left", False),
    )

    assert response.result is result
    assert response.raw_point == request.point
    assert response.purpose == "click"
    assert pending.payload == ("left", False)
    with pytest.raises(FrozenInstanceError):
        response.result = SnapResult(request.point, "free", 0.0)  # type: ignore[misc]
    with pytest.raises(FrozenInstanceError):
        pending.action = "route"  # type: ignore[misc]


def test_request_failures_are_frozen_and_fully_correlated() -> None:
    render = RenderFailure(11, 7, 13, "exact", "render failed")
    snap = SnapFailure(17, 7, "click", "snap failed")

    assert (
        render.request_id,
        render.config_generation,
        render.viewport_generation,
        render.purpose,
        render.message,
    ) == (11, 7, 13, "exact", "render failed")
    assert (
        snap.request_id,
        snap.config_generation,
        snap.purpose,
        snap.message,
    ) == (17, 7, "click", "snap failed")
    with pytest.raises(FrozenInstanceError):
        render.message = "changed"  # type: ignore[misc]
    with pytest.raises(FrozenInstanceError):
        snap.message = "changed"  # type: ignore[misc]
