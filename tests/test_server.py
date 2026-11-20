"""Тесты API «Сигнал.kg» и поиска сигнала по координатам."""
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from covermap.pipeline import build
from covermap.signal import SignalIndex
from server import app as server_app
from server.app import create_app

DATA = Path(__file__).resolve().parent.parent / "data" / "07_coverage"
HOUSE = {"lat": 42.8687, "lon": 74.604}  # координаты реального объявления house.kg


@pytest.fixture(scope="module")
def index():
    report = build(DATA / "measurements.csv", DATA / "towers.csv", DATA / "candidates.csv", DATA / "bounds.json")
    return SignalIndex(report), report


@pytest.fixture
def client(tmp_path):
    app = create_app(complaints_file=tmp_path / "complaints.jsonl", dgis_key="")
    with TestClient(app) as test_client:
        yield test_client


# --- поиск сигнала ------------------------------------------------------------

def test_lookup_point_inside_counted_cell(index):
    idx, report = index
    cell = report.worst[0]
    lat, lon = report.bounds.cell_center(cell.cell)
    found = idx.lookup(lat, lon)
    assert found["cell_id"] == cell.id and found["color"] == "red"
    assert found["median"] == cell.median and not found["estimated"]
    assert found["nearest_weak_m"] == 0


def test_lookup_outside_area_is_none(index):
    assert index[0].lookup(42.80, 74.60) is None


def test_lookup_reports_distance_to_weak_zone(index):
    found = index[0].lookup(**HOUSE)
    assert found["status"] == "ok" and found["color"] != "red"
    assert found["nearest_weak_m"] > 0


def test_lookup_estimates_from_neighbour_for_skipped_cell(index):
    idx, report = index
    counted = {c.cell for c in report.cells}
    skipped = {report.bounds.cell_of(m.lat, m.lon) for m in report.cleaned.measurements} - counted
    lat, lon = report.bounds.cell_center(sorted(skipped)[0])
    found = idx.lookup(lat, lon)
    assert found["status"] in ("ok", "no_data")
    if found["status"] == "ok":
        assert found["estimated"]


# --- API -----------------------------------------------------------------------

def test_signal_endpoint(client):
    r = client.get("/api/signal", params=HOUSE)
    assert r.status_code == 200
    assert r.json()["quality"] in ("хорошая", "средняя", "слабая")


@pytest.mark.parametrize("params", [
    {"lat": "abc", "lon": "74.6"}, {"lat": "nan", "lon": "74.6"}, {"lat": "inf", "lon": "74.6"}, {"lat": "42.86"}, {},
])
def test_signal_bad_coordinates_400(client, params):
    assert client.get("/api/signal", params=params).status_code == 400


def test_signal_out_of_area_422(client):
    assert client.get("/api/signal", params={"lat": 42.8, "lon": 74.6}).json()["error"] == "out_of_area"


def test_cells_and_result(client):
    cells = client.get("/api/cells").json()["cells"]
    assert len(cells) == 387 and {"cell_id", "median", "color", "center"} <= set(cells[0])
    assert client.get("/api/result").json()["best_candidates"] == ["C09", "C14"]


def test_panel_is_live(client):
    r = client.get("/")
    assert r.status_code == 200 and '"live":true' in r.text
    assert r.headers["x-frame-options"] == "DENY"
    assert r.headers["x-content-type-options"] == "nosniff"


def test_complaint_roundtrip_without_personal_data(client, tmp_path):
    r = client.post("/api/complaints", json=HOUSE)
    assert r.status_code == 201
    record = r.json()
    assert set(record) == {"id", "ts", "lat", "lon", "cell_id", "source", "site"}
    assert record["source"] == "plugin" and record["cell_id"]
    assert record["site"] is None
    assert client.get("/api/complaints", params={"after": 0}).json()["complaints"] == [record]
    assert client.get("/api/complaints", params={"after": record["id"]}).json()["complaints"] == []
    saved = (tmp_path / "complaints.jsonl").read_text()
    assert "testclient" not in saved and "127.0.0.1" not in saved


def test_complaints_survive_restart(tmp_path):
    path = tmp_path / "complaints.jsonl"
    with TestClient(create_app(complaints_file=path)) as first:
        first.post("/api/complaints", json=HOUSE)
    with TestClient(create_app(complaints_file=path)) as second:
        assert len(second.get("/api/complaints").json()["complaints"]) == 1
        assert second.post("/api/complaints", json=HOUSE).json()["id"] == 2


@pytest.mark.parametrize("body", [
    b"{oops", b"[]", b'{"lat": "x", "lon": 74.6}', b'{"lat": true, "lon": 74.6}',
    b'{"lat": 42.86, "lon": 74.58, "source": "<script>"}', b'{"lat": NaN, "lon": 74.58}',
    b'{"lat": 42.86, "lon": 74.58, "site": "evil.example"}',
])
def test_complaint_bad_body_400(client, body):
    r = client.post("/api/complaints", content=body, headers={"Content-Type": "application/json"})
    assert r.status_code == 400


def test_complaint_too_large_413(client):
    body = b'{"lat": 42.86, "lon": 74.58, "x": "' + b"a" * 2000 + b'"}'
    assert client.post("/api/complaints", content=body).status_code == 413


def test_complaint_out_of_area_422(client):
    assert client.post("/api/complaints", json={"lat": 42.8, "lon": 74.6}).status_code == 422


