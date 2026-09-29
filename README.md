# EnReal

Turn any image into a 2D platformer stage. EnReal traces the edges in a screenshot or a piece of art, and those edges become the ground you run and jump on.

**[Play it in your browser](https://coronring.github.io/EnReal/)**: pick an image, parse it, press Play. Nothing is uploaded; the engine runs on your device.

This repository holds two things:

- **The engine**, published on PyPI as [`enreal`](https://pypi.org/project/enreal/): a rule-based computer vision pipeline (OpenCV, no machine learning) that turns image bytes into a stage skeleton plus a terrain overlay. It depends only on NumPy, OpenCV and Pydantic.
- **The game**, a static web app built on Phaser 3. It runs the same Python engine either inside the browser (under [Pyodide](https://pyodide.org)) or on an optional server, and shows no difference between the two.

## Use the engine from Python

```bash
pip install enreal
```

```python
from pathlib import Path

from enreal import generate_skeleton

result = generate_skeleton(Path("screenshot.png").read_bytes(), scene="art")

print(result.skeleton.bounds)          # x=0 y=0 width=1280 height=720
print(len(result.skeleton.curves))     # terrain polylines traced from the image
result.overlay_image_b64               # "data:image/png;base64,...", edges painted pure red
Path("stage.json").write_text(result.model_dump_json())
```

| Argument | Default | Effect |
|---|---|---|
| `scene` | `"ui"` | `"art"` for scenes and artwork, `"ui"` for screenshots and documents |
| `max_dim` | `1280` | Longest side after downscaling; all coordinates are in this space |
| `grid_size` | `8` | Output points snap to this pixel grid |
| `rectify_perspective` | `True` | `"ui"` only: warp a dominant quadrilateral to top-down first |

`generate_skeleton` raises `ValueError` for bad parameters or bytes OpenCV cannot decode. The engine is pure: it never reads files, touches the network or reads environment variables.

The package depends on `opencv-python-headless`. If your environment already has `opencv-python` or `opencv-contrib-python`, keep only one of them installed, since they all provide the same `cv2` module.

The full data contract and the pipeline are described in [docs/design.md](docs/design.md).

## Play and controls

| Key | Action |
|---|---|
| A / D | Move |
| W or Space | Jump (double jump available) |
| S | Drop through a platform |
| Shift | Sprint |
| J | Fire, or talk to an NPC when next to one |
| K | Place or detonate a bomb |
| G | Throw or detonate a grenade |
| H | Paint terrain under the player |

Pure red pixels in the overlay are solid ground. Explosions erase them and painting adds them.

## Run it locally

The project uses [uv](https://docs.astral.sh/uv/). From the repository root:

```bash
uv sync
```

**Browser engine only, no server** (how the live site works):

```bash
uv run python scripts/build_web.py     # builds the engine wheel into web/engine/
python -m http.server 8000 -d web      # any static file server works
```

Open <http://127.0.0.1:8000/>. The page finds no server, so it loads the engine into the browser. The first visit downloads about 22 MB (Python, NumPy, OpenCV, Pydantic), which the browser then caches. Opening `index.html` straight from disk does not work, because browsers refuse to load WebAssembly from `file://`.

**With the optional server**:

```bash
uv run python -m server
```

Open <http://127.0.0.1:8000/>. The page detects the server and parses there. Add `?backend=local` to the URL to force the in-browser engine anyway, or use the Engine picker on the page.

| Variable | Default | Purpose |
|---|---|---|
| `HOST`, `PORT` | `127.0.0.1`, `8000` | Bind address |
| `UVICORN_RELOAD` | `0` | `1` reloads on code changes |
| `ENREAL_LOGLEVEL` | `INFO` | `DEBUG`, `INFO`, `WARNING` or `ERROR` |
| `ENREAL_CORS_ORIGINS` | empty | Comma-separated origins allowed to call the API from another site |

To point a static copy of the game at a server elsewhere, open it with `?server=https://your-server/` and list the page's origin in that server's `ENREAL_CORS_ORIGINS`.

## Development

```bash
uv run pytest                                          # engine, server and packaging tests
uv run ruff check src server scripts tests
uv run ruff format --check src server scripts tests
uv run mypy
```

The Pyodide parity test runs the wheel under Pyodide in Node and checks it against CPython. It needs Node 18 or newer:

```bash
cd tests/pyodide && npm ci && cd ../..
ENREAL_PYODIDE=1 uv run pytest tests/test_pyodide_parity.py
```

Without `npm ci` that test is skipped, and CI always runs it.

## Releasing

The version lives in `src/enreal/__init__.py` and nowhere else.

1. Bump `__version__` and add a `CHANGELOG.md` entry.
2. Commit, then tag and push: `git tag v0.1.1 && git push origin v0.1.1`.
3. `.github/workflows/release.yml` checks that the tag matches the version, runs the tests, builds, and publishes to PyPI through Trusted Publishing. Nothing is ever uploaded from a local machine.
4. Create the GitHub release for the tag.

The website redeploys from `main` on every push through `.github/workflows/pages.yml`, independently of PyPI releases.

## License

[MIT](LICENSE)
