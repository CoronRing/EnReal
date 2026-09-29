# DESIGN.md
EnReal: Screenshot-to-Stage (Rule-based)

Document version: 1.1

This document describes the system architecture, data contracts, implemented features, and a concrete plan for upcoming features to guide additional engineering work.

Goals
- Convert an arbitrary screenshot into a 2D platformer “stage” with:
  - Platforms (horizontal, one-way)
  - Barriers (vertical, bullets-only)
  - Bounds and a suggested spawn point
- Provide a debug visualization (red overlay over the screenshot)
- Offer a simple front-end to upload images and tune parameters
- Keep the pipeline deterministic, fast, and offline (no ML dependency)

Non-Goals (for v1)
- Photo-realistic understanding (heavy ML); we use rule-based CV
- Full in-browser editing (planned)

---

1. High-Level Architecture

Components
- Frontend (static, src/frontend/):
  - index.html: Upload UI, parameter controls, overlay + JSON preview
  - app.js: Calls backend, displays results, hands the payload to the game
  - game.html, game.js: Phaser 3 (Matter) platformer built from the skeleton
  - vn_player.js: Visual novel overlay that plays a story pack before the stage
- Backend (FastAPI, src/backend/):
  - main.py: API, static hosting, logging and error handling
  - processing.py: End-to-end pipeline; returns skeleton + overlay
  - boundary_extraction.py: Edge and boundary extraction used by the pipeline
  - geometry.py: Geometry primitives and operations (snap, merge, prune)
  - models.py: Pydantic I/O schemas
  - novel.py: Story generation (LLM via litellm) and story pack building
  - utils.py: Logging configuration
- voice_synth.py (project root): Boson voice cloning client. This copy is a stub that raises NotImplementedError, so voice falls back to silence; the working client is `src/voice_synth.py` in the SirenCore repository.

Runtime flow
1) User uploads an image via UI
2) Backend runs the CV pipeline:
   - Preprocess (downscale → grayscale → blur → Canny)
   - Hough line detection; classify horizontal vs vertical
   - Merge colinear segments; snap to grid; prune by spacing and caps
   - Fallback synthetic platforms if too sparse
3) Produce:
   - Skeleton JSON (bounds, spawn, platforms, barriers, meta)
   - Debug overlay image (original + red line overlay, as base64 PNG)
4) Frontend displays overlay and JSON

Deployment
- Cross-platform: Windows/Linux/macOS with Python 3.12+
- Dev server: `uv run python -m src.backend.main` from the project root. Autoreload is on by default; set UVICORN_RELOAD=0 to disable it, and HOST and PORT to change the bind address.
- Static frontend served by FastAPI at /. Requests to /story_pack are answered by that same mount from src/frontend/story_pack/, because it is registered first, so the dedicated /story_pack mount (the directory where new packs are written) is never reached. Newly generated packs return 404 until they are copied into src/frontend/story_pack/.
- Story generation needs OPENAI_API_KEY. Voice cloning needs VOICE_PROVIDER=boson, BOSON_API_KEY, and a reference voice (BOSON_REFERENCE_PATH plus BOSON_REFERENCE_TRANSCRIPT, or LOCAL_PRTS_ROOT to pick one automatically).

---

2. Data Contracts

API
- POST /api/process
  - Input: multipart/form-data { file: image }, query params to tune detection
  - Output: ProcessResponse
    - skeleton: Skeleton
      - bounds: { x: int, y: int, width: int, height: int }
      - spawn: { x: int, y: int }
      - platforms: [{ x1, y1, x2, y2 }]
      - barriers: [{ x1, y1, x2, y2 }]
      - ramps: [{ x1, y1, x2, y2 }] (always empty in the current pipeline)
      - curves: [{ points: [{ x, y }] }] (pixel-level polylines traced from the cleaned edge map)
      - meta: {
          gridSize: int,
          sourceImageSize: [width, height],
          params: dict (thresholds/limits used)
        }
    - overlay_image_b64: "data:image/png;base64,..."
    - pack_id: string or null (story pack id; null when story generation failed and the game should start directly)
    - pack_base_url: string or null (/story_pack/<pack_id>)
    - manifest_url: string or null (<pack_base_url>/manifest.json)
