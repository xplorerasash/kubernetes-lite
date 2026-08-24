"""End-to-end API tests through the Flask test client (no Docker daemon)."""


def test_health_ok(api_client):
    res = api_client.get("/api/health")
    assert res.status_code == 200
    body = res.get_json()
    assert body["status"] == "healthy"
    assert body["docker"] == "connected"


def test_deploy_and_status_roundtrip(api_client):
    res = api_client.post(
        "/api/deploy",
        json={"name": "web", "image": "nginx:alpine", "replicas": 2},
    )
    assert res.status_code == 201
    assert len(res.get_json()["container_ids"]) == 2

    status = api_client.get("/api/status").get_json()
    deploys = {d["name"]: d for d in status["deployments"]}
    assert deploys["web"]["desired"] == 2
    assert deploys["web"]["running"] == 2


def test_deploy_validation_errors(api_client):
    # missing body
    assert api_client.post("/api/deploy").status_code == 400
    # missing image
    res = api_client.post("/api/deploy", json={"name": "x"})
    assert res.status_code == 400
    # replicas out of range
    res = api_client.post(
        "/api/deploy", json={"name": "x", "image": "nginx:alpine", "replicas": 99}
    )
    assert res.status_code == 400
    assert "Replicas" in res.get_json()["error"]
    # bad health port
    res = api_client.post(
        "/api/deploy",
        json={"name": "x", "image": "nginx:alpine", "health_port": 70000},
    )
    assert res.status_code == 400


def test_scale_unknown_deployment_returns_400(api_client):
    res = api_client.post("/api/scale", json={"name": "ghost", "replicas": 3})
    assert res.status_code == 400


def test_scale_and_delete_flow(api_client):
    api_client.post(
        "/api/deploy", json={"name": "api", "image": "nginx:alpine", "replicas": 1}
    )

    res = api_client.post("/api/scale", json={"name": "api", "replicas": 4})
    assert res.status_code == 200

    res = api_client.delete("/api/delete/api")
    assert res.status_code == 200
    assert api_client.delete("/api/delete/api").status_code == 404


def test_update_requires_existing_deployment(api_client):
    res = api_client.post(
        "/api/update", json={"name": "ghost", "image": "nginx:alpine"}
    )
    assert res.status_code == 400


def test_backup_creates_snapshot(api_client):
    api_client.post(
        "/api/deploy", json={"name": "web", "image": "nginx:alpine", "replicas": 2}
    )

    res = api_client.post("/api/maintenance/backup")
    assert res.status_code == 201
    path = res.get_json()["backup"]
    import os

    assert os.path.isfile(path)

    listing = api_client.get("/api/maintenance/backups").get_json()
    assert any(b["file"] == os.path.basename(path) for b in listing["backups"])
    assert listing["backups"][0]["size_bytes"] > 0


def test_events_endpoint(api_client):
    api_client.post(
        "/api/deploy", json={"name": "web", "image": "nginx:alpine", "replicas": 1}
    )
    events = api_client.get("/api/events").get_json()
    assert events["count"] >= 1
    assert any(e["type"] == "DEPLOY" for e in events["events"])
