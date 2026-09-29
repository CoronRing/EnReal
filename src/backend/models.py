"""Pydantic models used by the backend."""

from __future__ import annotations

from typing import Any, Dict, List, Optional, Tuple

from pydantic import BaseModel, Field


class SegmentModel(BaseModel):
    x1: int = Field(..., description="Segment start x")
    y1: int = Field(..., description="Segment start y")
    x2: int = Field(..., description="Segment end x")
    y2: int = Field(..., description="Segment end y")


class Bounds(BaseModel):
    x: int
    y: int
    width: int
    height: int


class Point(BaseModel):
    x: int
    y: int


class PolylineModel(BaseModel):
    # A polyline represented by a sequence of points
    points: List[Point] = Field(..., description="Ordered points forming a curve/polyline")


class Meta(BaseModel):
    gridSize: int
    sourceImageSize: Tuple[int, int]
    params: Dict[str, Any]


class Skeleton(BaseModel):
    bounds: Bounds
    spawn: Point
    platforms: List[SegmentModel]
    barriers: List[SegmentModel]
    ramps: List[SegmentModel]  # slanted straight lines
    curves: List[PolylineModel]  # polylines for curved edges
    meta: Meta


class ProcessResponse(BaseModel):
    """
    Response model for /api/process.

    Fields:
    - skeleton: Dict[str, Any]
        The gameplay skeleton detected from the uploaded image.
        Structure:
          {
            "bounds": { "x": int, "y": int, "width": int, "height": int },
            "spawn": { "x": int, "y": int },
            "platforms": [ { "x1": int, "y1": int, "x2": int, "y2": int }, ... ],
            "barriers":  [ ... same-edge-structure ... ],
            "ramps":     [ ... same-edge-structure ... ],
            "curves":    [],
            "meta": {
              "gridSize": int,
              "sourceImageSize": [int, int],
              "params": Dict[str, int|float|str|bool]
            }
          }

    - overlay_image_b64: str
        Data URL representing the debug overlay image drawn with detected edges. Suitable for <img src="...">.

    - pack_id: Optional[str]
        The generated Visual Novel pack identifier (slug of characters or a hash). When None, VN generation failed and the
        game should start directly.

    - pack_base_url: Optional[str]
        Base URL to load the VN pack assets. If provided, manifest is at f"{pack_base_url}/manifest.json".

    - manifest_url: Optional[str]
        Direct URL for the manifest.json if you prefer to use this instead of composing pack_base_url.

    Notes:
    - pack_id / pack_base_url may be omitted when story/pack generation fails; frontend should handle this gracefully.
    """
    skeleton: Dict[str, Any] = Field(...)
    overlay_image_b64: str = Field(...)
    pack_id: Optional[str] = Field(default=None)
    pack_base_url: Optional[str] = Field(default=None)
    manifest_url: Optional[str] = Field(default=None)