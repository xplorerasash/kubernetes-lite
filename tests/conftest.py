"""Shared fixtures: an in-memory fake Docker client + app factory helpers."""

import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.orchestrator import Orchestrator  # noqa: E402
from app.state import StateManager  # noqa: E402


class FakeDockerClient:
    """Stands in for the Docker SDK client (only what /api/health needs)."""

    def ping(self):
        return True


class FakeDocker:
    """In-memory fake of DockerClientWrapper that needs no Docker daemon"""

    def __init__(self):
        self.containers = {}  # container_id -> info dict
        self.exec_log = []    # (container_id, command) pairs from exec_in_container
        self._next_id = 1000
        self.client = FakeDockerClient()

    def _new_id(self):
        self._next_id += 1
        return f"cid{self._next_id}"

    def create_container(self, image, name, deployment_name):
        cid = self._new_id()
        self.containers[cid] = {
            'full_id': cid,
            'name': name,
            'image': image,
            'deployment': deployment_name,
            'status': 'running',
            'ip': f"172.18.0.{self._next_id % 250 + 1}",
            'cpu_percent': 5.0,
        }
        return cid

    def set_cpu(self, container_id, percent):
        """Pretend a container is consuming `percent` CPU (for autoscaler tests)"""
        if container_id in self.containers:
            self.containers[container_id]['cpu_percent'] = percent

    def get_container_stats(self, container_id):
        info = self.containers.get(container_id)
        if not info or info['status'] != 'running':
            return None
        return {
            'cpu_percent': info.get('cpu_percent', 5.0),
            'memory_used_bytes': 32 * 1024 * 1024,
            'memory_limit_bytes': 512 * 1024 * 1024,
            'memory_percent': 6.25,
        }

    def stop_container(self, container_id):
        self.containers.pop(container_id, None)
        return True

    def kill_container(self, container_id):
        """Simulate `docker kill`: stop the process but keep the container object"""
        if container_id in self.containers:
            self.containers[container_id]['status'] = 'exited'
        return True

    def recreate_container(self, container_id, name, image, deployment_name):
        self.stop_container(container_id)
        return self.create_container(image, name, deployment_name)

    def get_container_ip(self, container_id):
        info = self.containers.get(container_id)
        return info['ip'] if info else None

    def get_container_status(self, container_id):
        info = self.containers.get(container_id)
        if not info:
            return {'id': container_id[:12], 'status': 'not_found', 'exit_code': -1}
        return {
            'id': container_id[:12],
            'full_id': info['full_id'],
            'name': info['name'],
            'image': info['image'],
            'status': info['status'],
            'exit_code': 0,
            'uptime': '0:00:01',
            'deployment': info['deployment'],
        }

    def list_containers_by_deployment(self, deployment_name, include_all=False):
        return [
            self.get_container_status(cid)
            for cid, info in self.containers.items()
            if info['deployment'] == deployment_name
            and (include_all or info['status'] == 'running')
        ]

    def get_all_managed_containers(self):
        return [self.get_container_status(cid) for cid in self.containers]

    # ---- Load balancer support ----

    def run_load_balancer(self, deployment_name, host_port, setup_cmd):
        cid = self._new_id()
        self.containers[cid] = {
            'full_id': cid,
            'name': f"{deployment_name}-lb",
            'image': 'nginx:alpine',
            'deployment': None,  # no deployment label: never listed as a replica
            'role': 'loadbalancer',
            'lb_for': deployment_name,
            'status': 'running',
            'host_port': host_port,
            'ip': f"172.18.0.{self._next_id % 250 + 1}",
            'cpu_percent': 2.0,
        }
        self.exec_log.append((cid, ['sh', '-c', setup_cmd]))
        return cid

    def exec_in_container(self, container_id, command):
        info = self.containers.get(container_id)
        if not info or info['status'] != 'running':
            return False
        self.exec_log.append((container_id, command))
        return True

    def get_lb_container(self, deployment_name):
        for cid, info in self.containers.items():
            if info.get('lb_for') == deployment_name and info.get('role') == 'loadbalancer':
                status = self.get_container_status(cid)
                status['host_port'] = info.get('host_port')
                return status
        return None

    def get_used_host_ports(self):
        return {i['host_port'] for i in self.containers.values() if i.get('host_port')}


@pytest.fixture
def orchestrator():
    state = StateManager(db_path=":memory:")
    fake = FakeDocker()
    orch = Orchestrator(docker=fake, state=state)
    yield orch


@pytest.fixture
def api_client(monkeypatch, tmp_path):
    """Flask test client wired to a fake-Docker orchestrator."""
    from app.main import create_app

    monkeypatch.setenv("K8SLITE_BACKUP_DIR", str(tmp_path / "backups"))
    orch = Orchestrator(docker=FakeDocker(), state=StateManager(db_path=":memory:"))
    app = create_app(orchestrator=orch)
    app.config["TESTING"] = True
    with app.test_client() as client:
        client.orchestrator = orch
        yield client
