"""Data contract returned by the engine.

The JSON form of :class:`StageResult` (``StageResult.model_dump_json()``) is what the
web game consumes, whether it was produced by the server or by the in-browser engine.
Field names are camelCase where the game reads them directly, so they stay as-is.
"""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, Field


class SegmentModel(BaseModel):
    """A straight segment in image pixel coordinates."""

    x1: int = Field(..., description="Segment start x")
    y1: int = Field(..., description="Segment start y")
    x2: int = Field(..., description="Segment end x")
    y2: int = Field(..., description="Segment end y")


class Bounds(BaseModel):
    """Playable area. Always the full (downscaled) image rectangle."""

    x: int
    y: int
    width: int
    height: int


class Point(BaseModel):
    """A point in image pixel coordinates, origin top-left, y pointing down."""

    x: int
    y: int


class PolylineModel(BaseModel):
    """A curve traced from the edge map, as an ordered list of points."""

    points: list[Point] = Field(..., description="Ordered points forming a curve/polyline")


class Meta(BaseModel):
    """Provenance: the image size and the parameters the pipeline ran with."""

    gridSize: int
    sourceImageSize: tuple[int, int]
    params: dict[str, Any]


class Skeleton(BaseModel):
    """Gameplay geometry detected in the image.

    ``curves`` carries the detected terrain. ``platforms``, ``barriers`` and ``ramps``
    are part of the contract but the current pipeline leaves them empty; see
    ``docs/design.md``.
    """

    bounds: Bounds
    spawn: Point
    platforms: list[SegmentModel]
    barriers: list[SegmentModel]
    ramps: list[SegmentModel]
    curves: list[PolylineModel]
    meta: Meta


class StageResult(BaseModel):
    """Everything the game needs to build a stage from one image.

    Attributes:
        skeleton: Detected geometry.
        overlay_image_b64: PNG data URL of the downscaled image with every detected
            edge painted pure red (BGR ``(0, 0, 255)``). The game uses these red pixels
            as its collision terrain, so this image is part of the contract, not only
            a debug view.
    """

    skeleton: Skeleton
    overlay_image_b64: str = Field(..., description="data:image/png;base64,... overlay with edges in red")
