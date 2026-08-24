"""Auto-scaler tests: HPA-style scaling decisions + API surface (no daemon)."""


def _deploy_loaded(orch, name="web", replicas=2, cpu=90.0):
    """Deploy and make every replica report the given CPU percent"""
    result = orch.deploy(name, "nginx:alpine", replicas)
    assert result["success"] is True
    for cid in result["container_ids"]:
        orch.docker.set_cpu(cid, cpu)
    return result


def test_configure_autoscaling_persists(orchestrator):
    orchestrator.deploy("web", "nginx:alpine", 2)

    result = orchestrator.configure_autoscaling(
        "web", min_replicas=1, max_replicas=6, target_cpu=60
    )
    assert result["success"] is True
    assert result["autoscale"] == {
        "enabled": True, "min_replicas": 1, "max_replicas": 6, "target_cpu": 60
    }

    stored = orchestrator.state.get_deployment("web")["autoscale"]
    assert stored["enabled"] is True
    assert stored["min_replicas"] == 1
    assert stored["max_replicas"] == 6
    assert stored["target_cpu"] == 60


def test_configure_autoscaling_validation(orchestrator):
    orchestrator.deploy("web", "nginx:alpine", 2)
    orch = orchestrator

    assert "error" in orch.configure_autoscaling("ghost", True, 1, 5, 70)
    assert "error" in orch.configure_autoscaling("web", True, 8, 3, 70)   # min > max
    assert "error" in orch.configure_autoscaling("web", True, 0, 5, 70)   # min < 1
    assert "error" in orch.configure_autoscaling("web", True, 1, 11, 70)  # max > 10
    assert "error" in orch.configure_autoscaling("web", True, 1, 5, 0)    # target < 1
    assert "error" in orch.configure_autoscaling("web", True, 1, 5, 101)  # target > 100


def test_disable_autoscaling(orchestrator):
    orchestrator.deploy("web", "nginx:alpine", 2)
    orchestrator.configure_autoscaling("web", True, 1, 6, 60)

    result = orchestrator.configure_autoscaling("web", enabled=False)
    assert result["success"] is True

    stored = orchestrator.state.get_deployment("web")["autoscale"]
    # Config values are kept so it can be re-enabled later, but it's off
    assert stored["enabled"] is False
    assert stored["max_replicas"] == 6


def test_scales_up_under_high_cpu(orchestrator):
    orchestrator.update_wait_seconds = 0
    _deploy_loaded(orchestrator, replicas=2, cpu=90.0)
    orchestrator.configure_autoscaling("web", True, min_replicas=1, max_replicas=10,
                                       target_cpu=70)
    orchestrator.autoscale_cooldown = 0

    orchestrator.autoscale_once()

    # HPA math: ceil(2 * 90 / 70) = 3
    assert orchestrator.state.get_deployment("web")["desired_replicas"] == 3
    assert len(orchestrator.state.get_deployment("web")["container_ids"]) == 3


def test_scales_down_under_low_cpu(orchestrator):
    _deploy_loaded(orchestrator, replicas=4, cpu=5.0)
    orchestrator.configure_autoscaling("web", True, min_replicas=1, max_replicas=10,
                                       target_cpu=50)
    orchestrator.autoscale_cooldown = 0

    orchestrator.autoscale_once()

    # floor(4 * 5 / 50) = 0 -> clamped to min_replicas=1
    deployment = orchestrator.state.get_deployment("web")
    assert deployment["desired_replicas"] == 1
    assert len(deployment["container_ids"]) == 1


def test_respects_max_replicas(orchestrator):
    _deploy_loaded(orchestrator, replicas=4, cpu=300.0)  # multi-core overload
    orchestrator.configure_autoscaling("web", True, min_replicas=1, max_replicas=5,
                                       target_cpu=50)
    orchestrator.autoscale_cooldown = 0

    orchestrator.autoscale_once()

    # ceil(4 * 300/50) = 24 -> capped at max_replicas=5
    assert orchestrator.state.get_deployment("web")["desired_replicas"] == 5


