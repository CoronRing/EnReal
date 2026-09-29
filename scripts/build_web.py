"""Build the static web game with the in-browser engine.

Builds the enreal wheel and writes it, plus ``engine.json``, into ``<target>/engine/``.
``web/worker.js`` reads ``engine.json`` to find the wheel and the Pyodide release to load.

Usage (from the repository root)::

    uv run python scripts/build_web.py              # in place: web/engine/, for local development
    uv run python scripts/build_web.py --out _site  # deployable copy with cache-busted asset URLs

The deployable copy stamps every local script and stylesheet URL in the HTML with
``?v=<build id>``, a hash of the site's contents, so a returning visitor can never pair a
cached script with a newer page. ``web/backend.js`` passes the same stamp on to the worker.
See docs/design.md, "Building and hosting the web game".
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import shutil
import subprocess
import sys
import tempfile
import tomllib
from pathlib import Path
from typing import TypedDict

ROOT = Path(__file__).resolve().parent.parent
WEB = ROOT / "web"
PYODIDE_CDN = "https://cdn.jsdelivr.net/pyodide/v{version}/full/"
# Loaded from the Pyodide distribution rather than PyPI: Pyodide ships WebAssembly builds
# of these, and its opencv-python stands in for the opencv-python-headless the wheel declares.
PYODIDE_PACKAGES = ["numpy", "opencv-python", "pydantic"]

_LOCAL_ASSET = re.compile(r'(?P<attr>\b(?:src|href))="(?P<path>(?!https?:|//|/|data:|#)[^"?#]+\.(?:js|css))"')
_VERSION = re.compile(r'^__version__\s*=\s*"(?P<version>[^"]+)"', re.MULTILINE)


class WheelInfo(TypedDict):
    file: str
    sha256: str


class PyodideInfo(TypedDict):
    version: str
    indexURL: str
    packages: list[str]


class EngineManifest(TypedDict):
    name: str
    version: str
    wheel: WheelInfo
    pyodide: PyodideInfo


def engine_version() -> str:
    """The version single-sourced in ``src/enreal/__init__.py``."""
    match = _VERSION.search((ROOT / "src" / "enreal" / "__init__.py").read_text(encoding="utf-8"))
    if not match:
        raise SystemExit("Could not find __version__ in src/enreal/__init__.py")
    return match["version"]


def pyodide_version() -> str:
    """The Pyodide release pinned in ``pyproject.toml`` under ``[tool.enreal.web]``."""
    config = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    try:
        return str(config["tool"]["enreal"]["web"]["pyodide-version"])
    except KeyError as e:
        raise SystemExit("pyproject.toml is missing [tool.enreal.web] pyodide-version") from e


def build_wheel(version: str) -> Path:
    """Build the wheel with uv into a temporary directory and return its path."""
    if shutil.which("uv") is None:
        raise SystemExit("uv is required to build the wheel: https://docs.astral.sh/uv/")
    out_dir = Path(tempfile.mkdtemp(prefix="enreal-wheel-"))
    subprocess.run(["uv", "build", "--wheel", "--out-dir", str(out_dir), str(ROOT)], check=True)
    wheel = out_dir / f"enreal-{version}-py3-none-any.whl"
    if not wheel.is_file():
        found = ", ".join(p.name for p in out_dir.iterdir()) or "nothing"
        raise SystemExit(f"Expected {wheel.name}, uv built: {found}")
    return wheel


def clear_directory(path: Path) -> None:
    """Empty ``path`` without removing it, so a preview server running inside it survives."""
    path.mkdir(parents=True, exist_ok=True)
    for child in path.iterdir():
        if child.is_dir() and not child.is_symlink():
            shutil.rmtree(child)
        else:
            child.unlink()


def write_engine(target: Path, wheel: Path, version: str, index_url: str) -> EngineManifest:
    """Copy the wheel into ``target/engine/`` and write ``engine.json`` next to it."""
    engine_dir = target / "engine"
    clear_directory(engine_dir)
    shutil.copy2(wheel, engine_dir / wheel.name)
    manifest: EngineManifest = {
        "name": "enreal",
        "version": version,
        "wheel": {"file": wheel.name, "sha256": hashlib.sha256(wheel.read_bytes()).hexdigest()},
        "pyodide": {"version": pyodide_version(), "indexURL": index_url, "packages": PYODIDE_PACKAGES},
    }
    (engine_dir / "engine.json").write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8")
    return manifest


def build_id(site: Path) -> str:
    """Stable hash of every file in the site, so identical inputs give an identical stamp."""
    digest = hashlib.sha256()
    for path in sorted(p for p in site.rglob("*") if p.is_file()):
        digest.update(path.relative_to(site).as_posix().encode())
        digest.update(b"\0")
        digest.update(path.read_bytes())
    return digest.hexdigest()[:12]


def stamp_assets(site: Path, stamp: str) -> int:
    """Append ``?v=<stamp>`` to local .js and .css URLs in every HTML file. Returns the count."""
    count = 0
    for html in site.rglob("*.html"):
        text = html.read_text(encoding="utf-8")
        new, n = _LOCAL_ASSET.subn(lambda m: f'{m["attr"]}="{m["path"]}?v={stamp}"', text)
        if n:
            html.write_text(new, encoding="utf-8", newline="\n")
            count += n
    return count


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0] if __doc__ else None)
    parser.add_argument(
        "--out", type=Path, help="Assemble a deployable copy of web/ here instead of building in place."
    )
    parser.add_argument(
        "--pyodide-index-url",
        help="Load Pyodide from this URL instead of the jsDelivr CDN, for example a self-hosted copy. Must end with '/'.",
    )
    args = parser.parse_args(argv)

    version = engine_version()
    index_url = args.pyodide_index_url or PYODIDE_CDN.format(version=pyodide_version())
    if not index_url.endswith("/"):
        raise SystemExit("--pyodide-index-url must end with '/'")

    wheel = build_wheel(version)
    try:
        if args.out is None:
            manifest = write_engine(WEB, wheel, version, index_url)
            print(f"Built enreal {version} into {WEB / 'engine'} (Pyodide {manifest['pyodide']['version']})")
            return 0

        site: Path = args.out.resolve()
        if site == WEB.resolve():
            raise SystemExit("--out must differ from web/; omit --out to build in place")
        clear_directory(site)
        shutil.copytree(WEB, site, ignore=shutil.ignore_patterns("engine"), dirs_exist_ok=True)
        manifest = write_engine(site, wheel, version, index_url)
        stamp = build_id(site)
        stamped = stamp_assets(site, stamp)
        print(f"Built site for enreal {version} at {site}: build {stamp}, {stamped} asset URLs stamped")
        return 0
    finally:
        shutil.rmtree(wheel.parent, ignore_errors=True)


if __name__ == "__main__":
    sys.exit(main())
