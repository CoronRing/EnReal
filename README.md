# EnReal: Screenshot-to-Stage (Rule-based)

Turn a screenshot into a 2D platformer skeleton using a deterministic, rule-based computer vision pipeline. The backend extracts horizontal “Platforms” and vertical “Barriers” and produces:
- A Skeleton JSON describing bounds, spawn, platforms, barriers, and meta
- A debug overlay image (base64 PNG) with all generated lines drawn in red on top of the original screenshot

The web UI lets you upload an image, tweak parameters, and preview the output.

---

## What’s Implemented

- Python FastAPI backend with a single endpoint: POST /api/process
- Rule-based CV pipeline (OpenCV) to detect structural lines:
  - Downscale, grayscale, blur, Canny edge detection
  - Probabilistic Hough transform to detect line segments
  - Orientation classification: horizontal (Platforms), vertical (Barriers)
  - Merge colinear segments with grid snapping and gap joining
  - Prune by spacing and cap counts to reduce clutter
  - Fallback synthetic platforms if detection is too sparse
- Spawn selection: midpoint of the lowest platform (fallback near bottom)
- Bounds: full image rectangle
- Meta block: records source size, grid size, and detection parameters
- Debug overlay: original image with generated lines drawn in red
- Simple frontend to upload images, control parameters, preview overlay, and view JSON
- Logging and basic error handling

---

## Current Logic (Pipeline)

1) Preprocess
- Downscale image to max_dim (default 1280) to reduce noise and speed up processing
- Convert to grayscale and apply Gaussian blur
- Compute Canny edges with adaptive thresholds (median-based heuristic)

2) Structural Line Detection
- Use Probabilistic Hough Transform on edges
- Classify segments by angle:
  - Horizontal if |angle| <= h_angle_tol_deg (default 7°)
  - Vertical if |90° - angle| <= v_angle_tol_deg (default 7°)

3) Merge, Snap, and Filter
- Merge colinear segments:
  - Horizontals: cluster by y, merge overlapping/nearby x spans
  - Verticals: cluster by x, merge overlapping/nearby y spans
- Snap endpoints to a grid (grid_size, default 8 px)
- Filter by minimum length:
  - Platforms: min_platform_len_pct of image width (default 0.15)
  - Barriers: min_barrier_len_pct of image height (default 0.12)
- Prune by spacing:
  - Platforms spaced along y with minimum spacing (default 48 px)
  - Barriers spaced along x with minimum spacing (default 48 px)
- Cap counts: max_platforms (default 100), max_barriers (default 60)
- Fallback: if no platforms remain, synthesize 2–3 evenly spaced platforms

4) Output
- Skeleton JSON: bounds, spawn, platforms, barriers, meta
- Debug overlay image (base64 data URL)

---

## Data Model (Response)

Endpoint: POST /api/process

Response shape:
- skeleton: {
  - bounds: { x: int, y: int, width: int, height: int }
  - spawn: { x: int, y: int }
  - platforms: [{ x1: int, y1: int, x2: int, y2: int }, ...]
  - barriers: [{ x1: int, y1: int, x2: int, y2: int }, ...]
  - meta: {
      gridSize: int,
      sourceImageSize: [width: int, height: int],
      params: dict of key parameters
    }
}
- overlay_image_b64: "data:image/png;base64,..." (debug visualization)

Coordinate system: origin at top-left; +x right, +y down. Segments are clamped to image bounds.

Note: One-way platform behavior and bullet-vs-barrier collision logic must be implemented in your game engine at runtime using this skeleton.

---

## Project Structure

- app/main.py — FastAPI app, routes, static hosting
- app/processing.py — Image pipeline, main generator, overlay drawing
- app/geometry.py — Segment helpers (merge, snap, prune)
- app/models.py — Pydantic models for I/O
- app/utils.py — Logging setup
- app/static/index.html — Web UI
- app/static/app.js — UI logic
- requirements.txt — Dependencies

---

## Installation and Launch

Prerequisites:
- Python 3.9+ (Windows or Linux)
- pip and virtualenv recommended

