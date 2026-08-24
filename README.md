# Kubernetes Lite ☸️

A miniature container orchestrator — a working, scaled-down Kubernetes built
from scratch with Flask and the Docker SDK. It manages real Docker containers
through a declarative desired-state model with a background reconciliation
loop, self-healing, elastic scaling, and zero-downtime rolling updates.

> **Why this is not "just a web project":** the web UI is only a control
> plane. The product itself is *cloud infrastructure software* — an
> orchestration engine that schedules containers, enforces desired state,
> probes health, and heals failures, plus a complete CI/CD pipeline and a
> manual VM hosting + maintenance story (see below).

## Architecture

```
                       ┌────────────────────────────────────────────┐
   Dashboard ──HTTP──► │  Flask REST API (control plane)            │
   (browser)           │    /api/deploy /api/scale /api/update ...  │
                       └───────────────┬────────────────────────────┘
                                       │
                                       ▼
                       ┌────────────────────────────────────────────┐
                       │  Orchestrator engine                       │
                       │  - deploy / scale / rolling update         │
                       │  - reconcile loop (every 5s):              │
                       │      desired state vs actual state         │
                       │      health probes → restart on failure    │
                       └───────┬────────────────────┬───────────────┘
                               │                    │
                               ▼                    ▼
                     ┌───────────────┐    ┌──────────────────┐
                     │ Docker SDK    │    │ SQLite state     │
                     │ (real engine) │    │ deployments,     │
                     │ containers +  │    │ containers,      │
                     │ bridge network│    │ events, backups  │
                     └───────────────┘    └──────────────────┘
```

**Reconciliation loop (the heart of Kubernetes):**
every 5 seconds the orchestrator compares *desired* replicas against
*actual* running containers. Missing containers are recreated (self-healing),
unhealthy ones are restarted after repeated failed HTTP probes, and crashed
containers are replaced — all without human intervention.

**Auto-scaler (HPA-style elasticity):**
a second background loop samples CPU usage of every replica (Docker stats
API) every 10 seconds. For auto-scaled deployments it computes
`desired = ceil(current × avgCPU / targetCPU)`, clamps to the configured
min/max bounds, and scales through the same path as manual scaling — with an
HPA-like 10% tolerance band and a 30s cooldown so it never flaps. Configure
per deployment via `/api/autoscale`; live average CPU shows on the dashboard.

**Load balancer (traffic distribution):**
each deployment can get its own nginx container on the managed bridge
network, publishing a unique host port (8000–8099). Its `upstream` pool is
generated from the *live* replica IPs; when replicas are added/removed/healed,
the reconcile loop rewrites the config inside the running nginx and reloads
it — no dropped connections. The LB itself is self-healed like any replica.
Enable via `/api/lb` or the 🌐 button; `target_port` defaults to the
deployment's health port (or 80).

## Cloud concepts covered

| Course topic | Where it lives in this project |
|---|---|
| **Virtualization / containerization** | Every managed workload runs as a Docker container (`app/docker_client.py`); the platform itself ships as a Docker image |
| **Orchestration & cloud infrastructure** | The whole `app/orchestrator.py` — deploy, schedule, replace, delete |
| **Elasticity / scalability** | Manual scaling 0–10 replicas plus an HPA-style **auto-scaler**: samples per-container CPU, computes desired replicas proportionally to a target, respects min/max bounds with cooldown + tolerance (`autoscale_once`) |
| **Fault tolerance & self-healing** | Reconciliation loop + HTTP health checks with restart thresholds |
| **Load balancing** | Per-deployment nginx reverse-proxy container: upstream pool generated from live replica IPs, rewritten (zero-downtime reload) whenever the reconcile loop changes membership, one published host port per service (`enable_load_balancer` / `_sync_load_balancer`) |
| **Zero-downtime deployment** | Rolling updates: new replica created before old one removed |
| **CI/CD** | GitHub Actions pipeline: lint → test → build → smoke test → push registry → deploy to VM (`.github/workflows/ci-cd.yml`) |
| **Manual hosting / maintenance** | Ubuntu VM bootstrap script, prod compose file, DB backup API with retention, log rotation, healthchecks (`deploy/`) |

