"""Optional HTTP server for EnReal.

Serves the static game from ``web/`` and exposes the engine at ``POST /api/process``.
The game does not need it: in-browser mode runs the same engine under Pyodide. Use it
for local development, or to parse on a server for clients that should not download the
engine. See ``docs/design.md``.

Run from the repository root::

    uv run python -m server
"""

from __future__ import annotations

import logging
import os
from collections.abc import Awaitable, Callable
from pathlib import Path

from fastapi import FastAPI, File, HTTPException, Query, Request, Response, UploadFile
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from starlette.concurrency import run_in_threadpool

import enreal
from enreal import Scene, StageResult, generate_skeleton

logger = logging.getLogger("enreal.server")

MAX_UPLOAD_BYTES = 30 * 1024 * 1024
"""Largest accepted upload. Anything bigger is rejected before it is decoded."""

WEB_DIR = Path(__file__).resolve().parent.parent / "web"
"""Static game served at ``/``."""


def _cors_origins() -> list[str]:
    """Origins allowed to call the API cross-site, from ``ENREAL_CORS_ORIGINS`` (comma separated).

    Empty by default, so only pages served by this server can call it. Set it to let a
    static copy of the game hosted elsewhere (for example GitHub Pages) use this server.
    """
    raw = os.getenv("ENREAL_CORS_ORIGINS", "")
    return [origin.strip() for origin in raw.split(",") if origin.strip()]


def create_app(web_dir: Path | None = WEB_DIR) -> FastAPI:
    """Build the FastAPI application.

    Args:
        web_dir: Directory served as the static game at ``/``. ``None`` or a missing
            directory serves the API only.

    Returns:
        The configured application.
    """
    app = FastAPI(
        title="EnReal",
        version=enreal.__version__,
        description="Turn an image into a 2D platformer stage.",
    )

    origins = _cors_origins()
    if origins:
        app.add_middleware(
            CORSMiddleware,
            allow_origins=origins,
            allow_methods=["GET", "POST"],
            allow_headers=["*"],
        )

    @app.middleware("http")
    async def cache_control(request: Request, call_next: Callable[[Request], Awaitable[Response]]) -> Response:
        # StaticFiles sends an ETag but no Cache-Control, so browsers guess a lifetime and
        # can pair a cached game.js with a fresh game.html. Force revalidation instead.
        response = await call_next(request)
        if request.url.path.startswith("/api/"):
            response.headers.setdefault("Cache-Control", "no-store")
        else:
            response.headers.setdefault("Cache-Control", "no-cache")
        return response

    @app.get("/api/health")
    def health() -> dict[str, str]:
        """Liveness probe. The web client also uses it to detect that a server is present."""
        return {"status": "ok", "engine": "enreal", "version": enreal.__version__}

    @app.post("/api/process", response_model=StageResult)
    async def process(
        file: UploadFile = File(..., description="Image (PNG, JPEG, WebP, ...)"),
        max_dim: int = Query(1280, ge=64, le=4096, description="Downscale the longest side to this many pixels."),
        grid_size: int = Query(8, ge=1, le=64, description="Snap output points to this pixel grid."),
        min_platform_len_pct: float = Query(
            0.22, ge=0.0, le=1.0, description="Minimum platform length, fraction of width."
        ),
        max_platforms: int = Query(200, ge=0, le=1000, description="Maximum platforms kept after merging."),
        scene: Scene = Query("art", description="Detection preset."),
        rectify_perspective: bool = Query(True, description="ui preset only"),
        text_suppression: bool = Query(True, description="ui preset only"),
    ) -> StageResult:
        """Run the engine on an uploaded image and return the stage."""
        content = await file.read(MAX_UPLOAD_BYTES + 1)
        if not content:
            raise HTTPException(status_code=400, detail="Empty file uploaded.")
        if len(content) > MAX_UPLOAD_BYTES:
            raise HTTPException(status_code=413, detail=f"File too large (max {MAX_UPLOAD_BYTES // (1024 * 1024)} MB).")

        logger.info("Processing %s (%d bytes, scene=%s)", file.filename, len(content), scene)
        try:
            # The engine is CPU bound; keep it off the event loop.
            return await run_in_threadpool(
                generate_skeleton,
                content,
                max_dim=max_dim,
                grid_size=grid_size,
                min_platform_len_pct=min_platform_len_pct,
                max_platforms=max_platforms,
                scene=scene,
                rectify_perspective=rectify_perspective,
                text_suppression=text_suppression,
            )
        except ValueError as e:
            raise HTTPException(status_code=400, detail=str(e)) from e
        except Exception as e:
            logger.exception("Engine failed")
            raise HTTPException(status_code=500, detail="Processing failed.") from e

    if web_dir is not None and web_dir.is_dir():
        # Mounted last so the /api routes above take precedence.
        app.mount("/", StaticFiles(directory=web_dir, html=True), name="web")
    else:
        logger.warning("No web directory at %s; serving the API only.", web_dir)

    return app


app = create_app()
