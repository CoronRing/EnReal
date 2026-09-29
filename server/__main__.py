"""Start the EnReal server: ``uv run python -m server`` from the repository root.

Environment:
    HOST, PORT: bind address, default ``127.0.0.1:8000``.
    UVICORN_RELOAD: ``1`` to reload on code changes.
    ENREAL_LOGLEVEL: DEBUG, INFO (default), WARNING or ERROR.
    ENREAL_CORS_ORIGINS: comma-separated origins allowed to call the API cross-site.
"""

from __future__ import annotations

import logging
import os

import uvicorn


def main() -> None:
    """Configure logging and run uvicorn."""
    level_name = os.getenv("ENREAL_LOGLEVEL", "INFO").upper()
    logging.basicConfig(
        level=getattr(logging, level_name, logging.INFO),
        format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
        datefmt="%H:%M:%S",
    )
    uvicorn.run(
        "server.app:app",
        host=os.getenv("HOST", "127.0.0.1"),
        port=int(os.getenv("PORT", "8000")),
        reload=os.getenv("UVICORN_RELOAD", "0").lower() in ("1", "true"),
        log_level=level_name.lower(),
    )


if __name__ == "__main__":
    main()
