# app/novel.py
from __future__ import annotations

import base64
import io
import json
import logging
import os
import re
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from PIL import Image, ImageDraw, ImageFont

try:
    from litellm import completion as llm_completion  # type: ignore
except Exception as e:
    raise ImportError("litellm is required for story generation. pip install litellm") from e

from voice_synth import BosonVoiceSynth, write_wav

LOGGER = logging.getLogger("enreal.novel")


def _b64_data_url(image_bytes: bytes, mime: str = "image/png") -> str:
    b64 = base64.b64encode(image_bytes).decode("ascii")
    return f"data:{mime};base64,{b64}"


def _safe_slug(text: str, max_len: int = 48) -> str:
    s = re.sub(r"[^\w\s-]", "", text, flags=re.UNICODE).strip().lower()
    s = re.sub(r"[-\s]+", "-", s)
    return s[:max_len] or "unnamed"


def _ensure_dir(p: Path) -> None:
    p.mkdir(parents=True, exist_ok=True)


def _save_png(img: Image.Image, path: Path) -> None:
    _ensure_dir(path.parent)
    img.save(str(path), format="PNG")


def _gen_grad_bg(size: Tuple[int, int] = (1280, 720)) -> Image.Image:
    w, h = size
    img = Image.new("RGB", size, "#101018")
    draw = ImageDraw.Draw(img)
    for y in range(h):
        c = int(16 + (y / h) * 80)
        draw.line([(0, y), (w, y)], fill=(c, c, c + 12))
    vign = Image.new("L", size, 0)
    vdraw = ImageDraw.Draw(vign)
    vdraw.ellipse([(-w * 0.3, -h * 0.3), (w * 1.3, h * 1.3)], fill=220)
    img.putalpha(vign)
    return img.convert("RGB")