- GET /api/packs: lists the story packs found in the story pack directory
- GET /api/debug/story_root: reports which story pack directory was resolved
- Static mounts: / (frontend), /story_pack (story packs)

Coordinate system
- Pixel coordinates in the image space with origin at top-left
- +x to the right, +y downward
- Segments are clamped to the image bounds

Storage format (planned)
- Stage document
  - id: string (uuid or hash of image)
  - created_at, updated_at: ISO timestamps
  - source_image: URL or hash key (if stored)
  - skeleton: Skeleton (as defined above)
  - user_edits: [{ action, payload, timestamp }] (if editor present)
  - version: int

---

3. Pipeline Design

Overview
- Inputs: image bytes
- Params: max_dim, grid_size, min lengths, spacing, caps, angle tolerances
- Outputs: skeleton + overlay

Steps
1) Preprocess
   - Downscale to max_dim keeping aspect ratio (default 1280)
   - Convert to grayscale
   - Gaussian blur (5x5)
   - Canny edges with adaptive thresholds based on image median (sigma=0.33)
2) Probabilistic Hough Lines
   - rho=1 px, theta=1°, threshold=60 votes
   - minLineLength ~ 8% of max(image width/height), maxLineGap ~ 1% of max dim
3) Orientation classification by angle
   - Horizontal if |angle| <= h_angle_tol_deg (default 7°)
   - Vertical if |90° - angle| <= v_angle_tol_deg (default 7°)
4) Merge and snap
   - Snap endpoints to grid (grid_size, default 8 px)
   - Merge along axis:
     - Platforms (horizontal): cluster by y; merge overlapping/near ranges on x
     - Barriers (vertical): cluster by x; merge overlapping/near ranges on y
   - Apply min lengths:
     - Platforms: >= min_platform_len_pct * width (default 15%)
     - Barriers: >= min_barrier_len_pct * height (default 12%)
5) Prune density and cap totals
   - Platforms: enforce min vertical spacing (default 48 px), keep longest first
   - Barriers: enforce min horizontal spacing (default 48 px), keep longest first
   - Caps: platforms <= 100, barriers <= 60
6) Fallback
   - If no platforms remain, synthesize 2–3 horizontal platforms at 90%, 60%, 30% height
7) Spawn and bounds
   - Bounds = full image rect
   - Spawn = midpoint of lowest platform; fallback near bottom center
8) Overlay
   - Draw all segments (platforms + barriers) in red; AA lines; 2 px thickness
   - Encode overlay image as base64 PNG data URL

Key design choices
- Deterministic rule-based approach: reproducible and tunable without models
- Grid snapping: reduces micro-misalignment and simplifies gameplay geometry
- Spacing and caps: mitigate noisy UIs and over-detection
- Fallback platforms: always produce a playable-ish layout

---

4. Implemented Features (v1)

- API endpoint: /api/process (FastAPI)
- Rule-based CV pipeline using OpenCV:
  - Canny edges, Hough lines, orientation classification
  - Merge, grid snapping, length filtering, spacing pruning
- Fallback synthetic platforms
- Spawn selection and bounds
- Debug overlay (red lines)
- Frontend to upload image, adjust parameters, preview results
- Logging, input validation, error handling, 30MB file size cap

---

5. Planned Features and Implementation Plan

A) Overlay Editor (priority)
- Goal: Allow users to correct auto-detected geometry
- Frontend:
  - Overlay canvas on top of the background image
  - Tools:
    - Toggle segment type: Platform / Barrier / Remove
    - Add segment by click-drag with snap-to-grid
    - Drag endpoints; delete via keyboard
    - Set spawn by click
  - Visual cues:
    - Color-code: Platform (red), Barrier (blue), Selected (yellow), Spawn (green)
    - Grid overlay toggle
