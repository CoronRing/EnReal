# Changelog

All notable changes to the `enreal` package and the web game. The package follows [Semantic Versioning](https://semver.org/).

## 0.1.0

First release.

- `enreal` engine package: `generate_skeleton(image_bytes, ...)` returns a typed `StageResult` (skeleton plus red terrain overlay). Depends only on NumPy, OpenCV (headless) and Pydantic, and supports OpenCV 4.10 through 5.x.
- Invalid parameters and undecodable images raise `ValueError`; a failed edge extraction raises `RuntimeError` instead of returning an empty stage.
- The engine configures no logging handlers.
- Web game runs the same engine in the browser under Pyodide, or on the optional FastAPI server in `server/`, chosen automatically or with `?backend=local|remote`.
- Static build (`scripts/build_web.py`) with a SHA-256-verified engine wheel and cache-busted asset URLs, deployed to GitHub Pages.
