# app/main.py
from __future__ import annotations

import json
import logging
import os
from pathlib import Path
from typing import Literal, Optional, Tuple
from urllib.parse import quote

from fastapi import FastAPI, File, UploadFile, HTTPException, Query
from fastapi.staticfiles import StaticFiles

from src.backend.processing import generate_skeleton
from src.backend.models import ProcessResponse
from src.backend.novel import generate_intro_story_from_image, build_vn_pack

try:
    from .utils import configure_logging
except Exception:
    def configure_logging() -> None:
        logging.basicConfig(level=logging.INFO, format="%(asctime)s | %(levelname)-8s | %(name)s | %(message)s")

# Configure logging early
configure_logging()
logger = logging.getLogger("enreal")


def _ensure_frontend_static_dir() -> str:
    here = Path(__file__).resolve()
    candidates = [
        here.parent.parent / "frontend",                 # <project>/src/frontend
        here.parent.parent.parent / "frontend",          # <project>/frontend
        Path("./src/frontend").resolve(),
        Path("./frontend").resolve(),
    ]
    for p in candidates:
        if p.is_dir() and (p / "index.html").exists():
            logger.info("Serving frontend from %s", p)
            return str(p)

    # Fallback: create a minimal placeholder so "/" doesn’t 404
    target = here.parent.parent / "frontend"
    target.mkdir(parents=True, exist_ok=True)
    index = target / "index.html"
    if not index.exists():
        index.write_text(
            "<!doctype html><html><body><h1>EnReal</h1><p>Frontend placeholder. Replace with your app.</p></body></html>",
            encoding="utf-8",
        )
        logger.warning("Created placeholder frontend at %s; replace with your index.html", target)
    return str(target)


def _try_find_reference_in_prts(root: Path, preferred_langs: Tuple[str, ...] = ("en", "jp", "cn")) -> Optional[Tuple[Path, str]]:
    """
    Scan a local PRTS dump to find any (audio file, transcript text) pair from voice.json.
    Returns (wav_path, transcript) or None if not found.
    """
    try:
        if not root.exists():
            return None
        for op_dir in root.iterdir():
            if not op_dir.is_dir():
                continue
            for lang in preferred_langs:
                lang_dir = op_dir / lang
                vjson = lang_dir / "voice.json"
                if not vjson.exists():
                    continue
                try:
                    data = json.loads(vjson.read_text(encoding="utf-8"))
                except Exception:
                    continue
                audios = data.get("audio", []) or []
                for a in audios:
                    p = str(a.get("path", "")).strip()
                    t = str(a.get("text", "")).strip()
                    if not p or not t:
                        continue
                    if "." in p:
                        f = lang_dir / p
                        if f.exists():
                            return f, t
                    else:
                        # try common extensions
                        for ext in (".wav", ".ogg", ".mp3"):
                            f = lang_dir / (p + ext)
                            if f.exists():
                                return f, t
        return None
    except Exception as e:
        logger.warning("Failed scanning PRTS root for reference: %s", e)
        return None


def _resolve_story_pack_dir() -> Path:
    """
    Resolve an absolute path to the story_pack directory regardless of server CWD.

    Precedence:
      1) env STORY_PACK_DIR
      2) nearest existing 'story_pack' relative to this file up the tree
      3) CWD/story_pack (created)
    """
    here = Path(__file__).resolve()
    env_dir = os.getenv("STORY_PACK_DIR")
    if env_dir:
        p = Path(env_dir).expanduser().resolve()
        p.mkdir(parents=True, exist_ok=True)
        return p

    candidates = [
        here.parent.parent / "story_pack",                       # <project>/src/story_pack
        here.parent.parent.parent / "story_pack",                # <project>/story_pack
        here.parent.parent.parent.parent / "story_pack",         # src/story_pack
        here.parent.parent.parent.parent.parent / "story_pack",  # repo_root/story_pack
        Path.cwd() / "story_pack",                               # CWD/story_pack
    ]
    for p in candidates:
        try:
            if p.exists() and p.is_dir():
                return p.resolve()
        except Exception:
            continue
    # Fallback to CWD
    fallback = (Path.cwd() / "story_pack").resolve()
    fallback.mkdir(parents=True, exist_ok=True)
    return fallback