- Backend:
  - Endpoints:
    - POST /api/stage (create) -> returns stage_id
    - GET /api/stage/{id} (retrieve)
    - PUT /api/stage/{id} (update, store edited skeleton)
    - Optional: POST /api/process?return_stage=true -> creates stage + returns id
  - Persistence:
    - Start with JSON file store on disk: stages/{id}.json
    - Later: SQLite or Postgres
- Acceptance criteria:
  - Users can add/edit/delete segments and set spawn
  - Changes persist and reload by stage_id
  - Undo/redo (optional v1.1)

B) Gameplay Integration Snippets
- Phaser 3 + Matter.js example:
  - Load background image and skeleton JSON
  - Create bounds as static walls
  - Platforms as one-way edges:
    - Only resolve collision when player is falling and above platform
    - Drop-through if “down” held: temporarily disable collision on overlapping platform
  - Barriers only collide with bullets (collision category masks)
- Deliverables:
  - /examples/phaser-matter/ with minimal playable demo (keyboard: A/D/W/S, Space)
- Acceptance criteria:
  - Avatar moves and jumps; can drop through platforms
  - Bullets stop on barriers; avatar passes through barriers

C) OCR/Text Suppression (noise reduction)
- Use pytesseract to detect text boxes/lines
- Suppress lines inside detected text regions to avoid tiny noisy segments
- Add feature flag: enable_ocr=true
- Acceptance criteria:
  - Fewer micro-lines in text-heavy screenshots
  - CPU budget acceptable (guard with max runtime)

D) Rectangle/Contour Heuristics
- Use cv2.findContours to detect rectangles/panels
- Prioritize top/bottom edges of large rectangles as platforms
- Prefer left/right edges of rectangles as barriers
- Acceptance criteria:
  - More stable large structures in UI layouts
  - Does not over-generate dense small segments

E) Caching and Idempotency
- Hash input file (SHA-256) and parameters (JSON) -> cache key
- Store skeleton + overlay in cache directory
- Endpoint query param: use_cache=true
- Acceptance criteria:
  - Repeated requests with same input return cached results
  - Configurable cache size and eviction policy (LRU)

F) CLI and Batch Mode
- Add a CLI: python -m app.cli --input path --output out_dir [params]
- Batch process images for testing/tuning
- Acceptance criteria:
  - Processes directories; writes skeleton.json and overlay.png per image

G) Dockerization and CI
- Dockerfile with multi-stage build; uvicorn startup
- GitHub Actions:
  - Lint (ruff), format (black), type-check (mypy), run unit tests
- Acceptance criteria:
  - One-command container build/run
  - Automated checks on PRs

H) Unit and Golden Tests
- geometry.py tests:
  - Merging behavior, snapping, pruning, axis classification
- processing.py tests:
  - Deterministic fixtures: known inputs -> known outputs
- Acceptance criteria:
  - >85% coverage on geometry; snapshot tests for pipelines

---

6. Detailed Specs for Key Planned Features

One-way Platform Logic (engine-agnostic)
- Rule:
  - Apply collision response only if:
    - avatar.vy > 0 (falling), and
    - previousBottom <= platform.y, and
    - currentBottom >= platform.y - epsilon
  - Drop-through:
    - When “down” pressed and overlapping, disable collision with that platform for 250–350 ms or until avatar center < platform.y
- Matter.js sketch:
  - Use collisionStart/Active callbacks
  - Maintain a set of disabled platform pairs for drop-through timeout

Barrier vs Bullet Collisions
- Collision categories:
  - Avatar: 0x0001
  - Bullet: 0x0002
  - Platforms: 0x0004
  - Barriers: 0x0008
  - Bounds: 0x0010
- Masks:
  - Avatar: Platforms | Bounds
  - Bullet: Barriers | Bounds (optionally Platforms)
  - Bounds: collide with all

Editor Data Flow
- Frontend state:
  - backgroundImage, segments[], spawn, selection
  - history stack for undo/redo
- Backend:
  - PUT /api/stage/{id}: persists the full skeleton; server validates:
    - Coordinates within bounds
    - Segment length >= minimal threshold
    - Types in {Platform, Barrier}
- Conflicts:
  - ETag-based optimistic concurrency (optional)
  - Or always last-write-wins initially

