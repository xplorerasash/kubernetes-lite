import os

from app.state import StateManager

# FakeDocker / orchestrator fixtures come from tests/conftest.py


def test_deploy_creates_replicas(orchestrator):
    result = orchestrator.deploy("web", "nginx:alpine", 3)

    assert result["success"] is True
    assert result["replicas"] == 3
    assert len(result["container_ids"]) == 3

    deployment = orchestrator.state.get_deployment("web")
    assert deployment["image"] == "nginx:alpine"
    assert deployment["desired_replicas"] == 3
    assert len(deployment["container_ids"]) == 3


def test_deploy_rejects_duplicate(orchestrator):
    orchestrator.deploy("web", "nginx:alpine", 1)
    result = orchestrator.deploy("web", "nginx:alpine", 1)
    assert "error" in result


def test_scale_up_and_down(orchestrator):
    orchestrator.deploy("web", "nginx:alpine", 2)

    result = orchestrator.scale("web", 4)
    assert result["success"] is True
    assert orchestrator.state.get_deployment("web")["desired_replicas"] == 4
    assert len(orchestrator.state.get_deployment("web")["container_ids"]) == 4

    result = orchestrator.scale("web", 1)
    assert result["success"] is True
    assert len(orchestrator.state.get_deployment("web")["container_ids"]) == 1


def test_self_heal_replaces_dead_container(orchestrator):
    result = orchestrator.deploy("web", "nginx:alpine", 2)
    dead_id = result["container_ids"][0]

    # Simulate a container dying outside the orchestrator's control
    orchestrator.docker.containers.pop(dead_id)

    orchestrator.reconcile_once()

    deployment = orchestrator.state.get_deployment("web")
    assert dead_id not in deployment["container_ids"]
    assert len(deployment["container_ids"]) == 2
    assert deployment["heal_count"] >= 1


def test_self_heal_after_docker_kill(orchestrator):
    """Regression test: `docker kill` leaves a stopped container behind whose
    name still exists, so the orchestrator must remove it before recreating."""
    result = orchestrator.deploy("web", "nginx:alpine", 2)
    killed_id = result["container_ids"][0]

    orchestrator.docker.kill_container(killed_id)

    orchestrator.reconcile_once()

    deployment = orchestrator.state.get_deployment("web")
    assert killed_id not in deployment["container_ids"]
    assert len(deployment["container_ids"]) == 2
    assert deployment["heal_count"] >= 1
    # No stale stopped container remains under the deployment's name
    assert len(orchestrator.docker.get_all_managed_containers()) == 2


def test_rolling_update_swaps_image(orchestrator):
    orchestrator.deploy("web", "nginx:1.20", 2)
    orchestrator.update_wait_seconds = 0

    result = orchestrator.update_deployment("web", "nginx:1.25")
    assert result["success"] is True
    assert result["new_image"] == "nginx:1.25"

    deployment = orchestrator.state.get_deployment("web")
    assert deployment["image"] == "nginx:1.25"
    assert len(deployment["container_ids"]) == 2

    # All running replicas now use the new image
    images = {
        info["image"]
        for info in orchestrator.docker.containers.values()
        if info["deployment"] == "web"
    }
    assert images == {"nginx:1.25"}


def test_health_check_restarts_unhealthy(orchestrator, monkeypatch):
    orchestrator.deploy("web", "nginx:alpine", 1, health_port=80, health_path="/healthz")
    original_id = orchestrator.state.get_deployment("web")["container_ids"][0]

    # Health endpoint always fails
    class Unhealthy:
        status_code = 503

    monkeypatch.setattr("app.orchestrator.requests.get", lambda *a, **k: Unhealthy())

    # One failure is below the threshold, no restart yet
    orchestrator.reconcile_once()
    assert orchestrator.state.get_deployment("web")["container_ids"] == [original_id]

    # Second consecutive failure triggers restart
    orchestrator.reconcile_once()
    new_id = orchestrator.state.get_deployment("web")["container_ids"][0]
    assert new_id != original_id
    assert orchestrator.state.get_deployment("web")["heal_count"] >= 1


def test_healthy_container_not_restarted(orchestrator, monkeypatch):
    orchestrator.deploy("web", "nginx:alpine", 1, health_port=80, health_path="/healthz")
    original_id = orchestrator.state.get_deployment("web")["container_ids"][0]

    class Healthy:
        status_code = 200

    monkeypatch.setattr("app.orchestrator.requests.get", lambda *a, **k: Healthy())

    orchestrator.reconcile_once()
    orchestrator.reconcile_once()

    assert orchestrator.state.get_deployment("web")["container_ids"] == [original_id]
    assert orchestrator.state.get_deployment("web")["heal_count"] == 0


def test_delete_deployment(orchestrator):
    orchestrator.deploy("web", "nginx:alpine", 2)
    result = orchestrator.delete_deployment("web")
    assert result["success"] is True
    assert orchestrator.state.get_deployment("web") is None
    assert orchestrator.docker.get_all_managed_containers() == []


def test_state_persists_across_instances():
    import tempfile

    with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as f:
        db_path = f.name
    try:
        state1 = StateManager(db_path=db_path)
        state1.add_deployment("web", "nginx:alpine", 2, ["a", "b"])
        state1.close()

        # New manager over the same file sees the persisted state
        state2 = StateManager(db_path=db_path)
        deployment = state2.get_deployment("web")
        assert deployment is not None
        assert deployment["image"] == "nginx:alpine"
        assert deployment["container_ids"] == ["a", "b"]
        state2.close()
    finally:
        os.unlink(db_path)


def test_summary_counts(orchestrator):
    orchestrator.deploy("web", "nginx:alpine", 2)
    summary = orchestrator.state.get_summary()
    assert summary["deployment_count"] == 1
    assert summary["total_desired_replicas"] == 2
    assert summary["total_tracked_containers"] == 2


def test_rolling_update_not_interfered_by_reconcile(orchestrator):
    """Regression test: a reconcile pass during a rolling update must not
    recreate old-image containers (race between loop and update)."""
    import threading
    import time

    orchestrator.deploy("web", "nginx:1.20", 2)
    orchestrator.update_wait_seconds = 0.2

    # Run the update in a background thread so reconcile can race with it
    result = {}

    def do_update():
        result["out"] = orchestrator.update_deployment("web", "nginx:1.25")

    t = threading.Thread(target=do_update)
    t.start()
    time.sleep(0.05)  # let the update start, then fire reconcile

    orchestrator.reconcile_once()
    t.join()

    assert result["out"]["success"] is True
    deployment = orchestrator.state.get_deployment("web")
    assert len(deployment["container_ids"]) == 2
    assert deployment["image"] == "nginx:1.25"

    # All managed containers must be the new image, exactly 2 of them
    managed = orchestrator.docker.get_all_managed_containers()
    assert len(managed) == 2
    images = {c["image"] for c in managed}
    assert images == {"nginx:1.25"}
