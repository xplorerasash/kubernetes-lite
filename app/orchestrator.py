"""
Orchestrator Engine
Handles deployment, scaling, rolling updates, and self-healing operations
"""

import base64
import logging
import math
import os
import re
import threading
import time
from typing import Dict, List, Optional

import requests

from .docker_client import DockerClientWrapper
from .state import StateManager, state_manager

logger = logging.getLogger(__name__)

DEFAULT_TARGET_CPU = 70


class Orchestrator:
    """Main orchestration engine"""

    def __init__(
        self,
        docker: Optional[DockerClientWrapper] = None,
        state: Optional[StateManager] = None,
    ):
        self.docker = docker or DockerClientWrapper()
        self.state = state or state_manager
        self.reconciliation_interval = 5  # seconds
        self.health_check_timeout = 3     # seconds
        self.health_fail_threshold = 2    # consecutive failures before restart
        self.update_wait_seconds = 2      # delay between rolling update steps
        self.autoscale_interval = int(    # seconds between auto-scaling passes
            os.environ.get("K8SLITE_AUTOSCALE_INTERVAL", "10")
        )
        self.scale_tolerance = 1.1        # ignore <10% deviation from target (HPA)
        self.autoscale_cooldown = 30      # min seconds between scaling actions
        self.lb_port_min = int(os.environ.get("K8SLITE_LB_PORT_MIN", "8000"))
        self.lb_port_max = int(os.environ.get("K8SLITE_LB_PORT_MAX", "8099"))
        self._lock = threading.Lock()
        self._running = True
        self._reconciliation_thread = None
        self._autoscaler_thread = None
        self._health_failures: Dict[str, int] = {}
        self._health_state: Dict[str, str] = {}
        self._last_scale_time: Dict[str, float] = {}
        self._metrics_cache: Dict[str, Dict] = {}
        self._lb_containers: Dict[str, str] = {}   # deployment -> LB container id
        self._lb_upstreams: Dict[str, tuple] = {}  # deployment -> last synced endpoints

    def _next_replica_index(self, deployment: Dict) -> int:
        """Find the next available replica index for a deployment"""
        existing_indices = set()
        for cid in deployment['container_ids']:
            container_info = self.docker.get_container_status(cid)
            name = container_info.get('name', '')
            match = re.search(r'replica-(\d+)$', name)
            if match:
                existing_indices.add(int(match.group(1)))
        return max(existing_indices, default=0) + 1

    def deploy(
        self,
        deployment_name: str,
        image: str,
        replicas: int,
        health_port: Optional[int] = None,
        health_path: Optional[str] = None,
    ) -> Dict:
        """
        Deploy a new containerized service

        Args:
            deployment_name: Unique name for the deployment
            image: Docker image to run
            replicas: Number of container replicas (1-10)
            health_port: Optional port to probe for HTTP health checks
            health_path: Optional URL path for health checks (default '/healthz')

        Returns:
            Result dictionary with deployment info
        """
        if self.state.get_deployment(deployment_name):
            return {"error": f"Deployment '{deployment_name}' already exists"}

        if replicas < 1 or replicas > 10:
            return {"error": "Replicas must be between 1 and 10"}

        container_ids = []

        try:
            with self._lock:
                for i in range(replicas):
                    container_name = f"{deployment_name}-replica-{i+1}"
                    container_id = self.docker.create_container(
                        image, container_name, deployment_name
                    )
                    container_ids.append(container_id)
                    self.state.add_container(container_id, deployment_name, image)

                self.state.add_deployment(
                    deployment_name,
                    image,
                    replicas,
                    container_ids,
                    health_port=health_port,
                    health_path=health_path or '/healthz',
                )

            return {
                "success": True,
                "deployment": deployment_name,
                "image": image,
                "replicas": replicas,
                "health_port": health_port,
                "health_path": health_path or '/healthz',
                "container_ids": container_ids
            }

        except Exception as e:
            logger.error(f"Deployment failed: {e}")
            for cid in container_ids:
                self.docker.stop_container(cid)
            return {"error": str(e)}

    def scale(self, deployment_name: str, new_replicas: int) -> Dict:
        """
        Scale a deployment up or down

        Args:
            deployment_name: Name of deployment to scale
            new_replicas: Desired replica count
        """
        deployment = self.state.get_deployment(deployment_name)
        if not deployment:
            return {"error": f"Deployment '{deployment_name}' not found"}

        if new_replicas < 0 or new_replicas > 10:
            return {"error": "Replicas must be between 0 and 10"}

        current_replicas = len(deployment['container_ids'])

        if new_replicas == current_replicas:
            return {"message": "Already at desired replica count"}

        try:
            with self._lock:
                if new_replicas > current_replicas:
                    # Scale Up
                    to_add = new_replicas - current_replicas
                    new_container_ids = deployment['container_ids'].copy()

                    next_idx = self._next_replica_index(deployment)

                    for i in range(to_add):
                        container_name = f"{deployment_name}-replica-{next_idx + i}"
                        container_id = self.docker.create_container(
                            deployment['image'], container_name, deployment_name
                        )
                        new_container_ids.append(container_id)
                        self.state.add_container(container_id, deployment_name, deployment['image'])

                    self.state.update_deployment_containers(deployment_name, new_container_ids)

                else:
                    # Scale Down
                    to_remove = current_replicas - new_replicas
                    containers_to_remove = deployment['container_ids'][-to_remove:]
                    for container_id in containers_to_remove:
                        self.docker.stop_container(container_id)
                        self.state.remove_container(container_id)

                    remaining = [
                        cid for cid in deployment['container_ids']
                        if cid not in containers_to_remove
                    ]
                    self.state.update_deployment_containers(deployment_name, remaining)

                self.state.update_deployment_replicas(deployment_name, new_replicas)

            result = {
                "success": True,
                "deployment": deployment_name,
                "old_replicas": current_replicas,
                "new_replicas": new_replicas
            }

        except Exception as e:
            logger.error(f"Scale operation failed: {e}")
            return {"error": str(e)}

        # Refresh LB upstreams immediately instead of waiting for reconcile
        if self.state.get_deployment(deployment_name) and \
                self.state.get_deployment(deployment_name).get('lb_port'):
            self._sync_load_balancer(deployment_name)

        return result

    def update_deployment(
        self,
        deployment_name: str,
        new_image: str,
        health_port: Optional[int] = None,
        health_path: Optional[str] = None,
    ) -> Dict:
        """
        Rolling update a deployment to a new image.
        Creates a new container with the new image before removing each old one,
        so the service stays available throughout the rollout.

        Args:
            deployment_name: Name of deployment to update
            new_image: New Docker image to roll out
            health_port: Optional health check port (kept from existing if None)
            health_path: Optional health check path (kept if None)

        Returns:
            Result dictionary
        """
        deployment = self.state.get_deployment(deployment_name)
        if not deployment:
            return {"error": f"Deployment '{deployment_name}' not found"}

        if deployment['image'] == new_image:
            return {"message": f"Deployment already running image {new_image}"}

        old_ids = list(deployment['container_ids'])
        new_ids: List[str] = []
        next_idx = self._next_replica_index(deployment)

        try:
            # Serialize with the reconciliation loop so it can't interfere
            with self._lock:
                for old_id in old_ids:
                    container_name = f"{deployment_name}-replica-{next_idx}"
                    logger.info(f"Rolling update: creating {container_name} with {new_image}")
                    new_id = self.docker.create_container(new_image, container_name, deployment_name)
                    new_ids.append(new_id)
                    self.state.add_container(new_id, deployment_name, new_image)

                    # Only remove the old container once the new one is created
                    self.docker.stop_container(old_id)
                    self.state.remove_container(old_id)
                    self._health_failures.pop(old_id, None)
                    self._health_state.pop(old_id, None)

                    next_idx += 1
                    time.sleep(self.update_wait_seconds)

                self.state.update_deployment_containers(deployment_name, new_ids)
                self.state.update_deployment_image(deployment_name, new_image)
                self.state.update_deployment_health(deployment_name, health_port, health_path)
                self.state.log_event(
                    "UPDATE",
                    f"Rolling update {deployment_name}: {deployment['image']} → {new_image}",
                )

            return {
                "success": True,
                "deployment": deployment_name,
                "old_image": deployment['image'],
                "new_image": new_image,
                "replicas": len(new_ids)
            }

        except Exception as e:
            logger.error(f"Rolling update failed: {e}")
            return {"error": str(e)}

    def configure_autoscaling(
        self,
        deployment_name: str,
        enabled: bool = True,
        min_replicas: Optional[int] = None,
        max_replicas: Optional[int] = None,
        target_cpu: Optional[int] = None,
    ) -> Dict:
        """
        Enable/disable HPA-style auto-scaling for a deployment.

        Args:
            deployment_name: Name of an existing deployment
            min_replicas: Lower replica bound (1-10, default: current/1)
            max_replicas: Upper replica bound (1-10, default: 10)
            target_cpu: Average CPU percent the scaler aims for (1-100)
        """
        deployment = self.state.get_deployment(deployment_name)
        if not deployment:
            return {"error": f"Deployment '{deployment_name}' not found"}

        if not enabled:
            if not self.state.update_autoscale(deployment_name, False):
                return {"error": f"Deployment '{deployment_name}' not found"}
            return {
                "success": True,
                "deployment": deployment_name,
                "autoscale": self.state.get_deployment(deployment_name)["autoscale"],
            }

        previous = deployment.get("autoscale") or {}
        lo = min_replicas if min_replicas is not None else (previous.get("min_replicas") or 1)
        hi = max_replicas if max_replicas is not None else (previous.get("max_replicas") or 10)
        tgt = target_cpu if target_cpu is not None else (previous.get("target_cpu") or DEFAULT_TARGET_CPU)

        if not all(isinstance(v, int) and not isinstance(v, bool) for v in (lo, hi, tgt)):
            return {"error": "min_replicas, max_replicas and target_cpu must be integers"}
        if lo < 1 or hi > 10:
            return {"error": "Replica bounds must be between 1 and 10"}
        if lo > hi:
            return {"error": "min_replicas cannot exceed max_replicas"}
        if not (1 <= tgt <= 100):
            return {"error": "target_cpu must be between 1 and 100"}

        if not self.state.update_autoscale(deployment_name, True, lo, hi, tgt):
            return {"error": f"Deployment '{deployment_name}' not found"}

        logger.info(
            f"Auto-scaling configured for {deployment_name}: "
            f"min={lo} max={hi} target={tgt}% CPU"
        )
        return {
            "success": True,
            "deployment": deployment_name,
            "autoscale": {
                "enabled": True,
                "min_replicas": lo,
                "max_replicas": hi,
                "target_cpu": tgt,
            },
        }

    def autoscale_once(self) -> None:
        """
        One pass of the auto-scaler (HPA-style): sample CPU across each
        enabled deployment's replicas, compute desired replicas
        proportionally to the target, and scale within [min, max].
        """
        for name, deployment in self.state.get_all_deployments().items():
            cfg = deployment.get("autoscale") or {}
            if not cfg.get("enabled"):
                continue

            replicas = self.docker.list_containers_by_deployment(name)
            if not replicas:
                continue  # no running replicas: reconcile loop handles healing

            samples = [
                s for s in (
                    self.docker.get_container_stats(c["full_id"]) for c in replicas
                ) if s is not None
            ]
            if not samples:
                continue

            avg_cpu = sum(s["cpu_percent"] for s in samples) / len(samples)
            self._metrics_cache[name] = {
                "avg_cpu": round(avg_cpu, 2),
                "replicas_sampled": len(samples),
                "sampled_at": time.time(),
            }

            desired = deployment["desired_replicas"]
            target = cfg.get("target_cpu") or DEFAULT_TARGET_CPU
            lo = cfg.get("min_replicas") or 1
            hi = cfg.get("max_replicas") or 10

            ratio = avg_cpu / target
            if ratio > self.scale_tolerance:
                proposed = math.ceil(desired * ratio)
            elif ratio < 1 / self.scale_tolerance:
                proposed = math.floor(desired * ratio)
            else:
                proposed = desired
            proposed = max(lo, min(hi, proposed))

            now = time.time()
            if proposed == desired:
                continue
            if now - self._last_scale_time.get(name, 0) < self.autoscale_cooldown:
                logger.info(
                    f"Auto-scale of {name} {desired} -> {proposed} suppressed by cooldown"
                )
                continue

            result = self.scale(name, proposed)
            if result.get("success"):
                self._last_scale_time[name] = now
                self.state.log_event(
                    "AUTOSCALE",
                    f"CPU {avg_cpu:.0f}% vs target {target}%: "
                    f"scaled {name} {desired} -> {proposed}",
                )

    def _autoscaling_loop(self):
        """Background thread that periodically runs the auto-scaler"""
        logger.info("Auto-scaling loop started")

        while self._running:
            try:
                self.autoscale_once()
            except Exception as e:
                logger.error(f"Auto-scaling loop error: {e}")
            time.sleep(self.autoscale_interval)

    # ---- Load balancing ----

    def _allocate_host_port(self) -> Optional[int]:
        """Pick the lowest free host port in the configured LB port range"""
        used = self.docker.get_used_host_ports()
        allocated = {
            d["lb_port"] for d in self.state.get_all_deployments().values()
            if d.get("lb_port")
        }
        for port in range(self.lb_port_min, self.lb_port_max + 1):
            if port not in used and port not in allocated:
                return port
        return None

    @staticmethod
    def _render_nginx_conf(endpoints: List[str], target_port: int) -> str:
        """nginx conf.d snippet: upstream pool of replica IPs + proxy server"""
        if not endpoints:
            return (
                "server {\n"
                "    listen 80;\n\n"
                "    location / {\n"
                "        return 503;\n"
                "    }\n"
                "}\n"
            )
        servers = "\n".join(f"    server {ip}:{target_port};" for ip in endpoints)
        return (
            "upstream backend {\n"
            f"{servers}\n"
            "}\n\n"
            "server {\n"
            "    listen 80;\n\n"
            "    location / {\n"
            "        proxy_pass http://backend;\n"
            "        proxy_next_upstream error timeout http_502 http_503 http_504;\n"
            "        proxy_connect_timeout 2s;\n"
            "    }\n"
            "}\n"
        )

    @staticmethod
    def _conf_to_cmd(conf: str, reload_only: bool = False) -> str:
        """Base64-encode a config and wrap it in an sh command (no quoting issues)"""
        b64 = base64.b64encode(conf.encode()).decode()
        suffix = " && nginx -s reload" if reload_only else ""
        return f"echo {b64} | base64 -d > /etc/nginx/conf.d/default.conf{suffix}"

    def _running_endpoints(self, deployment_name: str) -> List[str]:
        """IP addresses of all running replicas of a deployment"""
        ips = []
        for c in self.docker.list_containers_by_deployment(deployment_name):
            ip = self.docker.get_container_ip(c["full_id"])
            if ip:
                ips.append(ip)
        return sorted(ips)

    def enable_load_balancer(
        self,
        deployment_name: str,
        enabled: bool = True,
        target_port: Optional[int] = None,
    ) -> Dict:
        """
        Enable/disable the nginx load balancer fronting a deployment.

        Args:
            deployment_name: Existing deployment name
            target_port: Port replicas serve on (default: health_port, else 80)
        """
        deployment = self.state.get_deployment(deployment_name)
        if not deployment:
            return {"error": f"Deployment '{deployment_name}' not found"}

        if not enabled:
            lb_id = self._lb_containers.pop(deployment_name, None)
            if not lb_id:
                existing = self.docker.get_lb_container(deployment_name)
                lb_id = existing.get("full_id") if existing else None
            if lb_id:
                self.docker.stop_container(lb_id)
            self.state.update_load_balancer(deployment_name, None, None)
            self._lb_upstreams.pop(deployment_name, None)
            return {"success": True, "deployment": deployment_name,
                    "lb": {"enabled": False}}

        if target_port is not None and (
            not isinstance(target_port, int) or not (1 <= target_port <= 65535)
        ):
            return {"error": "target_port must be between 1 and 65535"}

        port = deployment.get("lb_port") or self._allocate_host_port()
        if not port:
            return {
                "error": f"No free host ports in range "
                         f"{self.lb_port_min}-{self.lb_port_max}"
            }

        tgt = target_port or deployment.get("lb_target_port") \
            or deployment.get("health_port") or 80
        self.state.update_load_balancer(deployment_name, port, tgt)
        # Force a config push even if the endpoint set didn't change
        self._lb_upstreams.pop(deployment_name, None)
        self._sync_load_balancer(deployment_name)

        return {
            "success": True,
            "deployment": deployment_name,
            "lb": {"enabled": True, "port": port, "target_port": tgt},
        }

    def _sync_load_balancer(self, deployment_name: str) -> None:
        """
        Converge the deployment's load balancer on reality:
        recreate it when missing/killed, rewrite its upstream pool whenever
        the set of running replicas changed. Called by the reconcile loop.
        """
        deployment = self.state.get_deployment(deployment_name)
        if not deployment or not deployment.get("lb_port"):
            return
        port = deployment["lb_port"]
        target = deployment.get("lb_target_port") or 80

        endpoints = self._running_endpoints(deployment_name)
        signature = tuple(endpoints)

        lb_info = self.docker.get_lb_container(deployment_name)
        lb_running = bool(lb_info and lb_info.get("status") == "running")

        if not lb_running:
            if lb_info:
                self.docker.stop_container(lb_info["full_id"])
            conf = self._render_nginx_conf(endpoints, target)
            try:
                lb_id = self.docker.run_load_balancer(
                    deployment_name, port, self._conf_to_cmd(conf)
                )
            except Exception as e:
                logger.error(f"Could not create load balancer for "
                             f"{deployment_name}: {e}")
                return
            self._lb_containers[deployment_name] = lb_id
            self._lb_upstreams[deployment_name] = signature
            logger.info(
                f"LB for {deployment_name} serving {len(endpoints)} upstream(s)"
                + ("" if endpoints else " (503 until replicas are running)")
            )
            return

        if signature == self._lb_upstreams.get(deployment_name):
            return  # nothing changed since last sync

        lb_id = lb_info["full_id"]
        cmd = self._conf_to_cmd(self._render_nginx_conf(endpoints, target),
                                reload_only=True)
        if self.docker.exec_in_container(lb_id, ["sh", "-c", cmd]):
            self._lb_upstreams[deployment_name] = signature
            logger.info(f"LB for {deployment_name} now routes to "
                        f"{len(endpoints)} replica(s)")
        else:
            # Reload failed: fall back to a clean recreation
            self.docker.stop_container(lb_id)
            self._lb_containers.pop(deployment_name, None)
            self._sync_load_balancer(deployment_name)

    def _is_healthy(self, deployment: Dict, container_id: str) -> bool:
        """Probe the container's HTTP health endpoint"""
        health_port = deployment.get('health_port')
        if not health_port:
            return True

        health_path = deployment.get('health_path') or '/healthz'
        ip = self.docker.get_container_ip(container_id)
        if not ip:
            return False

        try:
            response = requests.get(
                f"http://{ip}:{health_port}{health_path}",
                timeout=self.health_check_timeout,
            )
            return 200 <= response.status_code < 500
        except Exception:
            return False

    def _check_and_restart(self, deployment: Dict, container_id: str) -> None:
        """
        Health-check a running container and restart it after
        consecutive failures
        """
        container_name = self.docker.get_container_status(container_id).get('name', container_id)

        if self._is_healthy(deployment, container_id):
            self._health_failures.pop(container_id, None)
            self._health_state[container_id] = 'healthy'
            return

        self._health_state[container_id] = 'unhealthy'
        failures = self._health_failures.get(container_id, 0) + 1
        self._health_failures[container_id] = failures

        if failures < self.health_fail_threshold:
            logger.warning(
                f"Container {container_id[:12]} failed health check "
                f"({failures}/{self.health_fail_threshold}), waiting..."
            )
            return

        logger.warning(f"Restarting unhealthy container {container_name}")
        try:
            new_id = self.docker.recreate_container(
                container_id, container_name, deployment['image'], deployment['name']
            )
        except Exception as e:
            logger.error(f"Failed to restart {container_name}: {e}")
            return

        # Update state: swap old container id for new
        if container_id in deployment['container_ids']:
            deployment['container_ids'][
                deployment['container_ids'].index(container_id)
            ] = new_id
        self.state.remove_container(container_id)
        self.state.add_container(new_id, deployment['name'], deployment['image'])
        self.state.increment_heal_count(deployment['name'])
        self.state.log_event("HEAL", f"Restarted unhealthy container {container_name}")

        self._health_failures.pop(container_id, None)
        self._health_state.pop(container_id, None)
        self._health_state[new_id] = 'healthy'

    def reconcile_once(self) -> None:
        """Single pass enforcing desired state (deployed count + health)"""
        with self._lock:
            self._reconcile_locked()

    def _reconcile_locked(self) -> None:
        """Reconcile pass, assumes the state lock is already held"""
        deployments = self.state.get_all_deployments()

        for deployment_name, deployment in deployments.items():
            desired = deployment['desired_replicas']

            # Currently running containers
            running_containers = self.docker.list_containers_by_deployment(deployment_name)
            running_ids = {c['full_id'] for c in running_containers}

            # Remove failed containers (tracked but not running)
            tracked_ids = set(deployment['container_ids'])
            failed_ids = tracked_ids - running_ids
            for failed_id in failed_ids:
                logger.warning(f"Container {failed_id[:12]} failed, removing from state")
                # Also remove the actual Docker container (it may be stopped/exited
                # but still exist, which would block reuse of its name)
                self.docker.stop_container(failed_id)
                self.state.remove_container(failed_id)
                deployment['container_ids'].remove(failed_id)

            # Health-check running containers
            for cid in list(running_ids):
                self._check_and_restart(deployment, cid)

            # Re-list after potential restarts
            running_containers = self.docker.list_containers_by_deployment(deployment_name)
            running_ids = {c['full_id'] for c in running_containers}
            actual_count = len([cid for cid in deployment['container_ids'] if cid in running_ids])

            # Heal if needed
            if actual_count < desired:
                deficit = desired - actual_count
                logger.info(f"Self-healing: Need {deficit} new containers for {deployment_name}")

                next_idx = self._next_replica_index(deployment)

                for i in range(deficit):
                    container_name = f"{deployment_name}-replica-{next_idx + i}"
                    try:
                        container_id = self.docker.create_container(
                            deployment['image'], container_name, deployment_name
                        )
                        deployment['container_ids'].append(container_id)
                        self.state.add_container(container_id, deployment_name, deployment['image'])
                        self.state.increment_heal_count(deployment_name)
                        self.state.log_event("HEAL", f"Created replacement container {container_name}")
                        logger.info(f"Self-healed: Created {container_name}")
                    except Exception as e:
                        logger.error(f"Self-healing create failed: {e}")

            self.state.update_deployment_containers(deployment_name, deployment['container_ids'])

            # Keep the load balancer's upstream pool in sync with reality
            if deployment.get('lb_port'):
                self._sync_load_balancer(deployment_name)

    def _reconciliation_loop(self):
        """Background thread that enforces desired state"""
        logger.info("Self-healing reconciliation loop started")

        while self._running:
            try:
                self.reconcile_once()
            except Exception as e:
                logger.error(f"Reconciliation loop error: {e}")
            time.sleep(self.reconciliation_interval)

    def start(self):
        """Start the orchestrator background threads"""
        if self._reconciliation_thread is None or not self._reconciliation_thread.is_alive():
            self._running = True
            self._reconciliation_thread = threading.Thread(target=self._reconciliation_loop, daemon=True)
            self._reconciliation_thread.start()
            logger.info("Orchestrator started")

        if self._autoscaler_thread is None or not self._autoscaler_thread.is_alive():
            self._running = True
            self._autoscaler_thread = threading.Thread(target=self._autoscaling_loop, daemon=True)
            self._autoscaler_thread.start()

    def stop(self):
        """Stop the orchestrator"""
        self._running = False
        logger.info("Orchestrator stopping")

    def get_status(self) -> Dict:
        """Get current system status"""
        deployments = []
        all_deployments = self.state.get_all_deployments()

        for name, deployment in all_deployments.items():
            running_containers = self.docker.list_containers_by_deployment(name)
            for c in running_containers:
                c['health'] = self._health_state.get(c['full_id'], 'unknown')

            metrics = self._metrics_cache.get(name, {})
            deployments.append({
                'name': name,
                'image': deployment['image'],
                'desired': deployment['desired_replicas'],
                'actual': len(deployment['container_ids']),
                'running': len(running_containers),
                'heal_count': deployment['heal_count'],
                'health_port': deployment.get('health_port'),
                'health_path': deployment.get('health_path'),
                'autoscale': deployment.get('autoscale'),
                'avg_cpu': metrics.get('avg_cpu'),
                'lb': (
                    {'port': deployment['lb_port'],
                     'target_port': deployment.get('lb_target_port')}
                    if deployment.get('lb_port') else None
                ),
                'containers': running_containers
            })

        return {
            'deployments': deployments,
            'summary': self.state.get_summary(),
            'events': self.state.get_events(20)
        }

    def delete_deployment(self, deployment_name: str) -> Dict:
        """Delete a deployment and all its containers"""
        deployment = self.state.get_deployment(deployment_name)
        if not deployment:
            return {"error": "Deployment not found"}

        with self._lock:
            deployment = self.state.get_deployment(deployment_name)
            if not deployment:
                return {"error": "Deployment not found"}

            for container_id in deployment['container_ids']:
                self.docker.stop_container(container_id)

            lb_id = self._lb_containers.pop(deployment_name, None)
            if not lb_id:
                existing = self.docker.get_lb_container(deployment_name)
                lb_id = existing.get("full_id") if existing else None
            if lb_id:
                self.docker.stop_container(lb_id)
            self._lb_upstreams.pop(deployment_name, None)

            self.state.remove_deployment(deployment_name)

            # Residual sweep: any labeled container that slipped through
            # (e.g. created by a concurrent heal, or a silent stop failure)
            for c in self.docker.list_containers_by_deployment(deployment_name, include_all=True):
                logger.warning(f"Sweeping leftover container {c['name']}")
                self.docker.stop_container(c['full_id'])

        return {"success": True, "deployment": deployment_name}