Storage Layout (file-based prototype)
- data/
  - stages/
    - {id}.json
  - images/
    - {id}.png (optional copy)
- Index:
  - data/index.json mapping id -> metadata (filename, timestamps)

Security and Limits
- Input limits:
  - Max file size (30MB default)
  - Allowed content types (PNG/JPG/WEBP)
- Resource limits:
  - Processing timeout (e.g., 10–15s safeguard)
  - OCR optional and off by default
- Sanitization:
  - No arbitrary file writes from user input; only server-generated paths

---

7. Performance Considerations

- Downscale large images (max_dim) to keep Hough manageable
- Use adaptive thresholds to avoid per-image tuning
- Snap and merge reduce segment count early
- Prune by spacing to cap complexity
- Potential future optimization:
  - Early ROI detection to skip edges in texture-heavy regions
  - Multi-threaded Hough (OpenCV internal optimizations)

---

8. Observability

- Logging: ENREAL_LOGLEVEL env var (DEBUG/INFO/WARN/ERROR)
- Key logs:
  - Input size, image dims
  - Count of raw/merged/pruned segments
  - Fallback usage
  - Timing (planned: add simple timing metrics)
- Diagnostics:
  - Overlay images stored when caching is enabled (planned)

---

9. Risks and Mitigations

- Over-detection on complex images:
  - Increase min length; stricter spacing; caps; OCR/text suppression (planned)
- Under-detection on minimalistic images:
  - Fallback synthetic platforms; consider rectangle heuristics
- Non-determinism across OpenCV versions:
  - Pin versions; use golden tests; record meta params
- Large images:
  - Enforce max_dim; bound CPU and request timeouts

---

10. Milestones and Task Breakdown

M1: Stabilize pipeline (Done)
- API endpoint, pipeline, overlay, frontend
- Logging and error handling

M2: Editor MVP (2–3 weeks)
- Frontend overlay editor (canvas, grid, color-coding, add/edit/delete)
- Backend persistence: file-based stages; GET/PUT endpoints
- Acceptance tests for validation

M3: Gameplay Example (1–2 weeks)
- Phaser + Matter.js example with one-way platforms and bullet/barrier masks
- Small assets and controls; documentation

M4: Enhancements (2–4 weeks)
- Caching by input hash
- OCR/text suppression (feature-flagged)
- Rectangle/contour heuristics
- CLI batch mode

M5: Quality and Ops (1–2 weeks)
- Unit tests (geometry + fixtures)
- Dockerfile; CI pipeline
- Optional: SQLite persistence

M6: Dual-mode runtime (section 12)
- Split into the enreal package: stage, story, server
- Browser backend: Pyodide worker, Backend interface, IndexedDB story packs
- Boundary test for enreal.stage and a CPython vs Pyodide parity test

---

11. Open Questions

- Should bullets collide with Platforms by default? Current design leaves it configurable at runtime.
- Editor UX scope:
  - Do we need multi-select and bulk operations from the start?
  - Do we need a “goal” object and enemies later?
- Persistence:
  - File-based vs. SQLite for MVP? File-based is simplest; SQLite is safer for concurrency.
- Browser mode story generation: off, bring your own key, or hybrid (see 12.5)?
- Version control: EnReal is not tracked in any git repository yet. Give it its own repository before the section 12 restructure?

---

12. Dual-Mode Runtime: Browser or Server (planned)

Goal: EnReal runs entirely from static files, with image parsing done in the player's browser, while the existing server mode keeps working. The parsing logic becomes one Python library that both the server and the browser import, so there is a single implementation to maintain.

Status: design only. Nothing in this section is implemented yet.

12.1 Modes

| Mode | Parsing runs in | Story generation runs in | How it is hosted |
|---|---|---|---|
| Server | FastAPI process (CPython) | Server (`litellm`, Boson) | `uv run python -m enreal.server` |
| Browser | Pyodide in a Web Worker | Browser, see 12.5 | Any static file host |

