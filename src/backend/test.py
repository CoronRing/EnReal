#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""
boundary_extraction.py

Improved color- and scale-aware boundary/edge extraction.

What’s new vs. earlier version:
- Multichannel, multiscale edge detection (Lab channels, multiple blur scales)
- Optional Hough line reinforcement for long straight dividers
- Optional superpixel-based region boundary selection by color contrast (DeltaE)
- Unicode-safe image loader (np.fromfile + cv2.imdecode) for Windows paths

Dependencies:
- Python 3.8+
- numpy
- opencv-python
- Optional: scikit-image (for superpixel-based region boundaries). Install with:
    pip install scikit-image

Main entry point: extract_boundary

Result data structure (BoundaryResult):
    status: str
        "ok" on success, "partial" if some steps failed but edges exist, "error" otherwise.
    boundary_mask: Optional[np.ndarray]
        2D boolean (H, W). Primary filled-object boundary as a thin ring derived from
        the primary contour (if a primary contour was computed). None if not applicable.
    object_mask: Optional[np.ndarray]
        2D boolean (H, W). Filled primary object mask from the best external contour (if found).
    contours: List[np.ndarray]
        List of contours. Each is (N, 2) float32 in image coordinates. The first is the primary one.
    polygon: Optional[np.ndarray]
        Simplified polygon for the primary contour, (M, 2) float32. None if not available.
    hierarchy: Optional[np.ndarray]
        OpenCV hierarchy array from findContours (if used).
    edge_map: Optional[np.ndarray]
        2D boolean (H, W). Unified “all detected edges” after multiscale+multichannel detection,
        optional Hough reinforcement, and (optionally) superpixel-based filtering.
    debug: Dict[str, np.ndarray]
        Diagnostic images (uint8 unless noted):
        - "gray": preprocessed grayscale
        - "edges_gray": Canny edges on grayscale
        - "edges_lab": union of Canny edges on L,a,b channels
        - "edges_multiscale": union across multiple blur scales
        - "edges_hough": edge map after Hough reinforcement (if enabled)
        - "edges_superpixel": superpixel-based boundary map (if enabled and available)
        - "edge_overlay": BGR overlay of edge_map on original
        - "overlay": previous boundary overlay (if primary contour exists)
    errors: List[str]
        Any warnings/errors encountered.
    meta: Dict[str, Any]
        Parameters used and small stats (thresholds, scales, etc.).

CLI examples (Windows PowerShell):
    python .\boundary_extraction.py "C:\path\to\image.png" --mode edges --edge-mode all --hough --deltaE 12 --out-dir out --debug
