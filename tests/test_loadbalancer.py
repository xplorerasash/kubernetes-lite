"""Load balancer tests: nginx sidecar lifecycle + upstream syncing (fake Docker)."""

import base64
import re


def _last_conf(fake):
    """Decoded nginx config from the most recent exec/run against the fake"""
    cmd = " ".join(fake.exec_log[-1][1])
    b64 = cmd.split("echo ", 1)[1].split(" | base64", 1)[0].strip()
    return base64.b64decode(b64).decode()


def _upstream_count(conf):
    """Number of upstream server entries in an nginx config"""
    return len(re.findall(r"server \d+\.\d+\.\d+\.\d+:\d+;", conf))


def test_enable_creates_lb_with_replica_ips(orchestrator):
    result = orchestrator.deploy("web", "nginx:alpine", 2)
    ips = {orchestrator.docker.containers[c]['ip'] for c in result["container_ids"]}

    out = orchestrator.enable_load_balancer("web")
    assert out["success"] is True
    assert out["lb"]["port"] == 8000
    assert out["lb"]["target_port"] == 80

    lb = orchestrator.docker.get_lb_container("web")
    assert lb is not None
    assert lb["status"] == "running"
    assert lb["host_port"] == 8000
    # The generated upstream pool must contain every replica IP exactly once
    conf = _last_conf(orchestrator.docker)
    for ip in ips:
        assert f"server {ip}:80;" in conf


def test_target_port_defaults_to_health_port(orchestrator):
    orchestrator.deploy("web", "nginx:alpine", 1, health_port=8080)

    out = orchestrator.enable_load_balancer("web")
    assert out["lb"]["target_port"] == 8080
    assert ":8080;" in _last_conf(orchestrator.docker)


def test_scale_updates_upstream_immediately_and_reconcile_is_idempotent(orchestrator):
    orchestrator.deploy("web", "nginx:alpine", 2)
    orchestrator.enable_load_balancer("web")

    entries_after_enable = len(orchestrator.docker.exec_log)
    orchestrator.scale("web", 3)
    # Scale path pushes a fresh config right away
    assert len(orchestrator.docker.exec_log) == entries_after_enable + 1
    assert _upstream_count(_last_conf(orchestrator.docker)) == 3

    # A reconcile pass sees no drift -> must not rewrite config again
    orchestrator.reconcile_once()
    assert len(orchestrator.docker.exec_log) == entries_after_enable + 1


def test_reconcile_updates_upstream_after_self_heal(orchestrator):
    result = orchestrator.deploy("web", "nginx:alpine", 2)
    orchestrator.enable_load_balancer("web")
    dead_id = result["container_ids"][0]
    orchestrator.docker.containers.pop(dead_id)

    orchestrator.docker.exec_log.clear()
    orchestrator.reconcile_once()

    assert _upstream_count(_last_conf(orchestrator.docker)) == 2


def test_disable_removes_lb(orchestrator):
    orchestrator.deploy("web", "nginx:alpine", 2)
    orchestrator.enable_load_balancer("web")

    out = orchestrator.enable_load_balancer("web", enabled=False)
    assert out["success"] is True
    assert orchestrator.docker.get_lb_container("web") is None

    deployment = orchestrator.state.get_deployment("web")
    assert deployment["lb_port"] is None


def test_lb_self_heals_when_killed(orchestrator):
    orchestrator.deploy("web", "nginx:alpine", 2)
    orchestrator.enable_load_balancer("web")
    old_id = orchestrator.docker.get_lb_container("web")["full_id"]

    orchestrator.docker.kill_container(old_id)
    orchestrator.reconcile_once()

    lb = orchestrator.docker.get_lb_container("web")
    assert lb is not None
    assert lb["status"] == "running"
    assert lb["full_id"] != old_id
    assert lb["host_port"] == 8000


def test_delete_deployment_removes_lb(orchestrator):
    orchestrator.deploy("web", "nginx:alpine", 2)
    orchestrator.enable_load_balancer("web")

    orchestrator.delete_deployment("web")

    assert orchestrator.docker.get_lb_container("web") is None
    lbs = [c for c in orchestrator.docker.get_all_managed_containers()
           if c.get('name', '').endswith('-lb')]
    assert lbs == []


def test_unique_ports_across_deployments(orchestrator):
    orchestrator.deploy("a", "nginx:alpine", 1)
    orchestrator.deploy("b", "nginx:alpine", 1)

    first = orchestrator.enable_load_balancer("a")["lb"]["port"]
    second = orchestrator.enable_load_balancer("b")["lb"]["port"]

    assert first != second


def test_validation_errors(orchestrator):
    orchestrator.deploy("web", "nginx:alpine", 1)

    assert "error" in orchestrator.enable_load_balancer("ghost")
    assert "error" in orchestrator.enable_load_balancer(
        "web", target_port=70000
    )


# ---- API surface ----


def test_lb_endpoint_roundtrip(api_client):
    api_client.post("/api/deploy",
                    json={"name": "web", "image": "nginx:alpine", "replicas": 2})

    res = api_client.post("/api/lb", json={"name": "web", "target_port": 80})
    assert res.status_code == 200
    body = res.get_json()
    assert body["lb"]["enabled"] is True
    port = body["lb"]["port"]

    status = api_client.get("/api/status").get_json()
    deploys = {d["name"]: d for d in status["deployments"]}
    assert deploys["web"]["lb"]["port"] == port

    res = api_client.post("/api/lb", json={"name": "web", "enabled": False})
    assert res.status_code == 200
    assert res.get_json()["lb"]["enabled"] is False

    status = api_client.get("/api/status").get_json()
    deploys = {d["name"]: d for d in status["deployments"]}
    assert deploys["web"]["lb"] is None


def test_lb_endpoint_validation(api_client):
    api_client.post("/api/deploy",
                    json={"name": "web", "image": "nginx:alpine", "replicas": 1})

    assert api_client.post("/api/lb", json={}).status_code == 400
    assert api_client.post("/api/lb", json={"name": "ghost"}).status_code == 400
    res = api_client.post("/api/lb",
                          json={"name": "web", "target_port": 99999})
    assert res.status_code == 400
