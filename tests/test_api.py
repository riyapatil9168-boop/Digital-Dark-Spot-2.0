from fastapi.testclient import TestClient
import app.main as api

client = TestClient(api.app)

def test_health():
    response = client.get("/health")
    assert response.status_code == 200
    assert response.json()["ok"] is True

def test_frontend_served():
    response = client.get("/")
    assert response.status_code == 200
    assert "DIGITAL DARK SPOT" in response.text

def test_analyze_requires_question():
    response = client.post("/analyze", data={"question": "short", "sources": "[]"})
    assert response.status_code == 400

def test_analyze_requires_sources():
    response = client.post("/analyze", data={"question": "What should I research?", "sources": "[]"})
    assert response.status_code == 400

def test_analyze_structured_response(monkeypatch):
    async def fake_fetch(url):
        return {
            "url": url,
            "title": "Test source",
            "text": "This policy was announced in 2025. The program requires applicants to submit a form.",
            "status": "read",
        }
    monkeypatch.setattr(api, "fetch_source", fake_fetch)
    response = client.post(
        "/analyze",
        data={"question": "What are the program requirements?", "sources": '["https://example.com"]},
    )
    assert response.status_code == 200
    payload = response.json()
    assert payload["ok"] is True
    assert payload["source_count"] == 1
    assert "analysis" in payload
    assert "verification_checklist" in payload["analysis"]