"""

from __future__ import annotations

import argparse
import logging
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple, Union

import cv2
import numpy as np

try:
    from skimage.segmentation import slic, find_boundaries
    HAVE_SKIMAGE = True
except Exception:
    HAVE_SKIMAGE = False

# Configure logger
logger = logging.getLogger("boundary_extraction")
handler = logging.StreamHandler(stream=sys.stdout)
formatter = logging.Formatter("[%(levelname)s] %(asctime)s - %(message)s")
handler.setFormatter(formatter)
logger.addHandler(handler)
logger.setLevel(logging.INFO)


@dataclass
class BoundaryResult:
    status: str
    boundary_mask: Optional[np.ndarray]
    object_mask: Optional[np.ndarray]
    contours: List[np.ndarray]
    polygon: Optional[np.ndarray]
    hierarchy: Optional[np.ndarray]
    edge_map: Optional[np.ndarray]
    debug: Dict[str, np.ndarray]
    errors: List[str]
    meta: Dict[str, Any]


# -------------------- Utilities --------------------

def _read_image(source: Union[str, Path, np.ndarray]) -> Optional[np.ndarray]:
    """
    Load an image from a path or pass-through if ndarray.
    Returns BGR uint8 image or None on failure.

    Robust to:
    - Windows Unicode paths (fallback via np.fromfile + cv2.imdecode)
    - Grayscale inputs
    - Non-uint8 arrays (will be clipped and cast)
    """
    if isinstance(source, np.ndarray):
        if source.ndim == 2:
            return cv2.cvtColor(source, cv2.COLOR_GRAY2BGR)
        if source.ndim == 3:
            img = source
            if img.dtype != np.uint8:
                img = np.clip(img, 0, 255).astype(np.uint8)
            if img.shape[2] > 3:
                img = img[:, :, :3]
            return img
        logger.error("Unsupported ndarray shape for image: %s", getattr(source, "shape", None))
        return None

    p = Path(source)
    if not p.exists():
        logger.error("Input path does not exist: %s", str(p))
        return None

    img = cv2.imread(str(p), cv2.IMREAD_COLOR)
    if img is not None:
        return img

    # Unicode-safe/OneDrive-safe fallback
    try:
        data = np.fromfile(str(p), dtype=np.uint8)
        if data.size == 0:
            logger.error("File is empty or unreadable: %s", str(p))
            return None
        img = cv2.imdecode(data, cv2.IMREAD_COLOR)
        if img is None:
            logger.error("imdecode failed. The file may be corrupt: %s", str(p))
            return None
        return img
    except Exception as e:
        logger.exception("Failed to load image via Unicode-safe fallback: %s", e)
        return None


def _to_gray_enhanced(bgr: np.ndarray, clip_limit: float = 2.0, tile_grid: Tuple[int, int] = (8, 8)) -> np.ndarray:
    gray = cv2.cvtColor(bgr, cv2.COLOR_BGR2GRAY)
    clahe = cv2.createCLAHE(clipLimit=clip_limit, tileGridSize=tile_grid)
    return clahe.apply(gray)


def _auto_canny_thresholds(gray: np.ndarray, low_ratio: float = 0.33) -> Tuple[int, int]:
    v = float(np.median(gray))
    lower = int(max(0, (1.0 - low_ratio) * v))
    upper = int(min(255, (1.0 + low_ratio) * v))
    if lower == upper:
        upper = min(255, lower + 50)
    return lower, upper


def _approx_polygon(contour: np.ndarray, epsilon_ratio: float = 0.01) -> np.ndarray:
    peri = cv2.arcLength(contour, True)
    epsilon = max(1.0, epsilon_ratio * peri)
    approx = cv2.approxPolyDP(contour, epsilon, True)
    return approx.reshape(-1, 2).astype(np.float32)


def _largest_external_contour(contours: List[np.ndarray], hierarchy: Optional[np.ndarray], min_area: float) -> Optional[np.ndarray]:
    if not contours:
        return None
    best = None
    best_area = 0.0
    for i, c in enumerate(contours):
        area = float(cv2.contourArea(c))
        if area < min_area:
            continue
        if hierarchy is not None:
            h = hierarchy[0, i] if hierarchy.ndim == 3 and hierarchy.shape[0] == 1 else hierarchy[i]
            if h[3] != -1:  # has parent -> hole/internal
                continue
        if area > best_area:
            best = c
            best_area = area
    return best


def _make_mask_from_contour(shape: Tuple[int, int], contour: np.ndarray) -> np.ndarray:
    h, w = shape
    mask = np.zeros((h, w), dtype=np.uint8)
    c = contour.reshape(-1, 1, 2).astype(np.int32)
    cv2.fillPoly(mask, [c], 255)
    return mask > 0


def _boundary_from_mask(mask: np.ndarray, width: int = 3) -> np.ndarray:
    k = cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (max(1, width), max(1, width)))
    dil = cv2.dilate(mask.astype(np.uint8) * 255, k, iterations=1)
    ero = cv2.erode(mask.astype(np.uint8) * 255, k, iterations=1)
    grad = cv2.subtract(dil, ero)
    return grad > 0


# -------------------- Color- and scale-aware edges --------------------

def _edges_canny_gray(bgr: np.ndarray, blur_ksize: int, sigma_ratio: float) -> np.ndarray:
    gray = _to_gray_enhanced(bgr)
    if blur_ksize and blur_ksize > 1:
        k = blur_ksize if blur_ksize % 2 == 1 else blur_ksize + 1
        gray = cv2.GaussianBlur(gray, (k, k), 0)
    tl, th = _auto_canny_thresholds(gray, sigma_ratio)
    return cv2.Canny(gray, tl, th) > 0


def _edges_canny_lab(bgr: np.ndarray, blur_ksize: int, sigma_ratio: float) -> Tuple[np.ndarray, Dict[str, np.ndarray]]:
    lab = cv2.cvtColor(bgr, cv2.COLOR_BGR2LAB)
    edges_union = None
    debug = {}
    for idx, name in enumerate(["L", "a", "b"]):
        ch = lab[..., idx]
        if blur_ksize and blur_ksize > 1:
            k = blur_ksize if blur_ksize % 2 == 1 else blur_ksize + 1
            ch = cv2.GaussianBlur(ch, (k, k), 0)
        tl, th = _auto_canny_thresholds(ch, sigma_ratio)
        e = cv2.Canny(ch, tl, th) > 0
        edges_union = e if edges_union is None else (edges_union | e)
        debug[f"edges_lab_{name}"] = (e.astype(np.uint8) * 255)
    return edges_union, debug


def _edges_multiscale_lab(bgr: np.ndarray, scales: Tuple[int, ...], sigma_ratio: float) -> Tuple[np.ndarray, Dict[str, np.ndarray]]:
    """
    Run Canny on Lab channels at multiple blur scales and union the results.
    scales: tuple of odd kernel sizes (e.g., 3, 5, 9 or 5, 9, 13)
    """
    lab = cv2.cvtColor(bgr, cv2.COLOR_BGR2LAB)
    union = None
    debug = {}
    for k in scales:
        k = k if k % 2 == 1 else k + 1
        layer = None
        for idx in range(3):
            ch = lab[..., idx]
            ch_blur = cv2.GaussianBlur(ch, (k, k), 0)
            tl, th = _auto_canny_thresholds(ch_blur, sigma_ratio)
            e = cv2.Canny(ch_blur, tl, th) > 0
            layer = e if layer is None else (layer | e)
        union = layer if union is None else (union | layer)
        debug[f"edges_multiscale_k{k}"] = (layer.astype(np.uint8) * 255)
    return union, debug


def _reinforce_with_hough(edge_map: np.ndarray, min_line_len: int = 60, max_line_gap: int = 10, thickness: int = 1) -> Tuple[np.ndarray, np.ndarray]:
    """
    Strengthen long straight edges using probabilistic Hough transform.
    """
    h, w = edge_map.shape
    e_u8 = (edge_map.astype(np.uint8) * 255)
    lines = cv2.HoughLinesP(e_u8, rho=1, theta=np.pi/180, threshold=80,
                            minLineLength=min_line_len, maxLineGap=max_line_gap)
    out = e_u8.copy()
    if lines is not None:
        for x1, y1, x2, y2 in lines[:, 0]:
            cv2.line(out, (x1, y1), (x2, y2), 255, thickness=thickness, lineType=cv2.LINE_AA)
    return out > 0, out


def _deltaE94(lab1: np.ndarray, lab2: np.ndarray) -> np.ndarray:
    # Fast deltaE approximation suitable for thresholding; for strict CIEDE2000 use skimage.color.deltaE_ciede2000
    dl = lab1[..., 0] - lab2[..., 0]
    da = lab1[..., 1] - lab2[..., 1]
    db = lab1[..., 2] - lab2[..., 2]
    return np.sqrt(dl*dl + da*da + db*db)


def _superpixel_strong_boundaries(bgr: np.ndarray, n_segments: int, compactness: float, deltaE_thresh: float) -> Optional[np.ndarray]:
    """
    Use superpixels to get region boundaries with large color contrast.
    Returns boolean edge map or None if skimage is unavailable.
    """
    if not HAVE_SKIMAGE:
        logger.warning("scikit-image not available; skipping superpixel-based boundaries.")
        return None
    lab = cv2.cvtColor(bgr, cv2.COLOR_BGR2LAB)
    # skimage expects RGB float in [0,1]
    rgb_float = cv2.cvtColor(bgr, cv2.COLOR_BGR2RGB).astype(np.float32) / 255.0
    labels = slic(rgb_float, n_segments=n_segments, compactness=compactness, start_label=0)
    boundaries = find_boundaries(labels, mode="thick")  # boolean

    # Compute mean Lab per superpixel
    label_ids = np.unique(labels)
    means = np.zeros((label_ids.max() + 1, 3), dtype=np.float32)
    for lbl in label_ids:
        mask = (labels == lbl)
        if mask.any():
            means[lbl] = lab[mask].mean(axis=0)

    # For each boundary pixel, look at 4-neighbors; if labels differ, mark as strong if DeltaE > thresh
    strong = np.zeros_like(boundaries, dtype=bool)
    H, W = labels.shape
    for dy, dx in [(0, 1), (1, 0)]:
        y2 = np.clip(np.arange(H)[:, None] + dy, 0, H-1)
        x2 = np.clip(np.arange(W)[None, :] + dx, 0, W-1)
        l1 = labels
        l2 = labels[y2, x2]
        diff = l1 != l2
        if diff.any():
            m1 = means[l1]
            m2 = means[l2]
            de = _deltaE94(m1, m2)
            strong |= (diff & (de > deltaE_thresh))
    return strong


def _overlay_edges(bgr: np.ndarray, edge_map: np.ndarray, color=(0, 0, 255)) -> np.ndarray:
    out = bgr.copy()
    out[edge_map] = color
    return out


# -------------------- Main extractor --------------------

def extract_boundary(
    image: Union[str, Path, np.ndarray],
    # Overall mode
    mode: str = "edges",  # "edges" for full edge map; "boundary" for single primary contour as before
    # Primary-contour params (legacy/general)
    strategy: str = "auto",
    min_area_ratio: float = 0.01,
    approx_epsilon_ratio: float = 0.01,
    boundary_width: int = 3,
    # Edge detection params
    edge_mode: str = "all",   # "gray", "lab", "multiscale", "superpixel", "all"
    blur_ksize: int = 5,
    canny_sigma_ratio: float = 0.33,
    multiscale_ksizes: Tuple[int, ...] = (5, 9, 13),
    hough: bool = True,
    hough_min_line: int = 80,
    hough_max_gap: int = 12,
    hough_thickness: int = 1,
    superpixel_segments: int = 800,
    superpixel_compactness: float = 10.0,
    deltaE_thresh: float = 12.0,
    # Debug/logging
    return_debug: bool = True,
    log_level: int = logging.INFO,
) -> BoundaryResult:
    """
    Extract edges/boundaries with color and scale awareness.

    Parameters (key ones):
        mode: "edges" (default) to produce a dense edge_map capturing region boundaries,
              or "boundary" to also try a primary outer contour (legacy behavior).
        edge_mode: which edge sources to use:
            - "gray": Canny on enhanced grayscale only
            - "lab": Canny on L,a,b channels union
            - "multiscale": lab at multiple blur scales
            - "superpixel": only superpixel-based high-contrast boundaries (requires scikit-image)
            - "all": union of multiscale Lab edges (+gray) and superpixel boundaries (if available)
        hough: if True, reinforce long straight edges with probabilistic Hough lines
        deltaE_thresh: Lab color difference threshold for superpixel boundaries (higher -> fewer, stronger)

    Returns:
        BoundaryResult (see module docstring for full field descriptions)
    """
    logger.setLevel(log_level)
    errors: List[str] = []
    debug: Dict[str, np.ndarray] = {}
    meta: Dict[str, Any] = {
        "mode": mode, "strategy": strategy, "edge_mode": edge_mode,
        "blur_ksize": blur_ksize, "canny_sigma_ratio": canny_sigma_ratio,
        "multiscale_ksizes": multiscale_ksizes, "hough": hough,
        "superpixel_segments": superpixel_segments, "superpixel_compactness": superpixel_compactness,
        "deltaE_thresh": deltaE_thresh, "boundary_width": boundary_width,
    }

    bgr = _read_image(image)
    if bgr is None:
        errors.append("Failed to load image.")
        return BoundaryResult(
            status="error", boundary_mask=None, object_mask=None, contours=[],
            polygon=None, hierarchy=None, edge_map=None, debug=debug, errors=errors, meta=meta
        )
    H, W = bgr.shape[:2]
    img_area = float(H * W)

    # ---------- Build unified edge map (color- and scale-aware) ----------
    try:
        # Base gray edges
        edges_gray = _edges_canny_gray(bgr, blur_ksize=blur_ksize, sigma_ratio=canny_sigma_ratio)
        debug["edges_gray"] = (edges_gray.astype(np.uint8) * 255)

        edge_sources: List[np.ndarray] = []

        if edge_mode in ("gray", "all"):
            edge_sources.append(edges_gray)

        if edge_mode in ("lab", "all"):
            e_lab, d_lab = _edges_canny_lab(bgr, blur_ksize=blur_ksize, sigma_ratio=canny_sigma_ratio)
            edge_sources.append(e_lab)
            for k, v in d_lab.items():
                debug[k] = v

        if edge_mode in ("multiscale", "all"):
            e_ms, d_ms = _edges_multiscale_lab(bgr, scales=multiscale_ksizes, sigma_ratio=canny_sigma_ratio)
            edge_sources.append(e_ms)
            for k, v in d_ms.items():
                debug[k] = v
            debug["edges_multiscale"] = (e_ms.astype(np.uint8) * 255)

        if edge_mode in ("superpixel", "all"):
            e_sp = _superpixel_strong_boundaries(
                bgr, n_segments=superpixel_segments,
                compactness=superpixel_compactness, deltaE_thresh=deltaE_thresh
            )
            if e_sp is not None:
                edge_sources.append(e_sp)
                debug["edges_superpixel"] = (e_sp.astype(np.uint8) * 255)

        if not edge_sources:
            errors.append(f"No edge sources were selected for edge_mode='{edge_mode}'.")
            edge_map = edges_gray
        else:
            edge_map = edge_sources[0].copy()
            for e in edge_sources[1:]:
                edge_map |= e

        # Clean and optionally reinforce with Hough
        if hough:
            edge_map, e_hough = _reinforce_with_hough(
                edge_map, min_line_len=hough_min_line, max_line_gap=hough_max_gap, thickness=hough_thickness
            )
            debug["edges_hough"] = e_hough

        debug["edge_overlay"] = _overlay_edges(bgr, edge_map, (0, 0, 255))

    except Exception as e:
        logger.exception("Edge pipeline failed: %s", e)
        errors.append(f"Edge pipeline error: {e!r}")
        edge_map = None

    # ---------- Optional primary contour for legacy "boundary" mode ----------
    object_mask = None
    boundary_mask = None
    contours: List[np.ndarray] = []
    polygon = None
    hierarchy = None

    if mode == "boundary":
        try:
            gray = _to_gray_enhanced(bgr)
            k = 5
            gray_blur = cv2.GaussianBlur(gray, (k, k), 0)
            tl, th = _auto_canny_thresholds(gray_blur, canny_sigma_ratio)
            edges = cv2.Canny(gray_blur, tl, th)
            mk = 5
            kernel = cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (mk, mk))
            edges_closed = cv2.morphologyEx(edges, cv2.MORPH_CLOSE, kernel, iterations=1)
            cts, hier = cv2.findContours(edges_closed, cv2.RETR_CCOMP, cv2.CHAIN_APPROX_SIMPLE)
            hierarchy = hier
            min_area = img_area * min_area_ratio
            cont = _largest_external_contour(cts, hier, min_area)
            if cont is not None:
                primary_contour = cont.reshape(-1, 2).astype(np.float32)
                polygon = _approx_polygon(cont, approx_epsilon_ratio)
                contours = [primary_contour]
                object_mask = _make_mask_from_contour((H, W), primary_contour)
                boundary_mask = _boundary_from_mask(object_mask, width=max(1, boundary_width))
                ov = bgr.copy()
                if boundary_mask is not None:
                    ov[boundary_mask] = (0, 0, 255)
                debug["overlay"] = ov
        except Exception as e:
            logger.exception("Primary contour pipeline failed: %s", e)
            errors.append(f"Primary contour error: {e!r}")

    status = "ok" if (edge_map is not None and edge_map.any()) or (boundary_mask is not None and boundary_mask.any()) else ("partial" if edge_map is not None else "error")
    return BoundaryResult(
        status=status,
        boundary_mask=boundary_mask,
        object_mask=object_mask,
        contours=contours,
        polygon=polygon,
        hierarchy=hierarchy,
        edge_map=edge_map,
        debug=debug if return_debug else {},
        errors=errors,
        meta=meta,
    )


# -------------------- CLI --------------------

def _save_debug_images(out_dir: Path, debug: Dict[str, np.ndarray]) -> None:
    out_dir.mkdir(parents=True, exist_ok=True)
    for k, img in debug.items():
        p = out_dir / f"{k}.png"
        arr = img
        if arr is None:
            continue
        if isinstance(arr, np.ndarray) and arr.dtype == bool:
            arr = (arr.astype(np.uint8) * 255)
        cv2.imwrite(str(p), arr)


def main():
    parser = argparse.ArgumentParser(description="Color- and scale-aware edge/boundary extraction.")
    parser.add_argument("image", help="Path to input image")
    parser.add_argument("--mode", choices=["edges", "boundary"], default="edges")
    parser.add_argument("--edge-mode", choices=["gray", "lab", "multiscale", "superpixel", "all"], default="all")
    parser.add_argument("--out-dir", type=str, default=None, help="Directory to save debug outputs")
    parser.add_argument("--debug", action="store_true", help="Save intermediate images")
    parser.add_argument("--log-level", type=str, default="INFO", choices=["DEBUG", "INFO", "WARNING", "ERROR"])

    # Hough
    parser.add_argument("--hough", action="store_true", help="Enable Hough line reinforcement")
    parser.add_argument("--no-hough", dest="hough", action="store_false")
    parser.set_defaults(hough=True)
    parser.add_argument("--hough-min-line", type=int, default=80)
    parser.add_argument("--hough-max-gap", type=int, default=12)
    parser.add_argument("--hough-thickness", type=int, default=1)

    # Superpixel and DeltaE
    parser.add_argument("--deltaE", type=float, default=12.0, help="DeltaE threshold for superpixel boundaries")
    parser.add_argument("--sp-segments", type=int, default=800)
    parser.add_argument("--sp-compactness", type=float, default=10.0)

    args = parser.parse_args()

    result = extract_boundary(
        args.image,
        mode=args.mode,
        edge_mode=args.edge_mode,
        hough=args.hough,
        hough_min_line=args.hough_min_line,
        hough_max_gap=args.hough_max_gap,
        hough_thickness=args.hough_thickness,
        superpixel_segments=args.sp_segments,
        superpixel_compactness=args.sp_compactness,
        deltaE_thresh=args.deltaE,
        return_debug=args.debug,
        log_level=getattr(logging, args.log_level),
    )

    logger.info("Status: %s; Errors: %s", result.status, "; ".join(result.errors) if result.errors else "None")
    if args.out_dir and args.debug and result.debug:
        _save_debug_images(Path(args.out_dir), result.debug)
        logger.info("Saved debug images to %s", args.out_dir)


if __name__ == "__main__":
    main()