# app/geometry.py
from dataclasses import dataclass
from typing import List, Literal, Tuple, Dict
import math

import numpy as np


@dataclass
class Segment:
    x1: int
    y1: int
    x2: int
    y2: int


def segment_length(s: Segment) -> float:
    return float(np.hypot(s.x2 - s.x1, s.y2 - s.y1))


def calculate_angle_deg(s: Segment) -> float:
    # Angle relative to x-axis, in degrees [0, 180)
    angle = math.degrees(math.atan2((s.y2 - s.y1), (s.x2 - s.x1)))
    angle = abs(angle)
    if angle < 0:
        angle += 180.0
    return angle


def _normalize_segment(s: Segment) -> Segment:
    # Ensure for horizontals x1<=x2 and for verticals y1<=y2
    if abs(s.y2 - s.y1) <= abs(s.x2 - s.x1):
        # treat as horizontal
        if s.x2 < s.x1:
            return Segment(s.x2, s.y2, s.x1, s.y1)
        return s
    else:
        # treat as vertical
        if s.y2 < s.y1:
            return Segment(s.x2, s.y2, s.x1, s.y1)
        return s


def snap_segments_to_grid(segments: List[Segment], grid: int) -> List[Segment]:
    if grid <= 1:
        return segments
    snapped: List[Segment] = []
    for s in segments:
        snapped.append(
            Segment(
                int(round(s.x1 / grid) * grid),
                int(round(s.y1 / grid) * grid),
                int(round(s.x2 / grid) * grid),
                int(round(s.y2 / grid) * grid),
            )
        )
    return snapped


def snap_points_to_grid(points: List[Tuple[int, int]], grid: int) -> List[Tuple[int, int]]:
    if grid <= 1:
        return points
    return [(int(round(x / grid) * grid), int(round(y / grid) * grid)) for (x, y) in points]


def merge_segments_axis(
    segments: List[Segment],
    axis: Literal["x", "y"],
    snap: int,
    join_gap: int,
    min_len: int,
) -> List[Segment]:
    """
    Merge colinear segments along a given axis:
      - axis='y' for horizontals: cluster by y and merge overlapping x ranges
      - axis='x' for verticals: cluster by x and merge overlapping y ranges
    """
    if not segments:
        return []

    # Normalize direction and snap to grid along both axes
    segs = [snap_segments_to_grid([_normalize_segment(s)], grid=snap)[0] for s in segments]

    # Cluster by axis coordinate using a tolerance equal to snap
    buckets: Dict[int, List[Segment]] = {}
    for s in segs:
        if axis == "y":
            y_val = int(round((s.y1 + s.y2) * 0.5))
            key = int(round(y_val / snap) * snap)
        else:
            x_val = int(round((s.x1 + s.x2) * 0.5))
            key = int(round(x_val / snap) * snap)
        buckets.setdefault(key, []).append(s)

    merged: List[Segment] = []
    for key, group in buckets.items():
        if axis == "y":
            # Merge by x ranges on same y bucket
            ranges = []
            for s in group:
                x1, x2 = (s.x1, s.x2) if s.x1 <= s.x2 else (s.x2, s.x1)
                ranges.append([x1, x2])
            ranges.sort(key=lambda r: r[0])

            merged_ranges = []
            cur = ranges[0]
            for r in ranges[1:]:
                if r[0] <= cur[1] + join_gap:
                    cur[1] = max(cur[1], r[1])
                else:
                    merged_ranges.append(cur)
                    cur = r
            merged_ranges.append(cur)

            for r in merged_ranges:
                if (r[1] - r[0]) >= min_len:
                    merged.append(Segment(r[0], key, r[1], key))

        else:
            # axis == "x": Merge by y ranges on same x bucket
            ranges = []
            for s in group:
                y1, y2 = (s.y1, s.y2) if s.y1 <= s.y2 else (s.y2, s.y1)
                ranges.append([y1, y2])
            ranges.sort(key=lambda r: r[0])

            merged_ranges = []
            cur = ranges[0]
            for r in ranges[1:]:
                if r[0] <= cur[1] + join_gap:
                    cur[1] = max(cur[1], r[1])
                else:
                    merged_ranges.append(cur)
                    cur = r
            merged_ranges.append(cur)

            for r in merged_ranges:
                if (r[1] - r[0]) >= min_len:
                    merged.append(Segment(key, r[0], key, r[1]))

    return merged


# -------- Oblique (slanted) segment merging ----------

def _unit_from_angle_deg(angle_deg: float) -> Tuple[float, float]:
    theta = math.radians(angle_deg)
    return math.cos(theta), math.sin(theta)


