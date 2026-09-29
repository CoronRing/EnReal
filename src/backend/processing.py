# app/processing.py
import base64
import logging
from dataclasses import dataclass
from typing import List, Tuple, Optional, Literal

import cv2
import numpy as np

from .geometry import (
    Segment,
    segment_length,
    snap_segments_to_grid,
    snap_points_to_grid,
)
from .boundary_extraction import extract_boundary
from .models import ProcessResponse, Skeleton, Bounds, Point, Meta

logger = logging.getLogger("enreal")


@dataclass
class PipelineParams:
    # Common
    max_dim: int = 1280
    grid_size: int = 8
    # Orientation thresholds removed (treat all segments uniformly as platforms)
    min_platform_len_pct: float = 0.22
    max_platforms: int = 200

    # “UI” stabilizers
    rectify_perspective: bool = True
    text_suppression: bool = True
    text_line_overlap_drop: float = 0.45

    # “Artwork” enhancements
    use_clahe: bool = True
    clahe_clip: float = 3.0
    clahe_tile: int = 8
    use_bilateral: bool = True
    bilateral_d: int = 7
    bilateral_sigmaColor: int = 50
    bilateral_sigmaSpace: int = 7

    # Detection/selection (Hough used only if you re-enable line segments)
    hough_min_len_ratio: float = 0.03
    hough_max_gap_ratio: float = 0.006
    hough_threshold_ratio: float = 0.02
    keep_top_ratio: float = 1.0  # keep all by default

    # New: Edge-map postprocessing to remove speckle and keep only “real” boundaries
    edge_close_ksize: int = 3                     # small closing to bridge tiny gaps
    min_edge_component_pixels_ratio: float = 0.0002  # remove edge CCs smaller than this fraction of image area
    min_edge_bbox_diag_ratio: float = 0.01        # also require a minimum bbox diagonal relative to image max(H,W)
    min_region_area_ratio: float = 0.004          # keep edges that separate regions at least this big
    thinning: bool = True                         # thin after filtering for cleaner curves


def _downscale(image: np.ndarray, max_dim: int) -> np.ndarray:
    h, w = image.shape[:2]
    scale = min(1.0, float(max_dim) / float(max(h, w)))
    if scale < 1.0:
        image = cv2.resize(image, (int(w * scale), int(h * scale)), interpolation=cv2.INTER_AREA)
    return image


def _encode_png_base64(img_bgr: np.ndarray) -> str:
    ok, buf = cv2.imencode(".png", img_bgr)
    if not ok:
        raise RuntimeError("PNG encode failed.")
    return "data:image/png;base64," + base64.b64encode(buf.tobytes()).decode("ascii")


def _auto_canny(gray: np.ndarray, sigma: float = 0.33) -> np.ndarray:
    v = float(np.median(gray))
    lower = int(max(0, (1.0 - sigma) * v))
    upper = int(min(255, (1.0 + sigma) * v))
    edges = cv2.Canny(gray, lower, upper)
    k = cv2.getStructuringElement(cv2.MORPH_RECT, (3, 3))
    edges = cv2.morphologyEx(edges, cv2.MORPH_CLOSE, k, iterations=1)
    return edges


