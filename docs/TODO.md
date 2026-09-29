# TODO

## Gameplay bugs

- Player spawns inside or under the terrain: the engine's spawn falls back to 90% of the image height (see design.md section 11)
- Grenade is too bouncy
- Grenade is too fast and sometimes goes through walls
- Black spot remains when a bomb explodes
- NPC falls through the floor
- Some obvious lines do not render
- Close button floats around; its hit area is the top-left corner
- Add short jump and long jump

## Gameplay features

- Better playability overall
- Stage selection: bundled stage packs, and a cache so the same image is not parsed twice
- Mobile control mode
- Maybe some areas can be thicker; some solid lines
- Do something with borders and boundaries
- AI-generated inventory items, as pixel art so they look less AI-like
- Blockbench-like editable interface for the terrain
- A few sample images on the start page so a visitor can play without finding one

## Engine

- Straight-platform detection, so `platforms`, `barriers` and spawn selection carry real data and `min_platform_len_pct` and `max_platforms` take effect
- Wire in text suppression (`_text_mask`) for the `"ui"` preset, or remove the parameter
- Connect or drop the unused helpers in `processing.py`
- Parallel processing for large images
- A command line tool for batch processing

## Later

- Story and voice: redesign from scratch
- NPC chat backed by a real model (the chat panel currently echoes)
- Audio core and a video play mode
- A proper landing page