Steps:
1) Create and activate a virtual environment
- Windows:
  - python -m venv .venv
  - .venv\Scripts\activate
- Linux/macOS:
  - python -m venv .venv
  - source .venv/bin/activate

2) Install dependencies
- pip install -r requirements.txt

3) Run the server
- uvicorn app.main:app --reload

4) Open the UI
- Navigate to http://127.0.0.1:8000

---

## Usage

Web UI
- Choose an image (PNG/JPG/WEBP)
- Adjust parameters if needed (Advanced parameters)
- Click Process
- Preview the red-line overlay and inspect the Skeleton JSON

cURL Example
- curl -X POST "http://127.0.0.1:8000/api/process?max_dim=1280&grid_size=8" \
  -F "file=@/path/to/screenshot.png" \
  -H "Accept: application/json"

---

## Parameters (Query)

- max_dim: int (default 1280) — downscale max dimension
- grid_size: int (default 8) — grid snapping size for endpoints
- min_platform_len_pct: float (default 0.15) — minimum platform length relative to image width
- min_barrier_len_pct: float (default 0.12) — minimum barrier length relative to image height
- platform_spacing: int (default 48) — minimum vertical spacing between platforms
- barrier_spacing: int (default 48) — minimum horizontal spacing between barriers
- max_platforms: int (default 100)
- max_barriers: int (default 60)
- h_angle_tol_deg: float (default 7) — horizontal angle tolerance
- v_angle_tol_deg: float (default 7) — vertical angle tolerance (relative to 90°)

---

## Debug Overlay

- Lines are drawn in red (as requested) over the original screenshot.
- This is a diagnostic artifact and not meant for gameplay.

---

## Error Handling and Logging

- Input validation: empty file and excessive size (default 30 MB cap) are rejected.
- Robust decoding: returns errors for unsupported/corrupt images.
- Graceful fallback: if detection fails, synthetic platforms are generated to keep the stage playable.
- Logging:
  - Configurable via ENREAL_LOGLEVEL (DEBUG/INFO/WARN/ERROR).
  - Outputs to stdout.

---

## Cross-Platform Notes

- Tested with Python, FastAPI, OpenCV; should work on Windows and Linux (and macOS) as long as OpenCV wheels are available for your Python version.
- No OS-specific file paths; static files served via FastAPI.

---

## What Remains (Roadmap)

Gameplay Integration
- Load the Skeleton JSON in a browser game (e.g., Phaser 3 with Matter.js or Arcade Physics).
- Implement one-way platform logic:
  - Collide only when falling and coming from above; allow drop-through on “down” input.
- Implement bullet-vs-barrier collision filtering:
  - Bullets stop on vertical Barriers, avatar passes through.

Editor and UX
- Overlay editor to:
  - Toggle segment types (Platform/Barrier/None)
  - Add/remove segments
  - Drag endpoints with grid snapping
  - Set Spawn/Goal
- Color-coded overlays (e.g., red=platforms, blue=barriers).

CV Enhancements
- OCR-based text suppression (ignore text baselines/stems).
- Rectangle/contour heuristics to prioritize structural edges.
- Adaptive threshold tuning per image class.

Backend Improvements
- Caching by image hash to reuse results.
- Persistence for edited stages (database/JSON store).
- Rate limiting and file size/type validation hardening.
- Dockerfile and containerized deployment.
- Unit tests for geometry operations (merge/snap/prune) and deterministic fixtures.
- CI pipeline for tests and linting (ruff/black/mypy).
- Config file or env-based tuning for defaults.

Performance
- Profile with large images and optimize thresholds.
- Optional WebAssembly (OpenCV.js) for client-side processing to offload server.

---

## Known Limitations

- UI-heavy screenshots work best; photos or highly textured images may produce noisy results.
- Purely automatic extraction can over/under-detect; an editor step is recommended for production.
- Angle tolerances and length thresholds may need tuning per dataset.

---

## License

- Add your preferred license (e.g., MIT) as LICENSE if needed.

---