def _ensure_manifest_for_pack(pack_dir: Path) -> None:
    """Create a minimal manifest.json in the given pack directory if missing.

    This helps older packs created before manifest support avoid 404s when the
    frontend requests /story_pack/<pack_id>/manifest.json.
    """
    try:
        if not pack_dir.is_dir():
            return
        man = pack_dir / "manifest.json"
        if man.exists():
            return
        pack_id = pack_dir.name
        minimal = {
            "meta": {"title": pack_id},
            "lines": [
                {"speaker": "", "text": "This story pack had no manifest; a placeholder has been generated."},
                {"speaker": "Guide", "text": "Re-generate the pack to create a full script with audio if desired."},
            ],
        }
        man.write_text(json.dumps(minimal, ensure_ascii=False, indent=2), encoding="utf-8")
        logger.info("Created placeholder manifest for pack %s", pack_id)
    except Exception as e:
        logger.warning("Failed to create placeholder manifest in %s: %s", pack_dir, e)


app = FastAPI(
    title="EnReal + VN Intro Service",
    description="Generate a 2D platformer skeleton from a screenshot, auto-generate a VN intro, and play both.",
    version="2.1.0",
)

# Resolve story packs directory once
STORY_PACK_DIR: Path = _resolve_story_pack_dir()
logger.info("Story packs directory resolved to: %s", STORY_PACK_DIR)


@app.get("/api/packs")
def list_story_packs() -> dict:
    """Return a list of available VN packs under story_pack/.

    Each entry: { pack_id, manifest_url, title }
    Title is best-effort from manifest.json if present.
    """
    root = STORY_PACK_DIR
    items: list[dict] = []
    try:
        if root.exists():
            for child in sorted([p for p in root.iterdir() if p.is_dir()]):
                pack_id = child.name
                man = child / "manifest.json"
                title = ""
                if man.exists():
                    try:
                        data = json.loads(man.read_text(encoding="utf-8"))
                        title = data.get("meta", {}).get("title") or data.get("title") or ""
                    except Exception:
                        title = ""
                else:
                    # Proactively create a minimal manifest so the VN player can load something.
                    _ensure_manifest_for_pack(child)
                # Always URL-encode the directory name for safety in paths
                encoded = quote(pack_id, safe="")
                items.append({
                    "pack_id": pack_id,
                    "manifest_url": f"/story_pack/{encoded}/manifest.json",
                    "title": title,
                })
    except Exception as e:
        logger.warning("Failed to list story packs: %s", e)
    return {"packs": items}


@app.get("/api/debug/story_root")
def debug_story_root() -> dict:
    """Return the resolved story_pack directory and immediate children for debugging."""
    root = STORY_PACK_DIR
    try:
        children = [p.name + ("/" if p.is_dir() else "") for p in sorted(root.iterdir())]
    except Exception:
        children = []
    return {
        "story_pack_dir": str(root),
        "exists": root.exists(),
        "children": children,
    }