def test_complaint_rate_limit_per_ip(client):
    for _ in range(server_app.COMPLAINTS_PER_MINUTE):
        assert client.post("/api/complaints", json=HOUSE).status_code == 201
    r = client.post("/api/complaints", json=HOUSE, headers={"X-Forwarded-For": "9.9.9.9"})
    assert r.status_code == 429 and int(r.headers["retry-after"]) > 0
    assert client.get("/api/complaints").json()["rejected"] == 1


def test_complaint_keeps_listing_site(client):
    r = client.post("/api/complaints", json={**HOUSE, "site": "lalafo.kg"})
    assert r.status_code == 201 and r.json()["site"] == "lalafo.kg"


def test_cors_only_for_listing_sites(client):
    allowed = client.get("/api/signal", params=HOUSE, headers={"Origin": "https://www.house.kg"})
    assert allowed.headers["access-control-allow-origin"] == "https://www.house.kg"
    denied = client.get("/api/signal", params=HOUSE, headers={"Origin": "https://evil.example"})
    assert "access-control-allow-origin" not in denied.headers


# --- приложение для телефона (PWA) --------------------------------------------

def test_app_shell_and_files(client):
    assert client.get("/app", follow_redirects=False).status_code in (301, 302, 307)
    page = client.get("/app/")
    assert page.status_code == 200 and '<link rel="manifest" href="/app/manifest.webmanifest">' in page.text
    assert "/*FONTS_CSS*/" not in page.text and "@font-face" in page.text
    manifest = client.get("/app/manifest.webmanifest")
    assert manifest.headers["content-type"].startswith("application/manifest+json")
    data = manifest.json()
    assert data["display"] == "standalone" and data["start_url"] == "/app/"
    for icon in data["icons"]:
        assert client.get(icon["src"]).status_code == 200
    worker = client.get("/app/sw.js")
    assert worker.headers["content-type"].startswith("text/javascript")
    assert client.get("/app/../server/app.py").status_code == 404
    assert client.get("/app/secret.txt").status_code == 404


def test_panel_is_installable(client):
    assert client.get("/manifest.webmanifest").json()["start_url"] == "/"
    assert client.get("/sw.js").status_code == 200


def test_complaint_from_app(client):
    r = client.post("/api/complaints", json={**HOUSE, "source": "app"})
    assert r.status_code == 201 and r.json()["source"] == "app"


def test_panel_has_no_app_tab(client):
    page = client.get("/").text
    assert 'data-v="app"' not in page and 'id="v-app"' not in page
    assert client.get("/api/info").status_code == 404


# --- сигналы жителей «как ловит связь» -----------------------------------------

def test_report_roundtrip(client, tmp_path):
    r = client.post("/api/reports", json={**HOUSE, "level": "bad", "network": "4G", "site": "house.kg"})
    assert r.status_code == 201
    record = r.json()
    assert set(record) == {"id", "ts", "lat", "lon", "cell_id", "source", "site", "level", "network"}
    assert record["level"] == "bad" and record["network"] == "4G" and record["cell_id"]
    assert client.get("/api/reports").json()["reports"] == [record]
    assert client.get("/api/reports", params={"after": record["id"]}).json()["reports"] == []
    saved = (tmp_path / "reports.jsonl").read_text()
    assert "127.0.0.1" not in saved and "testclient" not in saved
    assert client.get("/api/complaints").json()["complaints"] == []  # жалобы и сигналы раздельно


def test_report_without_network(client):
    r = client.post("/api/reports", json={**HOUSE, "level": "good"})
    assert r.status_code == 201 and r.json()["network"] is None


@pytest.mark.parametrize("body", [
    {"level": "great"}, {"level": None}, {}, {"level": "ok", "network": "6G"},
    {"level": "ok", "site": "evil.example"}, {"level": "ok", "source": "bot"},
])
def test_report_bad_fields_400(client, body):
    assert client.post("/api/reports", json={**HOUSE, **body}).status_code == 400


def test_report_out_of_area_and_limit(client):
    assert client.post("/api/reports", json={"lat": 42.8, "lon": 74.6, "level": "ok"}).status_code == 422
    for _ in range(server_app.COMPLAINTS_PER_MINUTE):
        assert client.post("/api/reports", json={**HOUSE, "level": "ok"}).status_code == 201
    r = client.post("/api/reports", json={**HOUSE, "level": "ok"})
    assert r.status_code == 429 and client.get("/api/reports").json()["rejected"] == 1


def test_report_too_large(client):
    body = b'{"lat": 42.86, "lon": 74.58, "level": "ok", "x": "' + b"a" * 2000 + b'"}'
    assert client.post("/api/reports", content=body).status_code == 413


def test_cells_have_corners_for_app_map(client):
    cell = client.get("/api/cells").json()["cells"][0]
    assert len(cell["corners"]) == 4 and all(len(p) == 2 for p in cell["corners"])


def test_app_serves_leaflet_and_has_map_screen(client):
    assert client.get("/app/leaflet.js").headers["content-type"].startswith("text/javascript")
    assert client.get("/app/leaflet.css").headers["content-type"].startswith("text/css")
    page = client.get("/app/").text
    assert 'id="s-map"' in page and '<script src="/app/leaflet.js"></script>' in page