Pyodide is CPython compiled to WebAssembly, with prebuilt browser builds of NumPy, OpenCV, Pydantic and Pillow. A Web Worker is a background browser thread, which keeps the page responsive while Python loads and runs.

"No server" still means a static file host. Browsers refuse to load WebAssembly from `file://`, so double-clicking `index.html` will not work in browser mode. GitHub Pages, `python -m http.server`, or any CDN are enough, since nothing runs on the host.

12.2 Package layout

```
EnReal/
  pyproject.toml              name = "enreal"; extras: story, server
  src/enreal/
    __init__.py               re-exports generate_skeleton, ProcessResponse, Skeleton
    stage/                    image bytes in, ProcessResponse out
      pipeline.py             current processing.py
      boundary_extraction.py
      geometry.py
      models.py
    story/
      pack.py                 builds a pack in memory: manifest plus {path: bytes}
      providers.py            StoryProvider and VoiceProvider protocols, silence default
      voice_boson.py          Boson client, server only
    server/
      app.py                  FastAPI, a thin wrapper over enreal.stage and enreal.story
  web/                        static frontend, currently src/frontend
    index.html, game.html, app.js, game.js, vn_player.js
    backend.js                Backend interface, RemoteBackend, LocalBackend
    worker.js                 Pyodide worker
    wheels/enreal-<version>-py3-none-any.whl
```

`enreal.stage` rules: no file access, no network, no environment reads, no FastAPI. It imports only NumPy, OpenCV and Pydantic. A test imports it with `fastapi`, `litellm` and `PIL` blocked so the rule cannot drift. This already nearly holds: `generate_skeleton(image_bytes, ...)` is pure, and the only file writes in these modules are in the `boundary_extraction.py` command line helper, which moves out of the package.

Dependencies:

| Install | Pulls in |
|---|---|
| `enreal` | `numpy`, `opencv-python-headless`, `pydantic` |
| `enreal[story]` | adds `pillow` |
| `enreal[server]` | adds `fastapi`, `uvicorn`, `python-multipart`, `litellm`, `openai`, `pillow` |

Pyodide ships `opencv-python`, not `opencv-python-headless`, so `micropip` cannot satisfy the declared dependency. The worker therefore loads `numpy`, `opencv-python` and `pydantic` from the Pyodide distribution with `pyodide.loadPackage`, then installs the EnReal wheel with `micropip.install(url, deps=False)`.

12.3 Frontend backend interface

Both modes sit behind one JavaScript interface, and both return the same `ProcessResponse` JSON, so `app.js` and `game.js` do not know which one ran.

```js
// backend.js
interface Backend {
  mode: 'remote' | 'local';
  ready(onProgress?): Promise<void>;          // local: loads Pyodide and the wheel
  process(file, params): Promise<ProcessResponse>;
  buildStory?(file, skeleton): Promise<Pack>; // availability depends on mode, see 12.5
}
```

`RemoteBackend` calls `POST /api/process` as `app.js` does today.

`LocalBackend` talks to `worker.js` over `postMessage`. Requests are `{id, type, payload}`. The worker answers `{id, ok, result}` or `{id, ok: false, error}`, and sends `{type: "progress", stage, loaded, total}` events while loading, so the page can show a progress bar for the first download.

Mode selection, first match wins:

1. `?backend=local` or `?backend=remote` in the URL
2. A saved preference from a UI toggle
3. Auto: probe `GET /api/health` with a short timeout, use remote if it answers, local otherwise

Server addition: `GET /api/health` returns `{"status": "ok", "version": "<enreal version>"}`.

12.4 Moving data from index.html to game.html

Today the payload travels through `window.name` and `sessionStorage`, and the game fetches the pack from `/story_pack/...`. In browser mode there is no `/story_pack` URL, in-memory `blob:` URLs die when the page navigates, and `sessionStorage` holds only about 5 MB, which real voiced packs exceed.

Browser-built packs are stored in IndexedDB, keyed by pack id. `game.html` reads the pack, creates `blob:` URLs for each asset, and calls `VNPlayer.start(manifest)` with `base_url: ""`. `vn_player.js` already uses asset paths as-is when `base_url` is empty, so it needs no changes.