No managed cloud services are used anywhere — you host it yourself on a VM.

## Quickstart

### Local (needs a running Docker daemon)

```bash
pip install -r requirements.txt
python -m app.main          # http://localhost:5000
```

### With Docker Compose

```bash
docker compose up -d --build
open http://localhost:5000
```

### Try it (30-second demo)

```bash
# Deploy nginx with 3 replicas and HTTP health checking
curl -X POST http://localhost:5000/api/deploy \
  -H "Content-Type: application/json" \
  -d '{"name":"web","image":"nginx:alpine","replicas":3,"health_port":80}'

# Self-healing demo: kill a replica behind its back...
docker rm -f web-replica-1
# ...watch the reconcile loop recreate it within ~5s:
curl -s http://localhost:5000/api/status | python -m json.tool

# Elastic scaling demo
curl -X POST http://localhost:5000/api/scale \
  -H "Content-Type: application/json" -d '{"name":"web","replicas":8}'

# Auto-scaling demo: keep average CPU around 60%, between 2 and 6 replicas.
# Burn CPU in the containers and watch the scaler react within ~10s:
curl -X POST http://localhost:5000/api/autoscale \
  -H "Content-Type: application/json" \
  -d '{"name":"web","min_replicas":2,"max_replicas":6,"target_cpu":60}'

# Load balancing demo: nginx fronts the replicas on host port 8000.
# Scale up/down and watch the upstream pool follow automatically:
curl -X POST http://localhost:5000/api/lb \
  -H "Content-Type: application/json" \
  -d '{"name":"web","target_port":80}'
curl http://localhost:8000/

# Zero-downtime rolling update
curl -X POST http://localhost:5000/api/update \
  -H "Content-Type: application/json" -d '{"name":"web","image":"httpd:alpine"}'
```

The dashboard shows live replica states, self-heal counters, and an event log.

### Guided demo (what to show in a presentation)

```powershell
powershell -ExecutionPolicy Bypass -File scripts\demo.ps1
```

Walks through the whole story with pauses for commentary: deploy →
load-balanced round-robin → kill-a-replica chaos → self-heal → CPU load →
live auto-scaling up and back down → teardown, printing the event audit trail.

## API reference

| Method | Endpoint | Description |
|---|---|---|
| GET | `/api/status` | All deployments, containers, summary, recent events |
| POST | `/api/deploy` | `{name, image, replicas(1-10), health_port?, health_path?, min_replicas?, max_replicas?, target_cpu?}` |
| POST | `/api/scale` | `{name, replicas}` — scale up/down (elasticity) |
| POST | `/api/autoscale` | `{name, enabled?, min_replicas, max_replicas, target_cpu}` — HPA-style CPU auto-scaling |
| POST | `/api/lb` | `{name, enabled?, target_port?}` — nginx load balancer in front of the replicas |
| POST | `/api/update` | `{name, image}` — rolling update to a new image |
| DELETE | `/api/delete/<name>` | Remove deployment and all containers |
| GET | `/api/containers` | Every container managed by kubernetes-lite |
| GET | `/api/events?limit=50` | Orchestration event log |
| GET | `/api/health` | Liveness + Docker connectivity probe |
| POST | `/api/maintenance/backup` | Consistent DB snapshot (auto-pruned) |
| GET | `/api/maintenance/backups` | List available snapshots |

## CI/CD pipeline

`.github/workflows/ci-cd.yml` implements a four-stage pipeline:

1. **Lint & Test** — flake8 + pytest on every push/PR
2. **Build & Smoke Test** — builds the image and boots it against a real
   Docker daemon, failing unless `/api/health` reports healthy
