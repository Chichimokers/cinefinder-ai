def test_health(client):
    response = client.get("/api/v1/health")
    assert response.status_code == 200
    assert response.json()["status"] == "ok"


def test_ready(client):
    response = client.get("/api/v1/ready")
    assert response.status_code == 200
    assert "ready" in response.json()


def test_auth_required(client):
    assert client.get("/api/v1/movies").status_code == 401
    assert client.get("/api/v1/movies", headers={"Authorization": "Bearer bad"}).status_code == 401


def test_protected_endpoints_with_auth(client, auth_headers):
    for path in ("/api/v1/movies", "/api/v1/stats", "/api/v1/search/history", "/api/v1/openapi.json"):
        response = client.get(path, headers=auth_headers)
        assert response.status_code == 200, path


def test_search_validation(client, auth_headers):
    response = client.post("/api/v1/search", json={}, headers=auth_headers)
    assert response.status_code == 400


def test_job_not_found(client, auth_headers):
    response = client.get("/api/v1/jobs/no-existe", headers=auth_headers)
    assert response.status_code == 404
