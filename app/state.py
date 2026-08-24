"""
SQLite-Backed State Management
Stores desired state for all deployments persistently
"""

import json
import logging
import os
import sqlite3
import threading
from datetime import datetime
from typing import Dict, List, Optional

logger = logging.getLogger(__name__)

DB_PATH = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "kubernetes_lite.db"
)

MAX_EVENTS = 500
DEFAULT_BACKUP_KEEP = 10


class StateManager:
    """Thread-safe SQLite-backed state manager for deployments"""

    def __init__(self, db_path: Optional[str] = None):
        self._lock = threading.RLock()
        self._db_path = db_path or os.environ.get("K8SLITE_DB", DB_PATH)
        parent = os.path.dirname(os.path.abspath(self._db_path))
        if parent:
            os.makedirs(parent, exist_ok=True)
        self._conn = sqlite3.connect(self._db_path, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._init_db()
        logger.info(f"State manager connected to {self._db_path}")

    def _init_db(self) -> None:
        """Create tables if they don't exist"""
        with self._lock:
            self._conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS deployments (
                    name TEXT PRIMARY KEY,
                    image TEXT NOT NULL,
                    desired_replicas INTEGER NOT NULL,
                    container_ids TEXT NOT NULL DEFAULT '[]',
                    created_at TEXT NOT NULL,
                    heal_count INTEGER NOT NULL DEFAULT 0,
                    health_port INTEGER,
                    health_path TEXT,
                    autoscale_enabled INTEGER NOT NULL DEFAULT 0,
                    min_replicas INTEGER,
                    max_replicas INTEGER,
                    target_cpu INTEGER,
                    lb_port INTEGER,
                    lb_target_port INTEGER
                );

                CREATE TABLE IF NOT EXISTS containers (
                    id TEXT PRIMARY KEY,
                    deployment TEXT NOT NULL,
                    image TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    restart_count INTEGER NOT NULL DEFAULT 0
                );

                CREATE TABLE IF NOT EXISTS events (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    timestamp TEXT NOT NULL,
                    type TEXT NOT NULL,
                    message TEXT NOT NULL
                );
                """
            )
            # Lightweight migrations for databases created before a column existed
            existing = {
                r["name"] for r in self._conn.execute("PRAGMA table_info(deployments)")
            }
            migrations = {
                "autoscale_enabled": "INTEGER NOT NULL DEFAULT 0",
                "min_replicas": "INTEGER",
                "max_replicas": "INTEGER",
                "target_cpu": "INTEGER",
                "lb_port": "INTEGER",
                "lb_target_port": "INTEGER",
            }
            for column, ddl in migrations.items():
                if column not in existing:
                    self._conn.execute(
                        f"ALTER TABLE deployments ADD COLUMN {column} {ddl}"
                    )
            self._conn.commit()

    def _deployment_from_row(self, row: sqlite3.Row) -> Dict:
        return {
            "name": row["name"],
            "image": row["image"],
            "desired_replicas": row["desired_replicas"],
            "container_ids": json.loads(row["container_ids"] or "[]"),
            "created_at": row["created_at"],
            "heal_count": row["heal_count"],
            "health_port": row["health_port"],
            "health_path": row["health_path"],
            "autoscale": {
                "enabled": bool(row["autoscale_enabled"]),
                "min_replicas": row["min_replicas"],
                "max_replicas": row["max_replicas"],
                "target_cpu": row["target_cpu"],
            },
            "lb_port": row["lb_port"],
            "lb_target_port": row["lb_target_port"],
        }

    # ---- Deployments ----

    def add_deployment(
        self,
        name: str,
        image: str,
        replicas: int,
        container_ids: List[str],
        health_port: Optional[int] = None,
        health_path: Optional[str] = None,
    ) -> None:
        """Add a new deployment to state"""
        with self._lock:
            self._conn.execute(
                """INSERT INTO deployments
                   (name, image, desired_replicas, container_ids,
                    created_at, heal_count, health_port, health_path)
                   VALUES (?, ?, ?, ?, ?, 0, ?, ?)""",
                (
                    name,
                    image,
                    replicas,
                    json.dumps(container_ids),
                    datetime.now().isoformat(),
                    health_port,
                    health_path,
                ),
            )
            self._conn.commit()
            self.log_event("DEPLOY", f"Deployed {name} with {replicas} replicas")

    def get_deployment(self, name: str) -> Optional[Dict]:
        """Get deployment by name"""
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM deployments WHERE name = ?", (name,)
            ).fetchone()
            return self._deployment_from_row(row) if row else None

    def get_all_deployments(self) -> Dict[str, Dict]:
        """Get all deployments"""
        with self._lock:
            rows = self._conn.execute("SELECT * FROM deployments").fetchall()
            return {r["name"]: self._deployment_from_row(r) for r in rows}

    def update_deployment_replicas(self, name: str, new_replicas: int) -> bool:
        """Update desired replica count for a deployment"""
        with self._lock:
            row = self._conn.execute(
                "SELECT desired_replicas FROM deployments WHERE name = ?", (name,)
            ).fetchone()
            if not row:
                return False
            old = row["desired_replicas"]
            self._conn.execute(
                "UPDATE deployments SET desired_replicas = ? WHERE name = ?",
                (new_replicas, name),
            )
            self._conn.commit()
            self.log_event("SCALE", f"Scaled {name}: {old} → {new_replicas} replicas")
            return True

    def update_deployment_containers(self, name: str, container_ids: List[str]) -> None:
        """Update the list of container IDs for a deployment"""
        with self._lock:
            self._conn.execute(
                "UPDATE deployments SET container_ids = ? WHERE name = ?",
                (json.dumps(container_ids), name),
            )
            self._conn.commit()

    def update_deployment_image(self, name: str, new_image: str) -> None:
        """Update the image for a deployment (used by rolling updates)"""
        with self._lock:
            self._conn.execute(
                "UPDATE deployments SET image = ? WHERE name = ?", (new_image, name)
            )
            self._conn.commit()

    def update_deployment_health(
        self,
        name: str,
        health_port: Optional[int],
        health_path: Optional[str],
    ) -> None:
        """Update health check configuration, keeping existing values when not provided"""
        with self._lock:
            self._conn.execute(
                """UPDATE deployments
                   SET health_port = COALESCE(?, health_port),
                       health_path = COALESCE(?, health_path)
                   WHERE name = ?""",
                (health_port, health_path, name),
            )
            self._conn.commit()

    def update_autoscale(
        self,
        name: str,
        enabled: bool,
        min_replicas: Optional[int] = None,
        max_replicas: Optional[int] = None,
        target_cpu: Optional[int] = None,
    ) -> bool:
        """Enable/disable and configure auto-scaling for a deployment.

        Values that are None keep their previous setting (COALESCE), so
        auto-scaling can be toggled without re-sending the whole config.
        """
        with self._lock:
            row = self._conn.execute(
                "SELECT name FROM deployments WHERE name = ?", (name,)
            ).fetchone()
            if not row:
                return False
            self._conn.execute(
                """UPDATE deployments
                   SET autoscale_enabled = ?,
                       min_replicas = COALESCE(?, min_replicas),
                       max_replicas = COALESCE(?, max_replicas),
                       target_cpu = COALESCE(?, target_cpu)
                   WHERE name = ?""",
                (1 if enabled else 0, min_replicas, max_replicas, target_cpu, name),
            )
            self._conn.commit()
            if enabled:
                self.log_event(
                    "AUTOSCALE",
                    f"Auto-scaling enabled for {name} "
                    f"(min={min_replicas}, max={max_replicas}, target CPU {target_cpu}%)",
                )
            else:
                self.log_event("AUTOSCALE", f"Auto-scaling disabled for {name}")
            return True

    def update_load_balancer(
        self,
        name: str,
        port: Optional[int],
        target_port: Optional[int] = None,
    ) -> bool:
        """Set (port) or clear (None) the load-balancer host port for a deployment"""
        with self._lock:
            row = self._conn.execute(
                "SELECT name FROM deployments WHERE name = ?", (name,)
            ).fetchone()
            if not row:
                return False
            self._conn.execute(
                "UPDATE deployments SET lb_port = ?, lb_target_port = ? WHERE name = ?",
                (port, target_port, name),
            )
            self._conn.commit()
            if port:
                self.log_event(
                    "LB",
                    f"Load balancer enabled for {name} on host port {port} "
                    f"(backend :{target_port})",
                )
            else:
                self.log_event("LB", f"Load balancer disabled for {name}")
            return True

    def increment_heal_count(self, name: str) -> None:
        """Increment self-healing counter for a deployment"""
        with self._lock:
            self._conn.execute(
                "UPDATE deployments SET heal_count = heal_count + 1 WHERE name = ?",
                (name,),
            )
            self._conn.commit()

    def remove_deployment(self, name: str) -> bool:
        """Remove a deployment and all its containers from tracking"""
        with self._lock:
            row = self._conn.execute(
                "SELECT name FROM deployments WHERE name = ?", (name,)
            ).fetchone()
            if not row:
                return False
            self._conn.execute(
                "DELETE FROM containers WHERE deployment = ?", (name,)
            )
            self._conn.execute("DELETE FROM deployments WHERE name = ?", (name,))
            self._conn.commit()
            self.log_event("DELETE", f"Removed deployment {name}")
            return True

    # ---- Containers ----

    def add_container(self, container_id: str, deployment_name: str, image: str) -> None:
        """Track a container"""
        with self._lock:
            self._conn.execute(
                """INSERT OR REPLACE INTO containers
                   (id, deployment, image, created_at, restart_count)
                   VALUES (?, ?, ?, ?, 0)""",
                (container_id, deployment_name, image, datetime.now().isoformat()),
            )
            self._conn.commit()

    def remove_container(self, container_id: str) -> None:
        """Remove a container from tracking"""
        with self._lock:
            self._conn.execute(
                "DELETE FROM containers WHERE id = ?", (container_id,)
            )
            self._conn.commit()

    # ---- Events ----

    def log_event(self, event_type: str, message: str) -> None:
        """Add an event to the log"""
        with self._lock:
            self._conn.execute(
                "INSERT INTO events (timestamp, type, message) VALUES (?, ?, ?)",
                (datetime.now().isoformat(), event_type, message),
            )
            self._conn.execute(
                """DELETE FROM events WHERE id NOT IN
                   (SELECT id FROM events ORDER BY id DESC LIMIT ?)""",
                (MAX_EVENTS,),
            )
            self._conn.commit()

    def get_events(self, limit: int = 50) -> List[Dict]:
        """Get recent events in chronological order"""
        with self._lock:
            rows = self._conn.execute(
                "SELECT timestamp, type, message FROM events ORDER BY id DESC LIMIT ?",
                (limit,),
            ).fetchall()
            return [dict(r) for r in reversed(rows)]

    # ---- Summary ----

    def get_summary(self) -> Dict:
        """Get system summary"""
        with self._lock:
            row = self._conn.execute(
                """
                SELECT
                    (SELECT COUNT(*) FROM deployments) AS deployment_count,
                    COALESCE((SELECT SUM(desired_replicas) FROM deployments), 0) AS total_desired,
                    (SELECT COUNT(*) FROM containers) AS total_containers,
                    (SELECT COUNT(*) FROM events) AS events_count
                """
            ).fetchone()
            return {
                "deployment_count": row["deployment_count"],
                "total_desired_replicas": row["total_desired"],
                "total_tracked_containers": row["total_containers"],
                "events_count": row["events_count"],
            }

    # ---- Maintenance / backups ----

    def _backup_dir(self) -> str:
        """Directory where database snapshots are stored"""
        default_dir = os.path.join(
            os.path.dirname(os.path.abspath(self._db_path)), "backups"
        )
        return os.environ.get("K8SLITE_BACKUP_DIR", default_dir)

    def backup(self, dest_path: Optional[str] = None) -> str:
        """
        Create a consistent snapshot of the SQLite database using the
        sqlite3 backup API. Older backups beyond K8SLITE_BACKUP_KEEP
        (default 10) are pruned automatically.
        """
        with self._lock:
            if not dest_path:
                stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
                backup_dir = self._backup_dir()
                os.makedirs(backup_dir, exist_ok=True)
                dest_path = os.path.join(backup_dir, f"kubernetes-lite-{stamp}.db")

            dest = sqlite3.connect(dest_path)
            try:
                with dest:
                    self._conn.backup(dest)
            finally:
                dest.close()

            keep = int(os.environ.get("K8SLITE_BACKUP_KEEP", DEFAULT_BACKUP_KEEP))
            self._prune_backups(keep)

            logger.info(f"Database backed up to {dest_path}")
            return dest_path

    def _prune_backups(self, keep: int) -> None:
        """Delete the oldest snapshot files, keeping only the newest `keep`"""
        backup_dir = self._backup_dir()
        if not os.path.isdir(backup_dir):
            return
        snapshots = sorted(
            f for f in os.listdir(backup_dir)
            if f.startswith("kubernetes-lite-") and f.endswith(".db")
        )
        for old_file in snapshots[:-keep] if keep > 0 else snapshots:
            try:
                os.remove(os.path.join(backup_dir, old_file))
            except OSError as e:
                logger.warning(f"Could not prune backup {old_file}: {e}")

    def list_backups(self) -> List[Dict]:
        """List available backups (newest first) with size and mtime"""
        backup_dir = self._backup_dir()
        result = []
        if not os.path.isdir(backup_dir):
            return result
        for name in sorted(os.listdir(backup_dir), reverse=True):
            path = os.path.join(backup_dir, name)
            if not os.path.isfile(path):
                continue
            stat = os.stat(path)
            result.append({
                "file": name,
                "size_bytes": stat.st_size,
                "created_at": datetime.fromtimestamp(stat.st_mtime).isoformat(),
            })
        return result

    def close(self) -> None:
        """Close the database connection"""
        with self._lock:
            self._conn.close()


# Singleton instance
state_manager = StateManager()
