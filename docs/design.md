# EnReal design

Document version: 2.0

EnReal turns an image into a playable 2D platformer stage. A rule-based computer vision engine traces the edges in the image, and a Phaser game uses those edges as terrain. This document covers the engine, the data contract between engine and game, the two ways the game reaches the engine, and how everything is built, tested and released.

## 1. Goals and scope

- Turn an arbitrary screenshot or artwork into terrain that is fun to run on, deterministically and without machine learning.
- Ship the engine as a small library anyone can install: [`enreal`](https://pypi.org/project/enreal/) on PyPI, depending only on NumPy, OpenCV and Pydantic.
- Run the whole game from static files, with the engine executing in the player's browser, so it can be hosted on GitHub Pages with no backend.
- Keep an optional server that runs the same engine, for development and for clients that should not download it.

Out of scope for now: story generation, voice, and anything else that needs an LLM or a paid API. The earlier story prototype was removed and will be redesigned from scratch (see `docs/TODO.md`).

## 2. Components

```
EnReal/
  src/enreal/            the engine; the only thing published to PyPI
    processing.py        generate_skeleton(): the pipeline entry point
    boundary_extraction.py  multiscale, color-aware edge extraction
    geometry.py          segment and grid-snapping helpers
    models.py            the data contract (Pydantic)
  server/                optional FastAPI server; not published
    app.py               GET /api/health, POST /api/process, static web/
  web/                   the game; a static site
    index.html, app.js   choose an image, parse it, preview, hand off to the game
    backend.js           engine interface: RemoteBackend and LocalBackend
    worker.js            Pyodide Web Worker that runs the engine in the browser
    game.html, game.js   the Phaser 3 (Matter) platformer
  scripts/build_web.py   builds the engine wheel into the web build
  tests/                 engine, server, purity and Pyodide parity tests
  .github/workflows/     ci.yml, release.yml (PyPI), pages.yml (GitHub Pages)
```

```
                 image bytes
                      |
        +-------------+--------------+
        |                            |
   LocalBackend                RemoteBackend
   (worker.js, Pyodide)        (POST api/process)
        |                            |
        |                      server/app.py
        |                            |
        +--------> enreal.generate_skeleton() <--------+
                      |
                StageResult JSON
                      |
          app.js -> sessionStorage -> game.js
```

## 3. The engine pipeline

Entry point: `enreal.generate_skeleton(image_bytes, ...) -> StageResult`. Everything here is pure: bytes in, a model out, no file, network or environment access. A test enforces that the engine imports and runs with the server stack, Pillow, scikit-image and SciPy all blocked (`tests/test_purity.py`).

1. **Decode and downscale.** `cv2.imdecode` reads the bytes; undecodable input raises `ValueError`. The image is downscaled so its longest side is at most `max_dim` (default 1280). Every output coordinate is in this downscaled space.
2. **Optional rectification (`"ui"` preset).** If a dominant quadrilateral covering at least 15% of the image is found, the image is warped to top-down, processed, and the result mapped back.
3. **Edge extraction** (`boundary_extraction.extract_boundary`, `edge_mode="all"`). The union of: Canny on an enhanced grayscale; Canny on each Lab channel; Lab Canny at blur scales 5, 9 and 13; probabilistic Hough reinforcement of long straight edges; and superpixel boundaries with a DeltaE contrast test when scikit-image is installed. If the extractor fails outright, `generate_skeleton` raises `RuntimeError` instead of returning an empty stage.
4. **Edge cleanup.** Morphological close (3 px) to bridge gaps; drop edge components that are too small by pixel count or bounding-box diagonal; keep only edge pixels that separate two sufficiently large regions; thin with `cv2.ximgproc` when the contrib module is installed.
5. **Curves.** Contours of the cleaned edge map become polylines. Short ones are dropped, points are snapped to `grid_size` (default 8) and clamped to the image.
6. **Overlay.** The downscaled image with every remaining edge pixel painted pure red, BGR `(0, 0, 255)`, encoded as a PNG data URL.
7. **Spawn.** Midpoint of the lowest straight platform, falling back to `(width / 2, 0.9 * height)`. The pipeline emits no straight platforms today, so the fallback always applies; see section 11.

Presets:

| Preset | Rectification | Region filter (`min_region_area_ratio`) | Component filter (`min_edge_component_pixels_ratio`) |
|---|---|---|---|
| `"ui"` | on by default | 0.006 | 0.0003 |
| `"art"` | off | 0.004 | 0.0002 |

Parameters and what they actually change:

| Parameter | Effect today |
|---|---|
| `max_dim` | Working resolution and output coordinate space |
| `grid_size` | Snapping of curve points |
| `scene` | Preset above |
| `rectify_perspective` | `"ui"` only: enables step 2 |
| `min_platform_len_pct`, `max_platforms` | None. Reserved for straight-platform detection; `min_platform_len_pct` is echoed in `meta` |
| `text_suppression` | None. Echoed in `meta`; the MSER text detector (`_text_mask`) exists but is not wired in |

`processing.py` also carries helpers that nothing calls yet: `_enhance_for_art` (CLAHE and bilateral filtering, which is why `use_clahe` and `use_bilateral` have no effect), `_text_mask`, `_hough_segments_from_edge_map`, `_auto_canny`, the gradient and coverage scorers, and `_draw_overlay`. They are the building blocks for straight-platform detection.

## 4. Data contract

`StageResult.model_dump_json()` is the JSON every consumer sees, whether the server or the browser produced it:

```json
{
  "skeleton": {
    "bounds": { "x": 0, "y": 0, "width": 1280, "height": 720 },
    "spawn": { "x": 640, "y": 648 },
    "platforms": [],
    "barriers": [],
    "ramps": [],
    "curves": [ { "points": [ { "x": 672, "y": 352 }, "..." ] } ],
    "meta": { "gridSize": 8, "sourceImageSize": [1280, 720], "params": { "scene": "art", "...": "..." } }
  },
  "overlay_image_b64": "data:image/png;base64,..."
}
```

- Coordinates are pixels in the downscaled image, origin top-left, y pointing down.
- `platforms`, `barriers` and `ramps` are `{x1, y1, x2, y2}` segments and are always empty today.
- `curves` is the detected terrain.
- The overlay is part of the contract, not a debug view: the game treats pure red pixels (r >= 200, g <= 40, b <= 40) as solid ground.

`tests/test_engine.py::test_json_contract_is_stable` pins these keys.

## 5. The game

`web/game.js` is a Phaser 3 platformer using Matter physics. It loads the overlay as an editable canvas texture and resolves collisions against its red pixels: ground probes under the feet, wall probes to the sides, and pixel-aware grenade physics. Explosions erase red pixels and the H key paints them, so the terrain is destructible and buildable. It spawns an NPC, an enemy that pursues the player, and a pickup (a fast gun), and has health, contact damage with immunity frames, an inventory panel, bombs, grenades, sprinting, double jump and drop-through.

The NPC chat panel is a placeholder: `mockChatEndpoint` echoes the player's text. It is where a future LLM-backed conversation would connect.

Handoff from the page to the game: `app.js` stores `{ skeleton, bgDataUrl }` under the `sessionStorage` key `enreal_payload` and in `window.name`. `game.js` reads `sessionStorage` first and falls back to `window.name`. If the `sessionStorage` write fails (quota), `app.js` clears the key so the game can never load an older stage by mistake. A 1280 px photo produces a payload of about 3 MB, within the typical 5 MB `sessionStorage` quota.

## 6. Two ways to reach the engine

`web/backend.js` exposes one interface with two implementations. Both resolve `process(file, params)` to the same `StageResult` JSON, so the page and the game do not know which one ran.

| | RemoteBackend | LocalBackend |
|---|---|---|
| Runs the engine in | `server/app.py` (CPython) | A module Web Worker (`worker.js`) under Pyodide |
| Transport | `POST api/process`, multipart | `postMessage` |
| Startup cost | none | first visit about 22 MB, then cached |
| Image leaves the device | yes | no |

**Selection**, first match wins:

1. `?backend=local` or `?backend=remote` in the page URL.
2. The choice saved from the page's Engine picker (`localStorage` key `enreal.backend`).
3. Auto: `GET api/health` with a 1.5 s timeout. If it answers `{"engine": "enreal"}`, use the server; otherwise run locally. On a static host this probe returns 404, which appears once in the browser console and is expected.

`?server=<url>` points RemoteBackend at another origin, which must list the page's origin in `ENREAL_CORS_ORIGINS`.

**Worker protocol.** Requests are `{id, type: "init" | "process", payload}`. Replies are `{id, ok: true, result}` or `{id, ok: false, error}`. During `init` the worker also sends `{type: "progress", stage, message}` events, which the page shows as status text. Python exceptions are reduced to their last traceback line, for example `ValueError: Unsupported or corrupted image: OpenCV could not decode it.`

**Worker startup.** Pyodide 314 refuses to run in classic workers, so the worker is created with `{type: "module"}` and loads `pyodide.mjs` with a dynamic `import()`. It reads `engine/engine.json`, loads NumPy, OpenCV and Pydantic from the Pyodide distribution, fetches the enreal wheel, verifies its SHA-256 against the manifest, and unpacks it into `site-packages`. The wheel declares `opencv-python-headless`, which has no WebAssembly build, so it is unpacked without dependency resolution while Pyodide's own `opencv-python` provides `cv2`.

## 7. Building and hosting the web game

`scripts/build_web.py` builds the wheel with `uv build` and writes it, plus `engine/engine.json`, into the web build:

```json
{
  "name": "enreal",
  "version": "0.1.0",
  "wheel": { "file": "enreal-0.1.0-py3-none-any.whl", "sha256": "..." },
  "pyodide": { "version": "314.0.7", "indexURL": "https://cdn.jsdelivr.net/pyodide/v314.0.7/full/", "packages": ["numpy", "opencv-python", "pydantic"] }
}
```

- Without `--out` it builds in place into `web/engine/` (gitignored), for local development with either a static server or `server/`.
- With `--out DIR` it assembles a deployable copy and stamps every local `.js` and `.css` URL in the HTML with `?v=<build id>`, a hash of the whole site. `backend.js` reads its own stamp and applies it to `worker.js`, so a cached script can never pair with a newer page. GitHub Pages caches files for 10 minutes, so without this a returning visitor could get a mismatched page and script.
- `--pyodide-index-url` loads Pyodide from somewhere other than jsDelivr, for a fully self-hosted or offline copy.
- The Pyodide release is pinned once, in `pyproject.toml` under `[tool.enreal.web]`. The Node test harness pins the same version, and a test checks they agree.

`pages.yml` runs the build on every push to `main` and deploys it to GitHub Pages, served at <https://coronring.github.io/EnReal/>. Because `coronring.github.io` is a user site with no custom domain, any Pages-enabled project repository appears under it at `/<Repo>/`. The personal site only needs a link, and neither repository depends on the other's stack.

Phaser loads from jsDelivr with a Subresource Integrity hash, so a tampered CDN copy will not run.

## 8. Parity between the two engines

| | Server and tests | Browser |
|---|---|---|
| Python | 3.12 to 3.14 | 3.14.2 (Pyodide 314.0.7) |
| OpenCV | `opencv-python-headless` 4.11.0.86, pinned in the dev group | `opencv-python` 4.11.0.86 |
| NumPy | latest | 2.4.6 |

`tests/test_pyodide_parity.py` runs the wheel under Pyodide in Node through the exact Python glue in `worker.js` (read from the file, not copied), and compares the results with CPython:

- **PNG and WebP match byte for byte**, including the `"ui"` preset and downscaled input. WebP matches because both builds decode with the same libwebp.
- **JPEG cannot match exactly.** CPython's OpenCV wheel bundles libjpeg-turbo 3.0.3 and Pyodide's OpenCV uses a different JPEG library, so about 7.5% of decoded pixels differ by up to 25 levels on the test image. The test requires identical bounds, spawn, metadata and curve count, and every terrain pixel within 2 px of the other engine's terrain. For play, the two are equivalent.

The published package accepts any OpenCV from 4.10 up. OpenCV 5.0 changed the shape `HoughLinesP` returns, from `(N, 1, 4)` to `(N, 4)`, and the engine reshapes to handle both. The suite passes on 4.11 and 5.0, and the `latest-opencv` CI job re-checks it against each new release.

## 9. Download and speed

Measured on 2026-09-28 by downloading each file from jsDelivr for Pyodide 314.0.7:

| File | Size |
|---|---|
| `pyodide.asm.wasm` | 9.2 MB (3.4 MB compressed in transfer) |
| `python_stdlib.zip` | 2.4 MB |
| `pyodide.asm.mjs` | 1.2 MB |
| `opencv-python` 4.11.0.86 | 10.2 MB |
| `numpy` 2.4.6 | 2.8 MB |
| `pydantic`, `pydantic_core` and three small dependencies | 1.8 MB |
| enreal wheel | 24 KB |
| Total | 27.5 MB raw, about 22 MB transferred |

scikit-image is deliberately not loaded; it would add SciPy and Matplotlib. The server environment does not install it either, so both engines take the same code path.

Timings from a headless Chrome run on 2026-09-28, on the development machine:

| Input | In-browser engine | Server (native CPython) |
|---|---|---|
| Engine load, first visit | about 4 s | none |
| 960x540 synthetic stage | 0.6 s | 0.3 s |
| 1280x720 artwork (4.7 MB PNG source) | 1.4 s | 0.5 s |

## 10. Security

- Browser mode sends nothing anywhere; the image stays on the device.
- The server caps uploads at 30 MB and rejects them before decoding, validates every parameter range (for example `max_dim` 64 to 4096), runs the CPU-bound engine off the event loop, returns 400 for bad input and a generic 500 for anything else, and allows cross-site calls only from origins listed in `ENREAL_CORS_ORIGINS`.
- The server sends `Cache-Control: no-cache` for pages and assets and `no-store` for `/api/*`. Starlette's static files otherwise send an ETag with no caching policy, and browsers then guess a lifetime.
- The worker verifies the engine wheel's SHA-256 before installing it. Phaser is pinned with Subresource Integrity.
- No API keys exist anywhere in the project.

## 11. Known gaps and next steps

- **Spawn is often inside or under the terrain.** With no straight platforms, the spawn falls back to 90% of the image height, which on most images is below the traced ground, so the player drops to the bottom of the world. A fix is to spawn near the top of a column and let the player fall onto the first red pixel, or to pick the topmost terrain point near the horizontal center. This changes engine output, so it needs a minor version bump.
- **Reserved parameters.** `min_platform_len_pct`, `max_platforms` and `text_suppression` do nothing yet (section 3). Either wire in straight-platform detection and text suppression, or remove them before 1.0.
- **Unused helpers** in `processing.py` (section 3) should either be connected or dropped.
- **JPEG parity** is approximate (section 8). Decoding in JavaScript and handing PNG bytes to both engines would not help, because the browser's JPEG decoder differs from both.
- **The NPC chat** is a mock (section 5).
- Earlier plans that still apply: an overlay editor for correcting detected terrain, caching by input hash on the server, and a command line tool for batch processing.