12.5 Story generation in browser mode

The pack assets themselves (gradient background, placeholder portraits, silent line audio) are plain Pillow and run in the worker. Two steps need outside services:

- Story text: the server calls `gpt-4o` through `litellm`, which does not run in Pyodide.
- Voice: the Boson client targets `hackathon.boson.ai`, an event endpoint whose browser (CORS) policy and lifetime are unknown.

Options for browser mode:

| Option | Behavior | Trade-off |
|---|---|---|
| Off | Stage only; bundled packs or no VN intro | Zero keys, fully offline |
| Bring your own key | Browser calls the LLM provider's HTTP API directly; key stays in the user's browser | Needs a provider that allows browser calls; key is visible to anyone using that browser |
| Hybrid | Parse locally, request the story from an EnReal server | Still needs a server for the story |

Voice falls back to silent line audio in browser mode in every option.

Decision pending, see Open Questions.

12.6 Result parity between modes

- OpenCV: the server venv has 4.10.0.84 and Pyodide 314.0.7 ships 4.11.0.86. Pin the server to 4.11.0.86 so both modes produce the same geometry, in line with the version-pinning mitigation in section 9.
- Python: Pyodide 314.0.7 bundles CPython 3.14.2, so the library must support 3.12 through 3.14.
- Optional steps: `skimage` superpixels and `cv2.ximgproc` thinning are absent from the server venv today, so current results already come from their fallback paths. The browser build omits both too. scikit-image would add SciPy and Matplotlib to the download.
- A golden test runs fixture images through CPython and through Pyodide under Node, and compares the skeleton JSON.

12.7 Download cost

Measured on 2026-09-28 by downloading each file from jsDelivr for Pyodide 314.0.7:

| File | Size |
|---|---|
| `pyodide.asm.wasm` | 9.2 MB (3.4 MB compressed in transfer) |
| `python_stdlib.zip` | 2.4 MB |
| `pyodide.asm.mjs` | 1.2 MB |
| `opencv-python` 4.11.0.86 | 10.2 MB |
| `numpy` 2.4.6 | 2.8 MB |
| `pydantic`, `pydantic_core` and three small dependencies | 1.8 MB |
| Total | 27.5 MB raw, about 22 MB transferred |

The browser caches these after the first visit. For fully offline use, self-host them under `web/pyodide/` instead of loading from the CDN. Parse time in WebAssembly has not been measured yet; expect it to be slower than native because it runs on a single thread.

12.8 Security

- Browser mode never ships an API key. A user-supplied key is kept in that user's browser and sent only to the provider they chose.
- Server mode is unchanged, including the 30 MB upload cap.

12.9 Migration steps

1. Create `src/enreal/` and move the stage modules into `enreal.stage`, fixing imports.
2. Split `novel.py` into `enreal.story.pack` (pure builder) and providers.
3. Move the FastAPI app to `enreal.server` and add `GET /api/health`.
4. Move `src/frontend/` to `web/`, add `backend.js` and `worker.js`, and switch `app.js` to the `Backend` interface.
5. Teach `game.js` to load IndexedDB packs.
6. Add a build step: `uv build` the wheel and copy it into `web/wheels/`.
7. Add the `enreal.stage` boundary test and the parity test.

---

Appendix A: Parameter Defaults

- max_dim: 1280
- grid_size: 8
- h_angle_tol_deg: 7
- v_angle_tol_deg: 7
- min_platform_len_pct: 0.15 (of width)
- min_barrier_len_pct: 0.12 (of height)
- platform_spacing: 48 px
- barrier_spacing: 48 px
- max_platforms: 100
- max_barriers: 60

---

Appendix B: Developer Notes

- Keep geometry.py pure and thoroughly unit-tested
- processing.py should remain a thin orchestrator around:
  - preprocess()
  - detect_lines()
  - classify()
  - merge_and_snap()
  - prune()
  - build_skeleton()
- Avoid coupling editor/persistence logic into processing.py; treat it as a separate concern
- When adding new heuristics, expose parameters via API query to allow runtime tuning