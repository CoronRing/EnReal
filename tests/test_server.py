"""The optional HTTP server: API contract, input limits, headers and CORS."""

from __future__ import annotations

from pathlib import Path

import pytest

pytest.importorskip("fastapi")

from fastapi.testclient import TestClient  # noqa: E402

import enreal  # noqa: E402
from server import app as server_app  # noqa: E402

WEB_DIR = Path(__file__).resolve().parent.parent / "web"


@pytest.fixture
def client() -> TestClient:
    return TestClient(server_app.create_app(web_dir=WEB_DIR))


def test_health_reports_engine_version(client: TestClient) -> None:
    res = client.get("/api/health")
    assert res.status_code == 200
    assert res.json() == {"status": "ok", "engine": "enreal", "version": enreal.__version__}
    assert res.headers["cache-control"] == "no-store"


def test_process_returns_stage(client: TestClient, stage_png: bytes) -> None:
    res = client.post("/api/process?scene=art", files={"file": ("stage.png", stage_png, "image/png")})
    assert res.status_code == 200, res.text
    body = res.json()
    assert set(body) == {"skeleton", "overlay_image_b64"}
    assert body["skeleton"]["bounds"]["width"] == 640
    assert body["skeleton"]["curves"]


def test_process_matches_the_engine(client: TestClient, stage_png: bytes) -> None:
    # Server and in-browser mode must hand the game the same JSON for the same input.
    res = client.post("/api/process?scene=art", files={"file": ("stage.png", stage_png, "image/png")})
    direct = enreal.generate_skeleton(stage_png, scene="art").model_dump(mode="json")
    assert res.json() == direct


def test_empty_upload_is_400(client: TestClient) -> None:
    res = client.post("/api/process", files={"file": ("empty.png", b"", "image/png")})
    assert res.status_code == 400


def test_undecodable_upload_is_400(client: TestClient) -> None:
    res = client.post("/api/process", files={"file": ("junk.png", b"not an image", "image/png")})
    assert res.status_code == 400
    assert "decode" in res.json()["detail"]


def test_out_of_range_parameter_is_422(client: TestClient, stage_png: bytes) -> None:
    res = client.post("/api/process?max_dim=10", files={"file": ("stage.png", stage_png, "image/png")})
    assert res.status_code == 422


def test_oversized_upload_is_413(monkeypatch: pytest.MonkeyPatch, client: TestClient, stage_png: bytes) -> None:
    monkeypatch.setattr(server_app, "MAX_UPLOAD_BYTES", 1024)
    res = client.post("/api/process", files={"file": ("stage.png", stage_png, "image/png")})
    assert res.status_code == 413


def test_serves_the_game_with_revalidation(client: TestClient) -> None:
    res = client.get("/")
    assert res.status_code == 200
    assert "<title>EnReal</title>" in res.text
    assert res.headers["cache-control"] == "no-cache"


def test_api_only_without_web_dir(tmp_path: Path) -> None:
    api_only = TestClient(server_app.create_app(web_dir=tmp_path / "missing"))
    assert api_only.get("/api/health").status_code == 200
    assert api_only.get("/").status_code == 404


def test_cors_is_off_by_default(client: TestClient) -> None:
    res = client.get("/api/health", headers={"Origin": "https://example.com"})
    assert "access-control-allow-origin" not in res.headers


def test_cors_allows_configured_origin(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ENREAL_CORS_ORIGINS", "https://coronring.github.io")
    cors_client = TestClient(server_app.create_app(web_dir=None))
    res = cors_client.options(
        "/api/process",
        headers={"Origin": "https://coronring.github.io", "Access-Control-Request-Method": "POST"},
    )
    assert res.headers["access-control-allow-origin"] == "https://coronring.github.io"
    other = cors_client.get("/api/health", headers={"Origin": "https://evil.example"})
    assert "access-control-allow-origin" not in other.headers
