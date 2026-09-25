"""Smoke tests for the project skeleton (Phase 0)."""


def test_health_endpoint(client):
    response = client.get("/health/")
    assert response.status_code == 200
    assert response.json() == {"status": "ok"}


def test_imprint_page_renders(client):
    response = client.get("/imprint/")
    assert response.status_code == 200
