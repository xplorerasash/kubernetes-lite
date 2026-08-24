"""
Kubernetes Lite - Main Flask Application
REST API for container orchestration
"""

import logging
import os
import signal
import sys

from dotenv import load_dotenv
from flask import Flask, jsonify, render_template, request
from flask_cors import CORS

from .orchestrator import Orchestrator

load_dotenv()

logger = logging.getLogger(__name__)


def create_app(orchestrator: Orchestrator = None) -> Flask:
    """
    Application factory. Accepting an injected orchestrator (with a fake
    Docker client) lets the whole API be tested without a Docker daemon.
    """
    app = Flask(__name__, static_folder='../static', template_folder='../templates')
    CORS(app)
    orch = orchestrator or Orchestrator()
    orch.start()  # idempotent: safe to call again

    @app.route('/')
    def dashboard():
        """Serve the web dashboard"""
        return render_template('dashboard.html')

    @app.route('/api/status', methods=['GET'])
    def get_status():
        """Get complete system status"""
        return jsonify(orch.get_status())

    @app.route('/api/deploy', methods=['POST'])
    def deploy():
        """Deploy a new containerized service"""
        data = request.get_json(silent=True)

        if not data:
            return jsonify({"error": "No data provided"}), 400

        name = data.get('name', '').strip()
        image = data.get('image', '').strip()
        replicas = data.get('replicas', 1)
        health_port = data.get('health_port')
        health_path = data.get('health_path', '/healthz')
        min_replicas = data.get('min_replicas')
        max_replicas = data.get('max_replicas')
        target_cpu = data.get('target_cpu')

        if not name:
            return jsonify({"error": "Deployment name required"}), 400
        if not image:
            return jsonify({"error": "Image name required"}), 400

        if not isinstance(replicas, int) or replicas < 1 or replicas > 10:
            return jsonify({"error": "Replicas must be between 1 and 10"}), 400

        if health_port is not None and (
            not isinstance(health_port, int) or not (1 <= health_port <= 65535)
        ):
            return jsonify({"error": "Health port must be between 1 and 65535"}), 400

        autoscale_requested = any(
            v is not None for v in (min_replicas, max_replicas, target_cpu)
        )
        if autoscale_requested and any(
            v is None for v in (min_replicas, max_replicas, target_cpu)
        ):
            return jsonify({
                "error": "Auto-scaling requires min_replicas, max_replicas "
                         "and target_cpu together"
            }), 400

        result = orch.deploy(name, image, replicas, health_port, health_path)

        if result.get('error'):
            return jsonify(result), 400

        if autoscale_requested:
            cfg = orch.configure_autoscaling(
                name,
                enabled=True,
                min_replicas=min_replicas,
                max_replicas=max_replicas,
                target_cpu=target_cpu,
            )
            if cfg.get('error'):
                result['warning'] = f"Deployed, but auto-scaling was not enabled: {cfg['error']}"
            else:
                result['autoscale'] = cfg['autoscale']

        return jsonify(result), 201

    @app.route('/api/scale', methods=['POST'])
    def scale():
        """Scale a deployment"""
        data = request.get_json(silent=True)

        if not data:
            return jsonify({"error": "No data provided"}), 400

        name = data.get('name', '').strip()
        replicas = data.get('replicas')

        if not name:
            return jsonify({"error": "Deployment name required"}), 400

        if not isinstance(replicas, int) or replicas < 0 or replicas > 10:
            return jsonify({"error": "Replicas must be between 0 and 10"}), 400

        result = orch.scale(name, replicas)

        if result.get('error'):
            return jsonify(result), 400

        return jsonify(result)

    @app.route('/api/autoscale', methods=['POST'])
    def configure_autoscale():
        """Enable/disable/configure HPA-style auto-scaling for a deployment"""
        data = request.get_json(silent=True)

        if not data:
            return jsonify({"error": "No data provided"}), 400

        name = data.get('name', '').strip()
        if not name:
            return jsonify({"error": "Deployment name required"}), 400

        result = orch.configure_autoscaling(
            name,
            enabled=bool(data.get('enabled', True)),
            min_replicas=data.get('min_replicas'),
            max_replicas=data.get('max_replicas'),
            target_cpu=data.get('target_cpu'),
        )

        if result.get('error'):
            return jsonify(result), 400

        return jsonify(result)

    @app.route('/api/lb', methods=['POST'])
    def load_balancer():
        """Enable/disable the nginx load balancer fronting a deployment"""
        data = request.get_json(silent=True)

        if not data:
            return jsonify({"error": "No data provided"}), 400

        name = data.get('name', '').strip()
        if not name:
            return jsonify({"error": "Deployment name required"}), 400

        target_port = data.get('target_port')
        if target_port is not None and (
            not isinstance(target_port, int) or not (1 <= target_port <= 65535)
        ):
            return jsonify({"error": "target_port must be between 1 and 65535"}), 400

        result = orch.enable_load_balancer(
            name, enabled=bool(data.get('enabled', True)), target_port=target_port
        )

        if result.get('error'):
            return jsonify(result), 400

        return jsonify(result)

    @app.route('/api/update', methods=['POST'])
    def update_deployment():
        """Rolling update a deployment to a new image"""
        data = request.get_json(silent=True)

        if not data:
            return jsonify({"error": "No data provided"}), 400

        name = data.get('name', '').strip()
        image = data.get('image', '').strip()
        health_port = data.get('health_port')
        health_path = data.get('health_path')

        if not name:
            return jsonify({"error": "Deployment name required"}), 400
        if not image:
            return jsonify({"error": "Image name required"}), 400

        result = orch.update_deployment(name, image, health_port, health_path)

        if result.get('error'):
            return jsonify(result), 400

        return jsonify(result)

    @app.route('/api/delete/<deployment_name>', methods=['DELETE'])
    def delete_deployment(deployment_name):
        """Delete a deployment and all its containers"""
        result = orch.delete_deployment(deployment_name)

        if result.get('error'):
            return jsonify(result), 404

        return jsonify(result)

    @app.route('/api/events', methods=['GET'])
    def get_events():
        """Get orchestration event log"""
        limit = request.args.get('limit', 50, type=int)
        events = orch.state.get_events(limit)
        return jsonify({'events': events, 'count': len(events)})

    @app.route('/api/containers', methods=['GET'])
    def get_containers():
        """Get all containers managed by the system"""
        containers = orch.docker.get_all_managed_containers()
        return jsonify({'containers': containers, 'count': len(containers)})

    @app.route('/api/health', methods=['GET'])
    def health_check():
        """Health check endpoint"""
        try:
            # Check Docker connectivity
            orch.docker.client.ping()
            return jsonify({
                'status': 'healthy',
                'docker': 'connected',
                'deployments': len(orch.state.get_all_deployments())
            })
        except Exception as e:
            return jsonify({
                'status': 'unhealthy',
                'error': str(e)
            }), 503

    # ---- Maintenance ----

    @app.route('/api/maintenance/backup', methods=['POST'])
    def create_backup():
        """Create a consistent snapshot of the orchestrator database"""
        try:
            path = orch.state.backup()
            logger.info(f"Backup created: {path}")
            return jsonify({'success': True, 'backup': path}), 201
        except Exception as e:
            logger.error(f"Backup failed: {e}")
            return jsonify({'success': False, 'error': str(e)}), 500

    @app.route('/api/maintenance/backups', methods=['GET'])
    def list_backups():
        """List available backups with size and creation time"""
        try:
            return jsonify({'backups': orch.state.list_backups()})
        except Exception as e:
            return jsonify({'error': str(e)}), 500

    @app.errorhandler(404)
    def not_found(_):
        return jsonify({'error': 'Not found'}), 404

    @app.errorhandler(405)
    def method_not_allowed(_):
        return jsonify({'error': 'Method not allowed'}), 405

    return app


def shutdown_handler(signum, frame):
    """Handle shutdown gracefully"""
    logger.info("Shutting down orchestrator...")
    sys.exit(0)


def __getattr__(name):
    """
    Lazily build the real app (with a live Docker connection) only when
    something actually asks for it - e.g. `waitress-serve app.main:app`.
    Importing this module stays side-effect free so tests can inject fakes.
    """
    if name == 'app':
        return create_app()
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")


if __name__ == '__main__':
    app = create_app()

    signal.signal(signal.SIGINT, shutdown_handler)
    signal.signal(signal.SIGTERM, shutdown_handler)

    host = os.environ.get('K8SLITE_HOST', '0.0.0.0')
    port = int(os.environ.get('K8SLITE_PORT', '5000'))
    debug = os.environ.get('FLASK_DEBUG', 'false').lower() == 'true'

    logger.info(f"Starting Kubernetes Lite server on http://{host}:{port}")
    app.run(host=host, port=port, debug=debug, use_reloader=False)
