"""
Docker Client Wrapper
Handles all direct Docker operations through the Docker SDK
"""

from datetime import datetime
from typing import Dict, List, Optional

import docker
from docker.errors import ImageNotFound, NotFound

import logging

logger = logging.getLogger(__name__)


class DockerClientWrapper:
    """Wrapper class for Docker SDK operations"""

    def __init__(self):
        """Initialize Docker client connection"""
        try:
            self.client = docker.from_env()
            self.network_name = "k8s-lite-net"
            self._ensure_network()
            logger.info("Docker client initialized successfully")
        except Exception as e:
            logger.error(f"Failed to initialize Docker client: {e}")
            raise

    def _ensure_network(self):
        """Create the bridge network if it doesn't exist"""
        try:
            networks = self.client.networks.list(names=[self.network_name])
            if not networks:
                self.client.networks.create(
                    self.network_name,
                    driver="bridge",
                    check_duplicate=True
                )
                logger.info(f"Created network: {self.network_name}")
        except Exception as e:
            logger.warning(f"Network creation error: {e}")

    def create_container(self, image: str, name: str, deployment_name: str) -> str:
        """
        Create and start a container from an image

        Args:
            image: Docker image name (e.g., nginx:alpine)
            name: Container name (e.g., webapp-replica-1)
            deployment_name: Parent deployment name for labels

        Returns:
            container_id: Docker container ID
        """
        try:
            # Idempotent naming: clear any stale container (e.g. left exited
            # by an interrupted operation) squatting on the requested name,
            # otherwise creation would fail with a 409 conflict forever.
            try:
                stale = self.client.containers.get(name)
                logger.warning(f"Removing stale container {name} before create")
                stale.remove(force=True)
            except NotFound:
                pass
            except Exception as e:
                logger.error(f"Could not clear stale container {name}: {e}")

            # Pull image if not available locally
            try:
                self.client.images.get(image)
            except ImageNotFound:
                logger.info(f"Pulling image: {image}")
                self.client.images.pull(image)

            # Run container
            container = self.client.containers.run(
                image=image,
                name=name,
                detach=True,
                remove=False,
                network=self.network_name,
                labels={
                    'k8slite': 'true',
                    'deployment': deployment_name,
                    'managed_by': 'kubernetes-lite'
                }
            )
            logger.info(f"Created container: {name} (ID: {container.id[:12]})")
            return container.id

        except Exception as e:
            logger.error(f"Failed to create container {name}: {e}")
            raise

    def get_container_status(self, container_id: str) -> Dict:
        """
        Get detailed status of a container

        Returns:
            Dict with container information
        """
        try:
            container = self.client.containers.get(container_id)
            container.reload()

            # Calculate uptime (StartedAt is UTC)
            started_at = container.attrs['State'].get('StartedAt', '')
            uptime = ""
            if started_at:
                start_time = datetime.fromisoformat(started_at.replace('Z', '+00:00'))
                elapsed = datetime.now(start_time.tzinfo) - start_time
                uptime = str(elapsed).split('.')[0]

            return {
                'id': container_id[:12],
                'full_id': container_id,
                'name': container.name,
                'image': container.image.tags[0] if container.image.tags else 'unknown',
                'status': container.status,
                'exit_code': container.attrs['State'].get('ExitCode', 0),
                'started_at': started_at,
                'uptime': uptime,
                'deployment': container.labels.get('deployment', 'unknown')
            }
        except NotFound:
            return {
                'id': container_id[:12],
                'status': 'not_found',
                'exit_code': -1
            }
        except Exception as e:
            logger.error(f"Error getting container status: {e}")
            return {'id': container_id[:12], 'status': 'error', 'exit_code': -1}

    def get_container_ip(self, container_id: str) -> Optional[str]:
        """Get the container's IP address on the managed network"""
        try:
            container = self.client.containers.get(container_id)
            container.reload()
            networks = container.attrs.get('NetworkSettings', {}).get('Networks', {})
            for net in networks.values():
                ip = net.get('IPAddress')
                if ip:
                    return ip
            return None
        except Exception as e:
            logger.error(f"Error getting IP for container {container_id}: {e}")
            return None

    def get_container_stats(self, container_id: str) -> Optional[Dict]:
        """
        Sample CPU/memory usage for a running container (used by the
        auto-scaler). Returns None when the container is gone or stats
        cannot be read.
        """
        try:
            container = self.client.containers.get(container_id)
            stats = container.stats(stream=False)

            cpu = stats.get('cpu_stats', {})
            precpu = stats.get('precpu_stats', {})
            cpu_delta = (
                cpu.get('cpu_usage', {}).get('total_usage', 0)
                - precpu.get('cpu_usage', {}).get('total_usage', 0)
            )
            system_delta = (
                cpu.get('system_cpu_usage', 0) - precpu.get('system_cpu_usage', 0)
            )
            online_cpus = (
                cpu.get('online_cpus')
                or len(cpu.get('cpu_usage', {}).get('percpu_usage') or [])
                or 1
            )
            cpu_percent = (
                (cpu_delta / system_delta) * online_cpus * 100
                if system_delta > 0 else 0.0
            )

            mem = stats.get('memory_stats', {})
            mem_used = mem.get('usage', 0)
            mem_limit = mem.get('limit', 0) or 1
            return {
                'cpu_percent': round(max(0.0, cpu_percent), 2),
                'memory_used_bytes': mem_used,
                'memory_limit_bytes': mem_limit,
                'memory_percent': round(mem_used / mem_limit * 100, 2),
            }
        except NotFound:
            return None
        except Exception as e:
            logger.error(f"Error getting stats for container {container_id}: {e}")
            return None

    def run_load_balancer(self, deployment_name: str, host_port: int, setup_cmd: str) -> str:
        """
        Run an nginx reverse-proxy container that fronts a deployment's
        replicas. `setup_cmd` writes the generated config into the image's
        conf.d directory; nginx then starts in the foreground.
        The LB carries no 'deployment' label so it never shows up as a replica.
        """
        try:
            try:
                stale = self.client.containers.get(f"{deployment_name}-lb")
                logger.warning(f"Removing stale container {deployment_name}-lb before create")
                stale.remove(force=True)
            except NotFound:
                pass
            except Exception as e:
                logger.error(f"Could not clear stale LB container: {e}")

            try:
                self.client.images.get("nginx:alpine")
            except ImageNotFound:
                logger.info("Pulling image: nginx:alpine")
                self.client.images.pull("nginx:alpine")

            container = self.client.containers.run(
                image="nginx:alpine",
                name=f"{deployment_name}-lb",
                detach=True,
                remove=False,
                network=self.network_name,
                ports={'80/tcp': host_port},
                labels={
                    'k8slite': 'true',
                    'managed_by': 'kubernetes-lite',
                    'role': 'loadbalancer',
                    'lb_for': deployment_name,
                },
                command=["sh", "-c", setup_cmd + " && exec nginx -g 'daemon off;'"],
            )
            logger.info(
                f"Created load balancer {deployment_name}-lb on host port {host_port}"
            )
            return container.id
        except Exception as e:
            logger.error(f"Failed to create load balancer for {deployment_name}: {e}")
            raise

    def exec_in_container(self, container_id: str, command) -> bool:
        """Run a command inside a running container; True when exit code is 0"""
        try:
            container = self.client.containers.get(container_id)
            result = container.exec_run(command)
            if result.exit_code != 0:
                logger.error(
                    f"exec in {container_id[:12]} failed "
                    f"(exit {result.exit_code}): {result.output}"
                )
            return result.exit_code == 0
        except NotFound:
            return False
        except Exception as e:
            logger.error(f"Error executing in container {container_id}: {e}")
            return False

    def get_lb_container(self, deployment_name: str) -> Optional[Dict]:
        """Get the load-balancer container serving a deployment (if any)"""
        try:
            containers = self.client.containers.list(
                all=True, filters={'label': f'lb_for={deployment_name}'}
            )
            return self.get_container_status(containers[0].id) if containers else None
        except Exception as e:
            logger.error(f"Error looking up LB for {deployment_name}: {e}")
            return None

    def get_used_host_ports(self) -> set:
        """All host ports currently bound by any container on the daemon"""
        used = set()
        try:
            for c in self.client.containers.list(all=True):
                ports = c.attrs.get('NetworkSettings', {}).get('Ports') or {}
                for bindings in ports.values():
                    for b in bindings or []:
                        if b.get('HostPort'):
                            used.add(int(b['HostPort']))
        except Exception as e:
            logger.error(f"Error scanning host ports: {e}")
        return used

    def recreate_container(self, container_id: str, name: str, image: str, deployment_name: str) -> str:
        """Stop, remove, and recreate a container (used for unhealthy container restarts)"""
        try:
            self.stop_container(container_id)
            return self.create_container(image, name, deployment_name)
        except Exception as e:
            logger.error(f"Failed to recreate container {name}: {e}")
            raise

    def stop_container(self, container_id: str) -> bool:
        """Stop and remove a container"""
        try:
            container = self.client.containers.get(container_id)
            container.stop(timeout=10)
            container.remove()
            logger.info(f"Stopped and removed container: {container.name}")
            return True
        except NotFound:
            logger.warning(f"Container {container_id} already removed")
            return True
        except Exception as e:
            logger.error(f"Failed to stop container: {e}")
            return False

    def list_containers_by_deployment(self, deployment_name: str, include_all: bool = False) -> List[Dict]:
        """List all containers belonging to a deployment"""
        try:
            filters = {'label': f'deployment={deployment_name}'}
            if not include_all:
                filters['status'] = 'running'

            containers = self.client.containers.list(
                all=include_all,
                filters=filters
            )
            return [self.get_container_status(c.id) for c in containers]
        except Exception as e:
            logger.error(f"Error listing containers: {e}")
            return []

    def get_all_managed_containers(self) -> List[Dict]:
        """Get all containers managed by Kubernetes Lite"""
        try:
            containers = self.client.containers.list(
                all=True,
                filters={'label': 'k8slite=true'}
            )
            return [self.get_container_status(c.id) for c in containers]
        except Exception as e:
            logger.error(f"Error listing all containers: {e}")
            return []
