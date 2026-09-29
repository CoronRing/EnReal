"""The browser engine must produce what CPython produces.

Builds the wheel, runs it under Pyodide in Node through the same glue ``web/worker.js``
uses, and compares the results with CPython's.

Lossless inputs (PNG, and WebP, whose decoder is the same libwebp on both sides) must
match byte for byte. JPEG cannot: CPython's OpenCV wheel bundles libjpeg-turbo while
Pyodide's OpenCV uses a different JPEG library, and lossy decoders legitimately disagree
on some pixels. For JPEG the stage structure must match and the terrain must agree to
within two pixels, which is what matters for play.

Needs Node 18+ and ``npm ci`` in ``tests/pyodide``, plus network access for the Pyodide
packages. Skipped when those are missing, unless ``ENREAL_PYODIDE=1`` is set (as in CI),
which turns a missing prerequisite into a failure.
"""

from __future__ import annotations

import base64
import json
import os
import shutil
import subprocess
import tomllib
from pathlib import Path
from typing import Any

import cv2
import numpy as np
import pytest

import enreal
from tests.conftest import draw_stage

ROOT = Path(__file__).resolve().parent.parent
HARNESS = ROOT / "tests" / "pyodide"
REQUIRED = os.environ.get("ENREAL_PYODIDE") == "1"
JPEG_TOLERANCE_PX = 2


def _missing_prerequisite() -> str | None:
    if shutil.which("node") is None:
        return "node is not on PATH"
    if not (HARNESS / "node_modules" / "pyodide").is_dir():
        return "run `npm ci` in tests/pyodide"
    if shutil.which("uv") is None:
        return "uv is not on PATH"
    return None


def _write_cases(root: Path) -> dict[str, dict[str, Any]]:
    stage = draw_stage()
    files = {
        "stage.png": cv2.imencode(".png", stage)[1].tobytes(),
        "stage.webp": cv2.imencode(".webp", stage, [cv2.IMWRITE_WEBP_QUALITY, 90])[1].tobytes(),
        "stage.jpg": cv2.imencode(".jpg", stage, [cv2.IMWRITE_JPEG_QUALITY, 92])[1].tobytes(),
        "large.png": cv2.imencode(".png", draw_stage(1600, 900))[1].tobytes(),
        "junk.png": b"not an image",
    }
    for name, data in files.items():
        (root / name).write_bytes(data)
    return {
        "png-art": {"image": str(root / "stage.png"), "params": {"scene": "art"}},
        "png-ui": {"image": str(root / "stage.png"), "params": {"scene": "ui"}},
        "png-downscaled": {"image": str(root / "large.png"), "params": {"scene": "art", "max_dim": 800}},
        "webp": {"image": str(root / "stage.webp"), "params": {"scene": "art"}},
        "jpeg": {"image": str(root / "stage.jpg"), "params": {"scene": "art", "grid_size": 4}},
        "junk": {"image": str(root / "junk.png"), "params": {}},
    }


@pytest.fixture(scope="module")
def browser(tmp_path_factory: pytest.TempPathFactory) -> dict[str, dict[str, Any]]:
    """Run every case under Pyodide once. Maps case name to {case, result}."""
    reason = _missing_prerequisite()
    if reason:
        if REQUIRED:
            pytest.fail(f"ENREAL_PYODIDE=1 but {reason}")
        pytest.skip(reason)

    root = tmp_path_factory.mktemp("pyodide")
    subprocess.run(["uv", "build", "--wheel", "--out-dir", str(root), str(ROOT)], check=True, capture_output=True)
    wheel = root / f"enreal-{enreal.__version__}-py3-none-any.whl"
    cases = _write_cases(root)
    cases_file = root / "cases.json"
    cases_file.write_text(json.dumps(list(cases.values())), encoding="utf-8")

    proc = subprocess.run(
        ["node", str(HARNESS / "parity.mjs"), str(wheel), str(cases_file)],
        capture_output=True,
        text=True,
        timeout=600,
    )
    assert proc.returncode == 0, proc.stderr
    results = json.loads(proc.stdout)
    assert len(results) == len(cases)
    return {name: {"case": case, "result": res} for (name, case), res in zip(cases.items(), results, strict=True)}


def _cpython(case: dict[str, Any]) -> enreal.StageResult:
    return enreal.generate_skeleton(Path(case["image"]).read_bytes(), **case["params"])


def _red_mask(result: enreal.StageResult) -> np.ndarray:
    raw = base64.b64decode(result.overlay_image_b64.split(",", 1)[1])
    img = cv2.imdecode(np.frombuffer(raw, np.uint8), cv2.IMREAD_COLOR)
    return np.all(img == (0, 0, 255), axis=-1)


def test_harness_pins_the_same_pyodide_as_the_web_build() -> None:
    web = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))["tool"]["enreal"]["web"]
    harness = json.loads((HARNESS / "package.json").read_text(encoding="utf-8"))
    assert harness["dependencies"]["pyodide"] == web["pyodide-version"]


@pytest.mark.parametrize("name", ["png-art", "png-ui", "png-downscaled", "webp"])
def test_lossless_inputs_match_exactly(browser: dict[str, dict[str, Any]], name: str) -> None:
    entry = browser[name]
    assert entry["result"]["ok"], entry["result"].get("error")
    assert entry["result"]["json"] == _cpython(entry["case"]).model_dump_json()


def test_jpeg_terrain_matches_within_tolerance(browser: dict[str, dict[str, Any]]) -> None:
    entry = browser["jpeg"]
    assert entry["result"]["ok"], entry["result"].get("error")
    got = enreal.StageResult.model_validate_json(entry["result"]["json"])
    ref = _cpython(entry["case"])

    assert got.skeleton.bounds == ref.skeleton.bounds
    assert got.skeleton.spawn == ref.skeleton.spawn
    assert got.skeleton.meta == ref.skeleton.meta
    assert len(got.skeleton.curves) == len(ref.skeleton.curves)

    a, b = _red_mask(got), _red_mask(ref)
    size = 2 * JPEG_TOLERANCE_PX + 1
    kernel = np.ones((size, size), np.uint8)
    near_b = cv2.dilate(b.astype(np.uint8), kernel) > 0
    near_a = cv2.dilate(a.astype(np.uint8), kernel) > 0
    assert a.any() and b.any()
    assert (a & ~near_b).sum() == 0, "Pyodide terrain strays more than the tolerance from CPython's"
    assert (b & ~near_a).sum() == 0, "CPython terrain strays more than the tolerance from Pyodide's"


def test_bad_input_is_a_readable_error(browser: dict[str, dict[str, Any]]) -> None:
    result = browser["junk"]["result"]
    assert not result["ok"]
    assert result["error"].startswith("ValueError") and "decode" in result["error"]
