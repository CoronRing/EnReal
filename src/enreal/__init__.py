"""EnReal: turn any image into a 2D platformer stage.

The engine is a pure function: image bytes in, :class:`StageResult` out. It never
reads files, touches the network or reads environment variables, so the same code
runs under CPython and in the browser under Pyodide.

Example::

    from pathlib import Path
    from enreal import generate_skeleton

    result = generate_skeleton(Path("screenshot.png").read_bytes(), scene="art")
    print(result.skeleton.bounds, len(result.skeleton.curves))

See ``docs/design.md`` in the repository for the pipeline and the data contract.
"""

from enreal.models import (
    Bounds,
    Meta,
    Point,
    PolylineModel,
    SegmentModel,
    Skeleton,
    StageResult,
)
from enreal.processing import Scene, generate_skeleton

__version__ = "0.1.0"

__all__ = [
    "Bounds",
    "Meta",
    "Point",
    "PolylineModel",
    "Scene",
    "SegmentModel",
    "Skeleton",
    "StageResult",
    "__version__",
    "generate_skeleton",
]
