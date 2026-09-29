"""Engine behavior and the JSON contract the web game depends on."""

from __future__ import annotations

import base64
import json

import cv2
import numpy as np
import pytest

from enreal import StageResult, generate_skeleton
from tests.conftest import draw_stage, encode_png

SKELETON_KEYS = {"bounds", "spawn", "platforms", "barriers", "ramps", "curves", "meta"}


def _decode_overlay(result: StageResult) -> np.ndarray:
    prefix = "data:image/png;base64,"
    assert result.overlay_image_b64.startswith(prefix)
    raw = base64.b64decode(result.overlay_image_b64[len(prefix) :])
    img = cv2.imdecode(np.frombuffer(raw, np.uint8), cv2.IMREAD_COLOR)
    assert img is not None
    return img


@pytest.mark.parametrize("scene", ["art", "ui"])
def test_detects_terrain(stage_png: bytes, scene: str) -> None:
    result = generate_skeleton(stage_png, scene=scene)  # type: ignore[arg-type]

    assert isinstance(result, StageResult)
    assert (result.skeleton.bounds.width, result.skeleton.bounds.height) == (640, 360)
    assert result.skeleton.curves, "the block edges should produce at least one curve"
    assert result.skeleton.meta.params["scene"] == scene


def test_overlay_paints_edges_pure_red(stage_png: bytes) -> None:
    # The game treats pure red pixels as solid ground, so they must be present and exact.
    result = generate_skeleton(stage_png, scene="art")
    overlay = _decode_overlay(result)

    assert overlay.shape[:2] == (360, 640)
    red = np.all(overlay == (0, 0, 255), axis=-1)
    assert red.sum() > 500


def test_json_contract_is_stable(stage_png: bytes) -> None:
    payload = json.loads(generate_skeleton(stage_png, scene="art").model_dump_json())

    assert set(payload) == {"skeleton", "overlay_image_b64"}
    skeleton = payload["skeleton"]
    assert set(skeleton) == SKELETON_KEYS
    assert set(skeleton["bounds"]) == {"x", "y", "width", "height"}
    assert set(skeleton["spawn"]) == {"x", "y"}
    assert set(skeleton["meta"]) == {"gridSize", "sourceImageSize", "params"}
    assert skeleton["meta"]["sourceImageSize"] == [640, 360]
    first = skeleton["curves"][0]
    assert set(first) == {"points"}
    assert set(first["points"][0]) == {"x", "y"}


def test_is_deterministic(stage_png: bytes) -> None:
    a = generate_skeleton(stage_png, scene="art").model_dump_json()
    b = generate_skeleton(stage_png, scene="art").model_dump_json()
    assert a == b


def test_downscales_to_max_dim() -> None:
    big = encode_png(draw_stage(1600, 900))
    result = generate_skeleton(big, max_dim=800, scene="art")

    assert (result.skeleton.bounds.width, result.skeleton.bounds.height) == (800, 450)
    for curve in result.skeleton.curves:
        for p in curve.points:
            assert 0 <= p.x < 800 and 0 <= p.y < 450


def test_points_snap_to_grid(stage_png: bytes) -> None:
    grid = 8
    result = generate_skeleton(stage_png, grid_size=grid, scene="art")
    w, h = result.skeleton.bounds.width, result.skeleton.bounds.height

    for curve in result.skeleton.curves:
        for p in curve.points:
            # Snapped to the grid, except where clamping pulled a point onto the last pixel.
            assert p.x % grid == 0 or p.x == w - 1
            assert p.y % grid == 0 or p.y == h - 1


def test_spawn_is_inside_bounds(stage_png: bytes) -> None:
    result = generate_skeleton(stage_png, scene="art")
    spawn, bounds = result.skeleton.spawn, result.skeleton.bounds
    assert 0 <= spawn.x < bounds.width and 0 <= spawn.y < bounds.height


def test_rejects_undecodable_bytes() -> None:
    with pytest.raises(ValueError, match="decode"):
        generate_skeleton(b"definitely not an image")


@pytest.mark.parametrize(
    "kwargs",
    [
        {"max_dim": 0},
        {"grid_size": 0},
        {"min_platform_len_pct": 1.5},
        {"max_platforms": -1},
        {"scene": "photo"},
    ],
)
def test_rejects_bad_parameters(stage_png: bytes, kwargs: dict[str, object]) -> None:
    with pytest.raises(ValueError):
        generate_skeleton(stage_png, **kwargs)  # type: ignore[arg-type]


def test_extractor_failure_raises_instead_of_returning_an_empty_stage(
    stage_png: bytes, monkeypatch: pytest.MonkeyPatch
) -> None:
    from enreal import boundary_extraction, processing

    def broken(*args: object, **kwargs: object) -> boundary_extraction.BoundaryResult:
        return boundary_extraction.BoundaryResult(
            status="error",
            boundary_mask=None,
            object_mask=None,
            contours=[],
            polygon=None,
            hierarchy=None,
            edge_map=None,
            debug={},
            errors=["Edge pipeline error: simulated"],
            meta={},
        )

    monkeypatch.setattr(processing, "extract_boundary", broken)
    with pytest.raises(RuntimeError, match="simulated"):
        generate_skeleton(stage_png)