def _detect_dominant_quad(gray: np.ndarray) -> Optional[np.ndarray]:
    h, w = gray.shape[:2]
    blur = cv2.GaussianBlur(gray, (5, 5), 0)
    edges = cv2.Canny(blur, 50, 150)
    k = cv2.getStructuringElement(cv2.MORPH_RECT, (5, 5))
    edges = cv2.dilate(edges, k, iterations=1)
    contours, _ = cv2.findContours(edges, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
    if not contours:
        return None
    img_area = w * h
    best = None
    best_area = 0
    for cnt in contours:
        area = cv2.contourArea(cnt)
        if area < 0.15 * img_area:
            continue
        peri = cv2.arcLength(cnt, True)
        approx = cv2.approxPolyDP(cnt, 0.02 * peri, True)
        if len(approx) == 4 and area > best_area:
            best = approx.reshape(-1, 2).astype(np.float32)
            best_area = area
    if best is None:
        return None
    s = best.sum(axis=1)
    diff = np.diff(best, axis=1).ravel()
    ordered = np.zeros((4, 2), dtype=np.float32)
    ordered[0] = best[np.argmin(s)]
    ordered[2] = best[np.argmax(s)]
    ordered[1] = best[np.argmin(diff)]
    ordered[3] = best[np.argmax(diff)]
    return ordered


def _warp_to_topdown(bgr: np.ndarray, quad: np.ndarray) -> Tuple[np.ndarray, np.ndarray, np.ndarray]:
    (tl, tr, br, bl) = quad
    def dist(a, b): return float(np.hypot(*(a - b)))
    w_top, w_bottom = dist(tl, tr), dist(bl, br)
    h_left, h_right = dist(tl, bl), dist(tr, br)
    width = int(round(max(w_top, w_bottom)))
    height = int(round(max(h_left, h_right)))
    width = max(32, width)
    height = max(32, height)
    dst = np.array([[0, 0], [width - 1, 0], [width - 1, height - 1], [0, height - 1]], dtype=np.float32)
    H = cv2.getPerspectiveTransform(quad, dst)
    H_inv = cv2.getPerspectiveTransform(dst, quad)
    warped = cv2.warpPerspective(bgr, H, (width, height), flags=cv2.INTER_LINEAR)
    return warped, H, H_inv


def _apply_H(pt: Tuple[float, float], H: np.ndarray) -> Tuple[float, float]:
    x, y = pt
    vec = np.array([x, y, 1.0], dtype=np.float64)
    out = H.dot(vec)
    out /= out[2] + 1e-12
    return float(out[0]), float(out[1])


def _map_segments(segments: List[Segment], H: np.ndarray) -> List[Segment]:
    out: List[Segment] = []
    for s in segments:
        x1, y1 = _apply_H((s.x1, s.y1), H)
        x2, y2 = _apply_H((s.x2, s.y2), H)
        out.append(Segment(int(round(x1)), int(round(y1)), int(round(x2)), int(round(y2))))
    return out


def _hough_segments_from_edge_map(edge_map: np.ndarray, h: int, w: int,
                                  min_len_ratio: float, max_gap_ratio: float, thr_ratio: float) -> List[Segment]:
    e_u8 = (edge_map.astype(np.uint8) * 255)
    diag = float(np.hypot(h, w))
    min_len = max(10, int(min_len_ratio * max(h, w)))
    max_gap = max(6, int(max_gap_ratio * max(h, w)))
    thr = max(35, int(thr_ratio * diag))
    lines = cv2.HoughLinesP(
        e_u8,
        rho=1,
        theta=np.pi / 180.0,
        threshold=thr,
        minLineLength=min_len,
        maxLineGap=max_gap,
    )
    segs: List[Segment] = []
    if lines is not None:
        for line in lines:
            x1, y1, x2, y2 = map(int, line[0])
            segs.append(Segment(x1, y1, x2, y2))
    return segs


def _enhance_for_art(gray: np.ndarray, params: PipelineParams) -> np.ndarray:
    lab = cv2.cvtColor(cv2.cvtColor(gray, cv2.COLOR_GRAY2BGR), cv2.COLOR_BGR2LAB)
    L, A, B = cv2.split(lab)
    clahe = cv2.createCLAHE(clipLimit=params.clahe_clip, tileGridSize=(params.clahe_tile, params.clahe_tile))
    L2 = clahe.apply(L)
    lab2 = cv2.merge([L2, A, B])
    bgr2 = cv2.cvtColor(lab2, cv2.COLOR_LAB2BGR)
    gray2 = cv2.cvtColor(bgr2, cv2.COLOR_BGR2GRAY)
    if params.use_bilateral:
        gray2 = cv2.bilateralFilter(gray2, d=params.bilateral_d,
                                    sigmaColor=params.bilateral_sigmaColor,
                                    sigmaSpace=params.bilateral_sigmaSpace)
    return gray2


def _gradient_map(gray: np.ndarray) -> np.ndarray:
    gx = cv2.Sobel(gray, cv2.CV_32F, 1, 0, ksize=3)
    gy = cv2.Sobel(gray, cv2.CV_32F, 0, 1, ksize=3)
    mag = cv2.magnitude(gx, gy)
    return mag


def _segment_mean_grad(seg: Segment, grad: np.ndarray, step: int = 3) -> float:
    n = max(1, int(segment_length(seg) // step))
    xs = np.linspace(seg.x1, seg.x2, n).astype(np.int32)
    ys = np.linspace(seg.y1, seg.y2, n).astype(np.int32)
    H, W = grad.shape
    vals = []
    for x, y in zip(xs, ys):
        if 0 <= x < W and 0 <= y < H:
            vals.append(float(grad[y, x]))
    return float(np.mean(vals)) if vals else 0.0


def _segment_edge_coverage(seg: Segment, edges: np.ndarray, step: int = 2) -> float:
    n = max(1, int(segment_length(seg) // step))
    xs = np.linspace(seg.x1, seg.x2, n).astype(np.int32)
    ys = np.linspace(seg.y1, seg.y2, n).astype(np.int32)
    H, W = edges.shape
    hits = 0
    total = 0
    for x, y in zip(xs, ys):
        if 0 <= x < W and 0 <= y < H:
            total += 1
            if edges[y, x] > 0:
                hits += 1
    return (hits / float(total)) if total > 0 else 0.0


def _text_mask(gray: np.ndarray) -> np.ndarray:
    h, w = gray.shape[:2]
    area = w * h
    min_area = int(0.00002 * area)
    max_area = int(0.01 * area)
    try:
        mser = cv2.MSER_create(_delta=3, _min_area=min_area, _max_area=max_area)
    except TypeError:
        mser = cv2.MSER_create(3, min_area, max_area)
    regions, _ = mser.detectRegions(gray)
    mask = np.zeros_like(gray, dtype=np.uint8)
    for r in regions:
        r = np.array(r, dtype=np.int32).reshape((-1, 1, 2))
        cv2.fillPoly(mask, [r], 255)
    k = cv2.getStructuringElement(cv2.MORPH_RECT, (3, 3))
    mask = cv2.dilate(mask, k, iterations=2)
    return mask


def _draw_overlay(base_bgr: np.ndarray, segments: List[Segment], color=(0, 0, 255), thickness=2) -> np.ndarray:
    overlay = base_bgr.copy()
    for s in segments:
        cv2.line(overlay, (int(s.x1), int(s.y1)), (int(s.x2), int(s.y2)), color, thickness, lineType=cv2.LINE_AA)
    return overlay


# --------------------- New: Edge-map postprocessing ---------------------

def _morph_close(edge_map: np.ndarray, ksize: int) -> np.ndarray:
    ksize = max(1, int(ksize))
    k = cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (ksize, ksize))
    closed = cv2.morphologyEx(edge_map.astype(np.uint8) * 255, cv2.MORPH_CLOSE, k, iterations=1)
    return (closed > 0)


def _remove_small_edge_components(edge_map: np.ndarray, H: int, W: int,
                                  min_pixels_ratio: float, min_bbox_diag_ratio: float) -> np.ndarray:
    """
    Remove connected components in the edge map that are too small by pixel count
    and by bounding-box diagonal length.
    """
    if not edge_map.any():
        return edge_map
    min_pixels = max(8, int(min_pixels_ratio * H * W))
    min_diag = max(4, int(min_bbox_diag_ratio * max(H, W)))

    num, labels, stats, _ = cv2.connectedComponentsWithStats(edge_map.astype(np.uint8), connectivity=8)
    keep = np.zeros(num, dtype=bool)
    keep[0] = False  # background
    for i in range(1, num):
        area = int(stats[i, cv2.CC_STAT_AREA])
        w = int(stats[i, cv2.CC_STAT_WIDTH])
        h = int(stats[i, cv2.CC_STAT_HEIGHT])
        diag = int(np.hypot(w, h))
        keep[i] = (area >= min_pixels) and (diag >= min_diag)
    cleaned = keep[labels]
    return cleaned


def _keep_edges_between_large_regions(edge_map: np.ndarray, min_region_area_ratio: float) -> np.ndarray:
    """
    Keep only edge pixels that separate at least two different background regions whose
    connected-component areas exceed a minimum threshold. This removes edges that bound tiny specks.
    """
    H, W = edge_map.shape
    if not edge_map.any():
        return edge_map
    min_area = max(64, int(min_region_area_ratio * H * W))

    # Label background regions (non-edge)
    bg = (~edge_map).astype(np.uint8)
    num, labels, stats, _ = cv2.connectedComponentsWithStats(bg, connectivity=4)
    areas = stats[:, cv2.CC_STAT_AREA].astype(np.int64)  # length = num

    # Neighbor labels around each pixel (no wrap-around)
    L = labels
    up = np.vstack([L[0:1, :], L[:-1, :]])
    down = np.vstack([L[1:, :], L[-1:, :]])
    left = np.hstack([L[:, 0:1], L[:, :-1]])
    right = np.hstack([L[:, 1:], L[:, -1:]])

    neigh = np.stack([up, down, left, right], axis=0)  # (4,H,W)
    # Mark neighbors that are "large" regions; others -> -1
    large_mask = areas[neigh] >= min_area
    neigh_filtered = neigh.copy()
    neigh_filtered[~large_mask] = -1
    # Count unique large labels among 4-neighborhood
    srt = np.sort(neigh_filtered, axis=0)
    first_valid = (srt[0] != -1).astype(np.int32)
    transitions = ((srt[1:] != srt[:-1]) & (srt[1:] != -1)).astype(np.int32)
    uniq_count = first_valid + transitions.sum(axis=0)

    keep_edges = edge_map & (uniq_count >= 2)
    return keep_edges


def _thin_edges(edge_map: np.ndarray) -> np.ndarray:
    """
    Optional thinning for prettier curves. Uses ximgproc.thinning if available,
    otherwise returns the input.
    """
    try:
        from cv2.ximgproc import thinning  # type: ignore[attr-defined]
        e_u8 = (edge_map.astype(np.uint8) * 255)
        th = thinning(e_u8, thinningType=1)  # Guo-Hall
        return th > 0
    except Exception:
        return edge_map


def _postprocess_edge_map(edge_map: np.ndarray, params: PipelineParams, H: int, W: int) -> np.ndarray:
    """
    Apply gap closing, small-component removal, region-adjacency filtering, and thinning.
    """
    if edge_map is None or not edge_map.any():
        return edge_map

    # 1) Close tiny gaps for better connectivity
    if params.edge_close_ksize > 1:
        edge_map = _morph_close(edge_map, params.edge_close_ksize)

    # 2) Remove tiny edge connected components (noise specks)
    edge_map = _remove_small_edge_components(
        edge_map, H, W,
        min_pixels_ratio=params.min_edge_component_pixels_ratio,
        min_bbox_diag_ratio=params.min_edge_bbox_diag_ratio,
    )

    # 3) Keep only edges that separate sufficiently large regions (surface-aware)
    edge_map = _keep_edges_between_large_regions(edge_map, params.min_region_area_ratio)

    # 4) Optional thinning for cleaner display
    if params.thinning:
        edge_map = _thin_edges(edge_map)

    return edge_map


def generate_skeleton(
    image_bytes: bytes,
    # public tunables
    max_dim: int = 1280,
    grid_size: int = 8,
    min_platform_len_pct: float = 0.22,
    max_platforms: int = 200,
    # scene = "ui" or "art"
    scene: Literal["ui", "art"] = "ui",
    rectify_perspective: bool = True,
    text_suppression: bool = True,
) -> ProcessResponse:
    """
    Generates skeleton lines with two scene modes:
      - ui: optimized for documents/web screenshots (rectification, text suppression)
      - art: optimized for scenes/concept art (contrast enhancement, texture suppression)
    """
    params = PipelineParams(
        max_dim=max_dim,
        grid_size=grid_size,
        min_platform_len_pct=min_platform_len_pct,
        max_platforms=max_platforms,
    )
    if scene == "ui":
        params.rectify_perspective = rectify_perspective
        params.text_suppression = text_suppression
        params.use_clahe = False
        params.use_bilateral = False
        params.grad_quantile_keep = 0.5
        # For UI, boundaries are often large blocks; be stricter
        params.min_edge_component_pixels_ratio = 0.0003
        params.min_region_area_ratio = 0.006
    else:
        params.rectify_perspective = False
        params.text_suppression = False
        params.use_clahe = True
        params.use_bilateral = True
        params.grad_quantile_keep = 0.55
        # For art, allow slightly smaller regions
        params.min_edge_component_pixels_ratio = 0.0002
        params.min_region_area_ratio = 0.004

    # Decode
    img_array = np.frombuffer(image_bytes, np.uint8)
    bgr = cv2.imdecode(img_array, cv2.IMREAD_COLOR)
    if bgr is None:
        raise RuntimeError("Unsupported or corrupted image.")
    bgr = _downscale(bgr, params.max_dim)
    H_img, W_img = bgr.shape[:2]

    # Optional rectification (ui mode)
    work_bgr = bgr
    H_inv = np.eye(3, dtype=np.float32)
    if params.rectify_perspective:
        quad = _detect_dominant_quad(cv2.cvtColor(bgr, cv2.COLOR_BGR2GRAY))
        if quad is not None:
            work_bgr, _H, H_inv = _warp_to_topdown(bgr, quad)
            logger.info("Rectified to %dx%d", work_bgr.shape[1], work_bgr.shape[0])

    # Advanced edge detection (multiscale, color-aware)
    H_work, W_work = work_bgr.shape[:2]
    be_result = extract_boundary(
        work_bgr,
        mode="edges",
        edge_mode="all",
        hough=True,              # Hough reinforcement still helps connect straight dividers
        return_debug=False,
        log_level=logging.INFO,
    )
    edge_map = be_result.edge_map if be_result.edge_map is not None else None

    # New: postprocess the edge map to remove speckle and keep only meaningful boundaries
    if edge_map is not None and edge_map.any():
        edge_map = _postprocess_edge_map(edge_map, params, H_work, W_work)
        # Map back if rectified
        if params.rectify_perspective:
            # Warp boolean mask back with nearest-neighbor
            edge_u8 = (edge_map.astype(np.uint8) * 255)
            edge_back = cv2.warpPerspective(edge_u8, H_inv, (W_img, H_img), flags=cv2.INTER_NEAREST)
            edge_map = (edge_back > 0)
        else:
            # Ensure size matches original
            if (H_work, W_work) != (H_img, W_img):
                edge_map = cv2.resize(edge_map.astype(np.uint8), (W_img, H_img), interpolation=cv2.INTER_NEAREST) > 0

    # Build pixel-level polylines (curves) from cleaned edge map
    platforms: List[Segment] = []
    barriers: List[Segment] = []
    ramps: List[Segment] = []

    curves_points: List[List[Tuple[int, int]]] = []
    if edge_map is not None and edge_map.any():
        edge_u8 = (edge_map.astype(np.uint8) * 255)
        contours, _ = cv2.findContours(edge_u8, cv2.RETR_LIST, cv2.CHAIN_APPROX_NONE)
        # Filter out tiny contours again by arc length to be safe
        min_len_pixels = max(30, int(params.min_edge_bbox_diag_ratio * max(H_img, W_img)))
        for cnt in contours:
            if cv2.arcLength(cnt, closed=False) < min_len_pixels:
                continue
            pts = cnt.reshape(-1, 2)
            pts_list = [(int(p[0]), int(p[1])) for p in pts]
            pts_list = snap_points_to_grid(pts_list, params.grid_size)
            clamped = []
            for x, y in pts_list:
                clamped.append((max(0, min(W_img - 1, x)), max(0, min(H_img - 1, y))))
            curves_points.append(clamped)

    # Map back line segments if you add them later
    if params.rectify_perspective:
        platforms = _map_segments(platforms, H_inv)
        barriers = _map_segments(barriers, H_inv)
        ramps = _map_segments(ramps, H_inv)

    def clamp(s: Segment) -> Segment:
        return Segment(
            max(0, min(W_img - 1, s.x1)),
            max(0, min(H_img - 1, s.y1)),
            max(0, min(W_img - 1, s.x2)),
            max(0, min(H_img - 1, s.y2)),
        )

    platforms = [clamp(s) for s in snap_segments_to_grid(platforms, params.grid_size)]

    bounds = Bounds(x=0, y=0, width=W_img, height=H_img)
    def choose_spawn(plats: List[Segment], w: int, h: int) -> Tuple[int, int]:
        if plats:
            lowest = max(plats, key=lambda s: (s.y1 + s.y2) * 0.5)
            xm = int(round((lowest.x1 + lowest.x2) * 0.5))
            y = int(round((lowest.y1 + lowest.y2) * 0.5)) - 1
            return xm, max(0, min(h - 1, y))
        return w // 2, int(h * 0.9)
    _sx, _sy = choose_spawn(platforms, W_img, H_img)
    spawn = Point(x=_sx, y=_sy)

    meta = Meta(
        gridSize=params.grid_size,
        sourceImageSize=(W_img, H_img),
        params={
            "scene": scene,
            "rectify_perspective": 1 if params.rectify_perspective else 0,
            "text_suppression": 1 if params.text_suppression else 0,
            "edge_extractor": "boundary_extraction+post",
            "min_platform_len_pct": params.min_platform_len_pct,
            "min_region_area_ratio": params.min_region_area_ratio,
            "min_edge_component_pixels_ratio": params.min_edge_component_pixels_ratio,
        },
    )

    overlay = bgr.copy()
    if edge_map is not None and edge_map.any():
        overlay[edge_map] = (0, 0, 255)
    overlay_b64 = _encode_png_base64(overlay)

    from .models import PolylineModel
    skeleton = Skeleton(
        bounds=bounds,
        spawn=spawn,
        platforms=[],
        barriers=[],
        ramps=[],
        curves=[PolylineModel(points=[Point(x=p[0], y=p[1]) for p in poly]) for poly in curves_points],
        meta=meta,
    )
    # NOTE: models.ProcessResponse currently expects skeleton as a plain Dict, not a Skeleton model.
    # Convert to dict to satisfy Pydantic validation.
    return ProcessResponse(skeleton=skeleton.model_dump(), overlay_image_b64=overlay_b64)