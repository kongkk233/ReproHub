import pytest
from fastapi.testclient import TestClient

from reprohub import repo
from reprohub.db import get_conn
from reprohub.llm import LLM
from reprohub.pipeline.score import process_new
from reprohub.web.app import create_app


@pytest.fixture()
def client(settings, seeded):
    process_new(settings, seeded, LLM(settings, seeded), logger=lambda m: None)
    app = create_app(settings)
    return TestClient(app)


def test_status_reports_mock_mode(client):
    d = client.get("/api/v1/status").json()
    assert d["llm"]["mode"] == "mock" and d["mock_notice"]
    assert d["counts"]["selected"] >= 1


def test_pages_render(client):
    for p in ["/", "/all", "/papers", "/news", "/topics", "/topics/early-embryo", "/stories", "/editions", "/library", "/sources", "/about", "/agent", "/admin", "/search?q=blastocyst"]:
        r = client.get(p)
        assert r.status_code == 200, p
    assert client.get("/items/99999").status_code == 404
    r = client.get("/")
    assert "模拟模式" in r.text and "精选" in r.text


def test_item_page_shows_mock_badge_and_api_flag(client):
    item = client.get("/api/v1/items?selected=1&order=score").json()["items"][0]
    assert item["is_mock"] is True
    r = client.get(f"/items/{item['id']}")
    assert r.status_code == 200 and "模拟评分" in r.text


def test_reading_roundtrip_and_library(client):
    iid = client.get("/api/v1/items?selected=1").json()["items"][0]["id"]
    r = client.post(f"/api/v1/reading/{iid}", json={"status": "read", "starred": True, "tags": ["ZGA"], "notes": "n", "rating": 4})
    assert r.status_code == 200 and r.json()["starred"] == 1 and r.json()["rating"] == 4
    r = client.post(f"/api/v1/reading/{iid}", json={"rating": 0})
    assert r.json()["rating"] is None
    assert iid in [x["id"] for x in repo.list_items(get_conn(client.app.state.settings), starred=True)[0]]
    r = client.get("/library?tab=starred")
    assert r.status_code == 200 and "ZGA" in r.text
    for fmt in ("json", "csv", "md", "bib"):
        assert client.get(f"/library/export.{fmt}").status_code == 200


def test_collections(client):
    cid = client.post("/api/v1/collections", json={"name": "课题A"}).json()["id"]
    iid = client.get("/api/v1/items").json()["items"][0]["id"]
    assert client.post(f"/api/v1/collections/{cid}/items/{iid}").status_code == 200
    assert len(client.get(f"/api/v1/collections/{cid}").json()["items"]) == 1
    assert client.get(f"/collections/{cid}").status_code == 200


def test_ingest_requires_token_and_creates_new_item(client, monkeypatch):
    assert client.post("/api/v1/ingest", json=[{"url": "https://x/1", "title": "t"}]).status_code == 403
    monkeypatch.setenv("INGEST_TOKEN", "secret")
    payload = [{"url": "https://example.org/new-paper", "title": "Oocyte mitochondrial proteome in aging", "summary": "Proteomics of aged mouse oocytes.", "doi": "10.9/zz"}]
    r = client.post("/api/v1/ingest", json=payload, headers={"Authorization": "Bearer wrong"})
    assert r.status_code == 401
    r = client.post("/api/v1/ingest", json=payload, headers={"Authorization": "Bearer secret"})
    assert r.status_code == 200 and r.json()["results"][0]["status"] == "new"


def test_feeds_and_llms_txt(client):
    r = client.get("/feed.xml")
    assert r.status_code == 200 and r.text.startswith("<?xml") and "<item>" in r.text
    assert "<item>" in client.get("/feed/topic/early-embryo.xml").text
    assert "is_mock" in client.get("/llms.txt").text


def test_admin_requires_owner_when_password_set(settings, seeded, monkeypatch):
    monkeypatch.setenv("ADMIN_PASSWORD", "pw")
    app = create_app(settings)
    c = TestClient(app)
    assert c.post("/api/v1/reading/1", json={"status": "read"}).status_code == 401
    assert c.post("/api/v1/reading/1", json={"status": "read"}, headers={"X-Admin-Token": "pw"}).status_code == 200
    r = c.post("/login", data={"password": "pw"}, follow_redirects=False)
    assert r.status_code == 303 and "rh_auth" in r.headers.get("set-cookie", "")
    assert c.get("/admin").status_code == 200


def test_proxied_requests_are_not_owner_without_password(client):
    # 经反向代理转发（带 X-Forwarded-For）的请求即使来自 127.0.0.1 也不是本机管理员
    r = client.post("/api/v1/reading/1", json={"status": "read"}, headers={"X-Forwarded-For": "203.0.113.9"})
    assert r.status_code == 401


def test_session_cookie_is_signed_and_expires(settings, seeded, monkeypatch):
    from reprohub.web.common import make_session_token, verify_session_token

    monkeypatch.setenv("ADMIN_PASSWORD", "pw")
    tok = make_session_token(settings, now=1000)
    assert verify_session_token(settings, tok, now=1001)
    assert not verify_session_token(settings, tok, now=10**10)  # 过期
    assert not verify_session_token(settings, tok[:-1] + ("0" if tok[-1] != "0" else "1"), now=1001)  # 篡改
    monkeypatch.setenv("ADMIN_PASSWORD", "pw2")
    assert not verify_session_token(settings, tok, now=1001)  # 改口令即作废


def test_login_rejects_open_redirect(settings, seeded, monkeypatch):
    monkeypatch.setenv("ADMIN_PASSWORD", "pw")
    c = TestClient(create_app(settings))
    r = c.post("/login?next=//evil.example", data={"password": "pw"}, follow_redirects=False)
    assert r.headers["location"] == "/admin"
