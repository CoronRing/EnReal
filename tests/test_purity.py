"""The engine must run with nothing but numpy, OpenCV and pydantic.

That is what lets the browser load it under Pyodide. This runs the engine in a fresh
interpreter where every heavier or server-side package is made unimportable, so an
accidental import anywhere in ``enreal`` fails here rather than in a user's browser.
"""

from __future__ import annotations

import subprocess
import sys
import textwrap
from pathlib import Path

BLOCKED = [
    "fastapi",
    "starlette",
    "uvicorn",
    "httpx",
    "requests",
    "PIL",
    "litellm",
    "openai",
    "skimage",
    "scipy",
    "matplotlib",
]

SCRIPT = textwrap.dedent(
    """
    import importlib.abc
    import sys

    BLOCKED = set(sys.argv[1].split(","))

    class Block(importlib.abc.MetaPathFinder):
        def find_spec(self, name, path=None, target=None):
            if name.split(".")[0] in BLOCKED:
                raise ModuleNotFoundError(f"blocked in the purity test: {name}")
            return None

    sys.meta_path.insert(0, Block())

    import cv2
    import numpy as np

    import enreal

    img = np.full((120, 200, 3), 230, dtype=np.uint8)
    cv2.rectangle(img, (20, 70), (180, 90), (20, 20, 20), -1)
    ok, buf = cv2.imencode(".png", img)
    result = enreal.generate_skeleton(buf.tobytes(), scene="art")
    assert result.skeleton.bounds.width == 200

    loaded = sorted(m for m in sys.modules if m.split(".")[0] in BLOCKED)
    assert not loaded, loaded
    print("ok")
    """
)


def test_engine_runs_without_server_or_optional_packages() -> None:
    src = Path(__file__).resolve().parent.parent / "src"
    proc = subprocess.run(
        [sys.executable, "-c", SCRIPT, ",".join(BLOCKED)],
        capture_output=True,
        text=True,
        env={"PYTHONPATH": str(src), "SYSTEMROOT": _systemroot()},
        timeout=120,
    )
    assert proc.returncode == 0, proc.stderr
    assert proc.stdout.strip().endswith("ok")


def _systemroot() -> str:
    # Windows needs SYSTEMROOT to initialise sockets and crypto even in a bare environment.
    import os

    return os.environ.get("SYSTEMROOT", "")