3. **Push** — publishes to Docker Hub as `latest` + git-SHA tag (main only)
4. **Deploy** — SSHes into the VM, pulls the exact SHA-tagged image and
   restarts the stack via `deploy/deploy.sh` (main only)

### One-time setup

- Push the repo to GitHub (the Actions workflow activates automatically).
- Add repository secrets: `DOCKERHUB_USERNAME`, `DOCKERHUB_TOKEN`.
- After hosting (below), add: `DEPLOY_HOST`, `DEPLOY_USER`, `DEPLOY_SSH_KEY`,
  `DEPLOY_PATH`, then create a GitHub **environment** named `production`
  (Settings → Environments) with *Required reviewers* to get a manual
  approval gate before every deploy.

## Manual hosting guide (Ubuntu VM)

No PaaS, no managed hosting — a plain VM you administer yourself.

```bash
# 1. One-time server bootstrap (installs Docker, firewall rule)
git clone <your-repo-url> && cd kubernetes-lite
bash deploy/bootstrap-server.sh
# log out / back in so the docker group applies

# 2. Build & run right on the server (no registry needed)
docker compose up -d --build

# --- OR, registry-based flow (what CI/CD uses) ---
K8SLITE_IMAGE=<dockerhubuser>/kubernetes-lite:latest bash deploy/deploy.sh
```

The container uses Docker's `restart: unless-stopped` policy and Docker starts
at boot, so the service survives reboots. Verify: `curl localhost:5000/api/health`.

For production traffic, put nginx/caddy in front for TLS — both are plain
manual configs, which is exactly the point of the exercise.

## Maintenance

- **Database backups** — `POST /api/maintenance/backup` writes a consistent
  SQLite snapshot (via the sqlite3 backup API) to `/app/data/backups` and
  prunes old ones beyond `K8SLITE_BACKUP_KEEP`. The dashboard has a 💾 button;
  for scheduled backups add a cron entry:
  `*/30 * * * * curl -X POST http://localhost:5000/api/maintenance/backup`
- **Container log rotation** — compose files cap logs at 10 MB × 3 rotations.
- **Health monitoring** — Docker/compose healthchecks hit `/api/health`
  every 30s; the dashboard polls live status every 5s.
- **Updates** — pull the new image and `docker compose -f
  deploy/docker-compose.prod.yml up -d`; `deploy.sh` automates this and
  prunes dangling images.
- **Event audit trail** — deploys, scales, heals and updates are all logged
  to the `events` table and visible in the dashboard.

## Testing

```bash
pip install -r requirements.txt -r requirements-dev.txt
pytest -v       # 45 tests: deploy/scale/update/heal/rollback-races/API/backup/auto-scaling/load-balancing
flake8 app tests
```

Unit tests use an in-memory fake of the Docker client, so no daemon is
required; the real-Docker path is exercised by the CI smoke test.

## Project structure

```
├── app/
│   ├── main.py           # Flask app factory + REST API + maintenance endpoints
│   ├── orchestrator.py   # deploy/scale/rolling-update/reconcile/self-heal engine
│   ├── docker_client.py  # Docker SDK wrapper (containers, networks, labels)
│   └── state.py          # SQLite state store + backups with retention
├── templates/static/     # Web dashboard (live status, event log)
├── tests/                # pytest suite + fake Docker client (conftest)
├── deploy/
│   ├── bootstrap-server.sh        # prepare a bare Ubuntu VM
│   ├── deploy.sh                  # pull image + rolling host-side update
│   └── docker-compose.prod.yml    # registry-image based production stack
├── .github/workflows/ci-cd.yml    # lint→test→build→smoke→push→deploy
├── scripts/demo.ps1                 # guided live demo (deploy→LB→chaos→autoscale)
├── Dockerfile / docker-compose.yml
└── Makefile                        # make test / up / down / backup ...
```