@app.post("/api/process", response_model=ProcessResponse)
async def process_image(
    file: UploadFile = File(..., description="Image (png/jpg/webp)"),
    max_dim: int = Query(1280, description="Downscale largest side to this many pixels before processing."),
    grid_size: int = Query(8, description="Grid snapping size in pixels for output geometry."),
    min_platform_len_pct: float = Query(0.22, ge=0.0, le=1.0, description="Minimum platform length as fraction of image width."),
    max_platforms: int = Query(200, description="Maximum number of platforms to keep after merging."),
    scene: Literal["ui", "art"] = Query("art", description="Detection preset."),
    rectify_perspective: bool = Query(True, description="ui preset only"),
    text_suppression: bool = Query(True, description="ui preset only"),
) -> ProcessResponse:
    """
    Returns:
    - skeleton: Dict
    - overlay_image_b64: str (data URL)
    - pack_id: Optional[str]
    - pack_base_url: Optional[str]
    - manifest_url: Optional[str]

    The server auto-generates a short VN intro with gpt-5 and tries to synthesize line audio
    via Boson voice cloning if VOICE_PROVIDER=boson and reference+key are available.
    """
    content = await file.read()
    if not content:
        raise HTTPException(status_code=400, detail="Empty file uploaded.")
    if len(content) > 30 * 1024 * 1024:
        raise HTTPException(status_code=413, detail="File too large (max 30MB).")

    logger.info("Received file: name=%s size=%d bytes", file.filename, len(content))

    try:
        result = generate_skeleton(
            image_bytes=content,
            max_dim=max_dim,
            grid_size=grid_size,
            min_platform_len_pct=min_platform_len_pct,
            max_platforms=max_platforms,
            scene=scene,
            rectify_perspective=rectify_perspective,
            text_suppression=text_suppression,
        )
    except Exception as e:
        logger.exception("Unhandled error during skeleton generation")
        raise HTTPException(status_code=500, detail=f"Processing failed: {e}") from e

    # Voice config
    voice_provider = os.getenv("VOICE_PROVIDER", "silence").lower()
    boson_api_key = os.getenv("BOSON_API_KEY")  # required if provider = boson
    ref_path_env = os.getenv("BOSON_REFERENCE_PATH")
    ref_trans_env = os.getenv("BOSON_REFERENCE_TRANSCRIPT")

    reference_wav_path: Optional[Path] = Path(ref_path_env).expanduser() if ref_path_env else None
    reference_transcript: Optional[str] = ref_trans_env if ref_trans_env else None

    if voice_provider == "boson" and (not reference_wav_path or not reference_transcript):
        # attempt to auto-find a reference in local PRTS root
        prts_root_env = os.getenv("LOCAL_PRTS_ROOT")
        if prts_root_env:
            found = _try_find_reference_in_prts(Path(prts_root_env).expanduser())
            if found:
                reference_wav_path, reference_transcript = found
                logger.info("Auto-selected reference voice from PRTS: %s", reference_wav_path)

    # VN generation (best-effort)
    pack_id: str | None = None
    pack_base_url: str | None = None
    manifest_url: str | None = None
    try:
        story = generate_intro_story_from_image(content, model="gpt-4o")
        pack_id, pack_dir, manifest = build_vn_pack(
            story,
            pack_root=STORY_PACK_DIR,
            voice_provider=voice_provider,
            reference_wav_path=reference_wav_path,
            reference_transcript=reference_transcript,
            boson_api_key=boson_api_key,
        )
        # Use URL-encoded pack id for web paths to avoid 404s with spaces/plus signs
        if pack_id:
            encoded_pack_id = quote(pack_id, safe="")
            pack_base_url = f"/story_pack/{encoded_pack_id}"
            manifest_url = f"{pack_base_url}/manifest.json"
        # Safety: ensure manifest.json exists for the VN player fetch
        man_path = Path(pack_dir) / "manifest.json"
        if not man_path.exists():
            try:
                man_path.write_text(json.dumps(manifest, indent=2, ensure_ascii=False), encoding="utf-8")
                logger.info("Wrote missing VN manifest at %s", man_path)
            except Exception as e:
                logger.warning("Failed to ensure VN manifest on disk: %s", e)
        logger.info("Built VN pack: %s at %s (manifest: %s)", pack_id, pack_dir, manifest_url)
    except Exception as e:
        logger.warning("VN pack generation failed: %s", e)

    return ProcessResponse(
        skeleton=result.skeleton if hasattr(result, "skeleton") else result["skeleton"],  # type: ignore
        overlay_image_b64=result.overlay_image_b64 if hasattr(result, "overlay_image_b64") else result["overlay_image_b64"],  # type: ignore
        pack_id=pack_id,
        pack_base_url=pack_base_url,
        manifest_url=manifest_url,
    )


# Mount static frontend
static_dir = _ensure_frontend_static_dir()
app.mount("/", StaticFiles(directory=static_dir, html=True), name="static")

# Serve story packs from resolved absolute directory
STORY_PACK_DIR.mkdir(parents=True, exist_ok=True)
logger.info("Mounting /story_pack from %s", STORY_PACK_DIR)
app.mount("/story_pack", StaticFiles(directory=str(STORY_PACK_DIR), html=False), name="story_pack")


def main() -> None:
    """
    Start the FastAPI service using uvicorn.

    How to run:
      - pip install fastapi uvicorn litellm pillow orjson python-dotenv openai
      - set OPENAI_API_KEY for gpt-5 via LiteLLM
      - optional voice cloning:
          set VOICE_PROVIDER=boson
          set BOSON_API_KEY=...
          set BOSON_REFERENCE_PATH=/path/to/ref.wav
          set BOSON_REFERENCE_TRANSCRIPT="reference transcript text"
        or set LOCAL_PRTS_ROOT to auto-pick a reference.

      python -m app.main
    """
    import uvicorn
    # Enable autoreload by default when launched directly. Override with UVICORN_RELOAD=0 to disable.
    import os as _os
    _reload = _os.getenv("UVICORN_RELOAD", "1") not in ("0", "false", "False")
    _host = _os.getenv("HOST", "127.0.0.1")
    _port = int(_os.getenv("PORT", "8000"))
    # Use import string so uvicorn's reloader can import the app in a child process on Windows.
    uvicorn.run("src.backend.main:app", host=_host, port=_port, reload=_reload, log_level="info")

if __name__ == "__main__":
    main()