def _perp_unit_from_angle_deg(angle_deg: float) -> Tuple[float, float]:
    ux, uy = _unit_from_angle_deg(angle_deg)
    # Rotate by +90 degrees to get normal
    return -uy, ux


def _dot(ax: float, ay: float, bx: float, by: float) -> float:
    return ax * bx + ay * by


def merge_oblique_segments(
    segments: List[Segment],
    angle_bucket_deg: float,
    angle_tol_deg: float,
    offset_tol_px: int,
    join_gap_px: int,
    min_len_px: int,
    grid: int,
) -> List[Segment]:
    """
    Merge non-axis-aligned segments by clustering on:
      - orientation (angle bucket)
      - perpendicular offset (Hesse normal form)
    Then merging intervals along the segment direction.

    Parameters:
      - angle_bucket_deg: quantization bucket for angles (e.g., 5 deg)
      - angle_tol_deg: tolerance within a bucket (e.g., 4 deg)
      - offset_tol_px: tolerance for the perpendicular offset to consider as same line
      - join_gap_px: gap allowed when merging along the direction
      - min_len_px: minimal final length
      - grid: snap endpoints to this grid

    Returns:
      A list of merged, snapped segments of arbitrary angle.
    """
    if not segments:
        return []

    # Prepare entries with angle and line coordinates
    entries = []
    for s in segments:
        x1, y1, x2, y2 = float(s.x1), float(s.y1), float(s.x2), float(s.y2)
        angle = calculate_angle_deg(s)
        # Normalize angle to [0, 180)
        angle = angle % 180.0
        # Skip near-horizontal/vertical here; caller should have filtered them
        entries.append((angle, (x1, y1, x2, y2)))

    # Cluster by angle bucket and offset
    buckets: Dict[Tuple[int, int, int], List[Tuple[float, float, float, float, float, float, float, float]]] = {}
    for angle, (x1, y1, x2, y2) in entries:
        # Determine bucket center
        bucket = int(round(angle / angle_bucket_deg) * angle_bucket_deg)
        # Use representative orientation within bucket
        ux, uy = _unit_from_angle_deg(bucket)
        nx, ny = _perp_unit_from_angle_deg(bucket)

        # Project endpoints onto dir and normal
        t1 = _dot(x1, y1, ux, uy)
        t2 = _dot(x2, y2, ux, uy)
        # Perpendicular offset (average of two points, since segment is short)
        o1 = _dot(x1, y1, nx, ny)
        o2 = _dot(x2, y2, nx, ny)
        off = 0.5 * (o1 + o2)

        # Quantize offset to group neighboring colinear lines
        off_key = int(round(off / float(max(1, offset_tol_px))))
        # Keep also the bucket to use for reconstruction
        idx = (int(bucket), off_key, 0)
        buckets.setdefault(idx, []).append((x1, y1, x2, y2, ux, uy, nx, ny))

    merged: List[Segment] = []
    for (bucket_angle, off_key, _), segs in buckets.items():
        ux, uy = _unit_from_angle_deg(bucket_angle)
        nx, ny = _perp_unit_from_angle_deg(bucket_angle)
        # Recover real offset value from key
        off = off_key * float(max(1, offset_tol_px))

        # For all segments in this bucket, get t-intervals along direction
        intervals: List[Tuple[float, float]] = []
        for (x1, y1, x2, y2, *_rest) in segs:
            t1 = _dot(x1, y1, ux, uy)
            t2 = _dot(x2, y2, ux, uy)
            a, b = (t1, t2) if t1 <= t2 else (t2, t1)
            intervals.append((a, b))
        intervals.sort(key=lambda ab: ab[0])

        # Merge intervals by join_gap_px
        merged_intervals: List[Tuple[float, float]] = []
        cur_a, cur_b = intervals[0]
        for (a, b) in intervals[1:]:
            if a <= cur_b + join_gap_px:
                cur_b = max(cur_b, b)
            else:
                merged_intervals.append((cur_a, cur_b))
                cur_a, cur_b = a, b
        merged_intervals.append((cur_a, cur_b))

        # Reconstruct segments from merged intervals
        for (a, b) in merged_intervals:
            if (b - a) < min_len_px:
                continue
            # Convert line coordinates back to 2D points: p = d*t + n*off
            x_start = ux * a + nx * off
            y_start = uy * a + ny * off
            x_end = ux * b + nx * off
            y_end = uy * b + ny * off
            seg = Segment(int(round(x_start)), int(round(y_start)), int(round(x_end)), int(round(y_end)))
            merged.append(seg)

    # Snap endpoints to grid
    merged = snap_segments_to_grid(merged, grid)
    return merged