def _portrait_placeholder(name: str, size: int = 512) -> Image.Image:
    """
    Create a round placeholder portrait with centered initials.
    Uses ImageDraw.textbbox for modern Pillow; falls back to textsize if needed.
    """
    img = Image.new("RGBA", (size, size), (30, 30, 40, 255))
    draw = ImageDraw.Draw(img)
    # simple circle avatar with random-ish color
    rnd = sum(ord(c) for c in name) % 255
    color = (80 + rnd % 120, 90 + rnd % 100, 160 + rnd % 80, 255)
    r = size // 2 - 12
    draw.ellipse([(size // 2 - r, size // 2 - r), (size // 2 + r, size // 2 + r)], fill=color)

    initials = "".join([p[0].upper() for p in name.split() if p][:2]) or "?"
    try:
        font = ImageFont.truetype("arial.ttf", size // 4)
    except Exception:
        font = ImageFont.load_default()

    # Compute text size and a robust center position
    try:
        # Preferred for Pillow >= 8.0
        bbox = draw.textbbox((0, 0), initials, font=font)
        tw = max(1, bbox[2] - bbox[0])
        th = max(1, bbox[3] - bbox[1])
        tx = (size - tw) / 2 - bbox[0]
        ty = (size - th) / 2 - bbox[1]
    except AttributeError:
        # Fallback for older Pillow
        tw, th = draw.textsize(initials, font=font)  # type: ignore[attr-defined]
        tx = (size - tw) / 2
        ty = (size - th) / 2

    draw.text((tx, ty), initials, fill=(240, 240, 252, 240), font=font)
    return img.convert("RGB")


def _generate_silent_wav(path: Path, duration_sec: float = 1.0, framerate: int = 16000) -> None:
    import wave, struct
    nframes = int(framerate * max(0.2, duration_sec))
    _ensure_dir(path.parent)
    with wave.open(str(path), "w") as wf:
        wf.setnchannels(1)
        wf.setsampwidth(2)
        wf.setframerate(framerate)
        zero = struct.pack("<h", 0)
        for _ in range(nframes):
            wf.writeframesraw(zero)


def _default_story_fallback() -> Dict[str, Any]:
    chars = [
        {"name": "Alex", "visual_description": "Young survivor with a torn jacket and determined gaze."},
        {"name": "Mira", "visual_description": "Alex's sister, agile and focused, carrying a small backpack."},
    ]
    script = [
        {"type": "background", "file_name": "bg_000.png", "description": "Dusky city skyline, smoke on the horizon."},
        {"type": "line", "name": "Alex", "text": "Keep moving, Mira. Streets are empty, but it won't stay that way."},
        {"type": "line", "name": "Mira", "text": "I hear drones. And... that glow again. It's getting closer."},
        {"type": "line", "name": "Alex", "text": "We cut through the old plaza. If anything jumps us... we run."},
        {"type": "line", "name": "Mira", "text": "And if we can't run?"},
        {"type": "line", "name": "Alex", "text": "Then we fight. Together."},
    ]
    return {"title": "The Ember Path", "characters": chars, "script": script}


SYSTEM_PROMPT = """
You are a creative scene writer. Given a single image of a game setting, write a short intro story with multiple characters that
happens in that setting. Output strictly valid JSON following the schema below. Keep it compact and playable in under 90 seconds.

Requirements:
- 2 to 4 named characters, each with a visual_description for portrait generation.
- Tone: cinematic but crisp. No more than 25 lines.
- Structure: start with a background cue, then dialogue lines alternating naturally between characters.
- Avoid violence gore; keep it PG-13. English only.

JSON schema:
{
  "title": str,
  "characters": [
    { "name": str, "visual_description": str }
  ],
  "script": [
    { "type": "background", "file_name": "bg_000.png", "description": str },
    { "type": "line", "name": str, "text": str },
    ...
  ]
}
Only produce JSON. No markdown, no comments.
""".strip()


def generate_intro_story_from_image(image_bytes: bytes, model: str = "gpt-4o") -> Dict[str, Any]:
    data_url = _b64_data_url(image_bytes)
    user_content = [
        {"type": "text", "text": "Write a short playable intro in the given schema."},
        {"type": "image_url", "image_url": {"url": data_url}},
    ]
    try:
        resp = llm_completion(
            model=model,
            messages=[
                {"role": "system", "content": SYSTEM_PROMPT},
                {"role": "user", "content": user_content},
            ],
            temperature=1.0,
            max_tokens=800,
        )
        content = resp["choices"][0]["message"]["content"]
        if not isinstance(content, str) or not content.strip():
            raise ValueError("empty LLM content")

        text = content.strip()
        try:
            data = json.loads(text)
        except Exception:
            start = text.find("{")
            end = text.rfind("}")
            if start >= 0 and end > start:
                data = json.loads(text[start : end + 1])
            else:
                raise

        chars = data.get("characters") or []
        script = data.get("script") or []
        if not chars or not script:
            raise ValueError("missing characters/script")

        if script[0].get("type") != "background":
            script = [{"type": "background", "file_name": "bg_000.png", "description": "A dramatic establishing shot."}] + script

        bg = script[0:1]
        lines = [s for s in script[1:] if s.get("type") == "line"]
        script = bg + lines[:25]

        if not any(s.get("type") == "line" for s in script):
            raise ValueError("no dialogue lines after trimming")

        title = (data.get("title") or "Untitled Scene").strip()
        return {"title": title, "characters": chars, "script": script}
    except Exception as e:
        LOGGER.warning("Story generation failed, using fallback: %s", e)
        return _default_story_fallback()


def build_vn_pack(
    story: Dict[str, Any],
    *,
    pack_root: Path = Path("story_pack"),
    voice_provider: str = "silence",
    reference_wav_path: Optional[Path] = None,
    reference_transcript: Optional[str] = None,
    boson_api_key: Optional[str] = None,
) -> Tuple[str, Path, Dict[str, Any]]:
    """
    Build a VN pack on disk.

    - Creates story_pack/<pack_id>/
      - backgrounds/bg_000.png (gradient)
      - characters/<slug>.png (placeholder portraits)
      - voices/<slug>/<i>.wav (per line; either 1s silence or cloned voice)
      - manifest.json

    Parameters:
      voice_provider: "silence" (default) or "boson"
      reference_wav_path: path to short reference audio
      reference_transcript: transcript text for the reference audio
      boson_api_key: API key when using voice_provider="boson"

    Returns: (pack_id, pack_dir, manifest)
    """
    _ensure_dir(pack_root)
    chars = story.get("characters", [])
    script = story.get("script", [])

    names = [_safe_slug(c.get("name", "")) for c in chars if c.get("name")]
    base = "+".join(sorted(set([n for n in names if n]))).strip("+") or "scene"
    pack_id = base[:80]
    pack_dir = pack_root / pack_id
    _ensure_dir(pack_dir)

    bg_dir = pack_dir / "backgrounds"
    ch_dir = pack_dir / "characters"
    voice_dir = pack_dir / "voices"
    _ensure_dir(bg_dir)
    _ensure_dir(ch_dir)
    _ensure_dir(voice_dir)

    _save_png(_gen_grad_bg((1280, 720)), bg_dir / "bg_000.png")

    portrait_map: Dict[str, str] = {}
    for c in chars:
        name = c.get("name", "Unnamed")
        slug = _safe_slug(name)
        img = _portrait_placeholder(name)
        dst = ch_dir / f"{slug}.png"
        _save_png(img, dst)
        portrait_map[name] = f"characters/{dst.name}"

    char_line_counts: Dict[str, int] = {}
    char_voice_map: Dict[str, List[str]] = {c.get("name", ""): [] for c in chars}

    # Initialize voice renderer
    boson: Optional[BosonVoiceSynth] = None
    use_boson = voice_provider.lower() == "boson" and reference_wav_path and reference_transcript and boson_api_key
    if use_boson:
        try:
            boson = BosonVoiceSynth(api_key=str(boson_api_key))
        except Exception as e:
            LOGGER.warning("Failed to initialize BosonVoiceSynth, falling back to silence: %s", e)
            boson = None
            use_boson = False

    for s in script:
        if s.get("type") != "line":
            continue
        speaker = s.get("name", "")
        if not speaker:
            continue
        idx = char_line_counts.get(speaker, 0)
        slug = _safe_slug(speaker)
        wav_rel = f"voices/{slug}/{idx}.wav"
        wav_abs = voice_dir / slug / f"{idx}.wav"

        if use_boson and boson is not None:
            try:
                wav_bytes = boson.synthesize(
                    text=s.get("text", ""),
                    reference_wav_path=Path(reference_wav_path),
                    reference_transcript=str(reference_transcript),
                )
                write_wav(wav_abs, wav_bytes)
            except Exception as e:
                LOGGER.warning("Boson synth failed for '%s' line %d. Falling back to silence. (%s)", speaker, idx, e)
                _generate_silent_wav(wav_abs, 1.0)
        else:
            _generate_silent_wav(wav_abs, 1.0)

        char_line_counts[speaker] = idx + 1
        char_voice_map.setdefault(speaker, []).append(wav_rel)

    base_url = f"/story_pack/{pack_id}"
    manifest_chars: List[Dict[str, Any]] = []
    for c in chars:
        name = c.get("name", "")
        manifest_chars.append({
            "character_name": name,
            "visual_description": c.get("visual_description", ""),
            "portrait": portrait_map.get(name, ""),
            "voices": char_voice_map.get(name, []),
        })

    manifest = {
        "pack_id": pack_id,
        "base_url": base_url,
        "script": script,
        "characters": manifest_chars,
        "assets": {
            "backgrounds": [{"file_name": "bg_000.png", "path": "backgrounds/bg_000.png"}],
            "portraits": [{"character_name": n, "path": p} for n, p in portrait_map.items()],
        },
        "meta": {"title": story.get("title", "")},
    }

    (pack_dir / "manifest.json").write_text(json.dumps(manifest, indent=2, ensure_ascii=False), encoding="utf-8")
    return pack_id, pack_dir, manifest