def test_no_flapping_within_tolerance(orchestrator):
    _deploy_loaded(orchestrator, replicas=2, cpu=75.0)  # ratio 75/70 ~ 1.07 < 1.1
    orchestrator.configure_autoscaling("web", True, min_replicas=1, max_replicas=10,
                                       target_cpu=70)
    orchestrator.autoscale_cooldown = 0

    orchestrator.autoscale_once()

    assert orchestrator.state.get_deployment("web")["desired_replicas"] == 2


def test_cooldown_blocks_immediate_second_scale(orchestrator):
    _deploy_loaded(orchestrator, replicas=2, cpu=90.0)
    orchestrator.configure_autoscaling("web", True, min_replicas=1, max_replicas=10,
                                       target_cpu=70)

    orchestrator.autoscale_once()
    assert orchestrator.state.get_deployment("web")["desired_replicas"] == 3

    # Load rises further, but cooldown (default 30s) suppresses a second action
    for cid in orchestrator.state.get_deployment("web")["container_ids"]:
        orchestrator.docker.set_cpu(cid, 95.0)
    orchestrator.autoscale_once()
    assert orchestrator.state.get_deployment("web")["desired_replicas"] == 3


def test_disabled_or_unconfigured_deployment_untouched(orchestrator):
    _deploy_loaded(orchestrator, replicas=2, cpu=99.0)

    orchestrator.autoscale_once()

    deployment = orchestrator.state.get_deployment("web")
    assert deployment["desired_replicas"] == 2
    # Metrics are not even sampled for non-autoscaled deployments
    assert "web" not in orchestrator._metrics_cache


def test_metrics_cached_for_status(orchestrator):
    _deploy_loaded(orchestrator, replicas=2, cpu=80.0)
    orchestrator.configure_autoscaling("web", True, 1, 10, 70)
    orchestrator.autoscale_cooldown = 30

    orchestrator.autoscale_once()

    cached = orchestrator._metrics_cache["web"]
    assert cached["avg_cpu"] == 80.0
    assert cached["replicas_sampled"] == 2


# ---- API surface ----


def test_autoscale_endpoint_roundtrip(api_client):
    api_client.post("/api/deploy",
                    json={"name": "web", "image": "nginx:alpine", "replicas": 2})

    res = api_client.post("/api/autoscale",
                          json={"name": "web", "min_replicas": 1,
                                "max_replicas": 6, "target_cpu": 60})
    assert res.status_code == 200
    assert res.get_json()["autoscale"]["enabled"] is True

    status = api_client.get("/api/status").get_json()
    deploys = {d["name"]: d for d in status["deployments"]}
    assert deploys["web"]["autoscale"]["enabled"] is True

    res = api_client.post("/api/autoscale", json={"name": "web", "enabled": False})
    assert res.status_code == 200
    assert res.get_json()["autoscale"]["enabled"] is False


def test_autoscale_endpoint_validation(api_client):
    api_client.post("/api/deploy",
                    json={"name": "web", "image": "nginx:alpine", "replicas": 1})

    res = api_client.post("/api/autoscale",
                          json={"name": "web", "min_replicas": 9,
                                "max_replicas": 2, "target_cpu": 70})
    assert res.status_code == 400

    res = api_client.post("/api/autoscale",
                          json={"name": "ghost", "min_replicas": 1,
                                "max_replicas": 2, "target_cpu": 70})
    assert res.status_code == 400


def test_deploy_with_autoscale_params(api_client):
    res = api_client.post(
        "/api/deploy",
        json={"name": "web", "image": "nginx:alpine", "replicas": 2,
              "min_replicas": 2, "max_replicas": 8, "target_cpu": 70},
    )
    assert res.status_code == 201
    body = res.get_json()
    assert body["autoscale"]["enabled"] is True
    assert body["autoscale"]["max_replicas"] == 8

    status = api_client.get("/api/status").get_json()
    deploys = {d["name"]: d for d in status["deployments"]}
    assert deploys["web"]["autoscale"]["target_cpu"] == 70


def test_deploy_with_partial_autoscale_params_rejected(api_client):
    res = api_client.post(
        "/api/deploy",
        json={"name": "web", "image": "nginx:alpine", "replicas": 1,
              "min_replicas": 2},
    )
    assert res.status_code == 400
    assert "together" in res.get_json()["error"]
