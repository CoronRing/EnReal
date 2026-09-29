"""Shared fixtures: synthetic stage images, so the suite needs no binary test assets."""

from __future__ import annotations

import cv2
import numpy as np
import pytest


def encode_png(image: np.ndarray) -> bytes:
    """Encode a BGR image as PNG bytes."""
    ok, buf = cv2.imencode(".png", image)
    assert ok
    return buf.tobytes()


def draw_stage(width: int = 640, height: int = 360) -> np.ndarray:
    """A light background with solid dark blocks and a slope, like a simple level."""
    img = np.full((height, width, 3), 235, dtype=np.uint8)
    cv2.rectangle(img, (0, int(height * 0.85)), (width - 1, height - 1), (40, 60, 90), thickness=-1)
    cv2.rectangle(
        img, (int(width * 0.10), int(height * 0.55)), (int(width * 0.40), int(height * 0.62)), (30, 30, 30), -1
    )
    cv2.rectangle(
        img, (int(width * 0.55), int(height * 0.35)), (int(width * 0.90), int(height * 0.42)), (120, 40, 40), -1
    )
    slope = np.array(
        [
            [int(width * 0.45), int(height * 0.85)],
            [int(width * 0.70), int(height * 0.65)],
            [int(width * 0.70), int(height * 0.85)],
        ],
        dtype=np.int32,
    )
    cv2.fillPoly(img, [slope], (60, 120, 60))
    return img


@pytest.fixture
def stage_png() -> bytes:
    """A 640x360 synthetic stage, encoded as PNG."""
    return encode_png(draw_stage())
