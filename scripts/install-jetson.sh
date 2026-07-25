#!/usr/bin/env bash
# Install Edge AI Vision on a Jetson Orin Nano (JetPack 6.x).
#
# Recommended deploy path:
#   Docker Compose pull from GHCR + systemd unit for boot persistence.
#   Not Portainer-on-Jetson (extra overhead for one edge box).
#   Not a bare-metal pip install (JetPack/CUDA drift is painful).
#
# Prerequisites (you do once by hand):
#   1. Flash JetPack 6.x
#   2. USB camera present as /dev/video0 (or edit CAMERA_INDEX later)
#   3. Network access to ghcr.io (repo is private → need a GHCR token)
#
# Usage (on the Jetson, from a clone of this repo):
#   export GHCR_USER=your-github-username
#   export GHCR_TOKEN=ghp_xxxxxxxx          # read:packages (+ repo if needed)
#   ./scripts/install-jetson.sh
#
# Flags:
#   --build-fallback   if :jetson pull fails, build locally with Dockerfile.jetson
#   --no-systemd       start with compose only; do not install the unit
#   --skip-login       assume docker is already logged into ghcr.io
#   --with-monitoring  also deploy node-exporter, cAdvisor, promtail, GPU exporter
#   --dir DIR          install directory (default: /opt/edge-ai-vision)
set -euo pipefail

IMAGE_DEFAULT="ghcr.io/mxslms/edge-ai-vision:jetson"
INSTALL_DIR="/opt/edge-ai-vision"
BUILD_FALLBACK=0
INSTALL_SYSTEMD=1
DO_LOGIN=1
WITH_MONITORING=0
COMPOSE_FILE="docker-compose.jetson.yml"

log()  { printf '==> %s\n' "$*"; }
warn() { printf 'warning: %s\n' "$*" >&2; }
die()  { printf 'error: %s\n' "$*" >&2; exit 1; }

need_cmd() {
  command -v "$1" >/dev/null 2>&1 || die "missing required command: $1"
}

for arg in "$@"; do
  case "$arg" in
    --build-fallback) BUILD_FALLBACK=1 ;;
    --no-systemd) INSTALL_SYSTEMD=0 ;;
    --skip-login) DO_LOGIN=0 ;;
    --with-monitoring) WITH_MONITORING=1 ;;
    --dir=*) INSTALL_DIR="${arg#--dir=}" ;;
    --dir)
      die "--dir requires a value (use --dir=/path)"
      ;;
    -h|--help)
      sed -n '2,28p' "$0"
      exit 0
      ;;
    *)
      die "unknown argument: $arg"
      ;;
  esac
done

# Allow --dir /path form by re-parsing if someone passed it positionally-ish
# (handled above via --dir= only for simplicity)

[[ "$(uname -m)" == "aarch64" ]] || die "expected aarch64 Jetson host, got $(uname -m)"

need_cmd docker
need_cmd sudo

if ! docker compose version >/dev/null 2>&1; then
  die "docker compose plugin not found. Install Docker Compose v2 on the Jetson first."
fi

# NVIDIA runtime is required for GPU inside containers on Jetson.
if ! docker info 2>/dev/null | grep -qi 'Runtimes:.*nvidia'; then
  cat >&2 <<'EOF'
error: Docker is missing the nvidia runtime.

On JetPack 6, install/configure nvidia-container-toolkit, then:
  sudo nvidia-ctk runtime configure --runtime=docker
  sudo systemctl restart docker

Verify with:
  docker info | grep -i runtime
  docker run --rm --runtime=nvidia ultralytics/ultralytics:latest-jetson-jetpack6 \
    python -c "import torch; print(torch.cuda.is_available())"
EOF
  exit 1
fi

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
[[ -f "${REPO_ROOT}/${COMPOSE_FILE}" ]] || die "compose file not found: ${REPO_ROOT}/${COMPOSE_FILE}"
[[ -f "${REPO_ROOT}/deploy/jetson/edge-ai-vision.service" ]] || die "systemd unit missing under deploy/jetson/"

if [[ "${DO_LOGIN}" -eq 1 ]]; then
  if [[ -z "${GHCR_USER:-}" || -z "${GHCR_TOKEN:-}" ]]; then
    cat >&2 <<'EOF'
error: GHCR credentials required (this repository is private).

Create a GitHub PAT with at least read:packages, then:

  export GHCR_USER=your-github-username
  export GHCR_TOKEN=ghp_xxxxxxxx
  ./scripts/install-jetson.sh

Or log in yourself and re-run with --skip-login:
  echo "$GHCR_TOKEN" | docker login ghcr.io -u "$GHCR_USER" --password-stdin
EOF
    exit 1
  fi
  log "Logging into ghcr.io as ${GHCR_USER}"
  echo "${GHCR_TOKEN}" | docker login ghcr.io -u "${GHCR_USER}" --password-stdin
fi

log "Installing deploy files into ${INSTALL_DIR}"
sudo mkdir -p "${INSTALL_DIR}"
sudo cp "${REPO_ROOT}/${COMPOSE_FILE}" "${INSTALL_DIR}/${COMPOSE_FILE}"
# Dockerfile.jetson is only needed for --build-fallback / local rebuilds
if [[ -f "${REPO_ROOT}/Dockerfile.jetson" ]]; then
  sudo cp "${REPO_ROOT}/Dockerfile.jetson" "${INSTALL_DIR}/Dockerfile.jetson"
fi
if [[ -f "${REPO_ROOT}/app.py" ]]; then
  sudo cp "${REPO_ROOT}/app.py" "${INSTALL_DIR}/app.py"
fi
if [[ -f "${REPO_ROOT}/requirements.txt" ]]; then
  sudo cp "${REPO_ROOT}/requirements.txt" "${INSTALL_DIR}/requirements.txt"
fi

if [[ ! -f "${INSTALL_DIR}/.env" ]]; then
  sudo cp "${REPO_ROOT}/deploy/jetson/env.example" "${INSTALL_DIR}/.env"
  log "Wrote ${INSTALL_DIR}/.env from env.example (edit tunables there)"
else
  log "Keeping existing ${INSTALL_DIR}/.env"
fi

# Root owns /opt copy; make .env editable by the invoking user when possible.
if [[ -n "${SUDO_USER:-}" ]]; then
  sudo chown "${SUDO_USER}:${SUDO_USER}" "${INSTALL_DIR}/.env" || true
fi

log "Ensuring Docker network monitoring-net exists"
docker network create monitoring-net 2>/dev/null || true

cd "${INSTALL_DIR}"

IMAGE="$(grep -E '^IMAGE=' .env 2>/dev/null | cut -d= -f2- || true)"
IMAGE="${IMAGE:-$IMAGE_DEFAULT}"

log "Pulling ${IMAGE}"
if docker compose -f "${COMPOSE_FILE}" pull; then
  log "Pull succeeded"
elif [[ "${BUILD_FALLBACK}" -eq 1 ]]; then
  warn "Pull failed; building locally with Dockerfile.jetson (slow on Orin Nano)"
  docker compose -f "${COMPOSE_FILE}" build
else
  die "Failed to pull ${IMAGE}. Merge/publish the Jetson CI image, check GHCR auth, or re-run with --build-fallback."
fi

if [[ "${INSTALL_SYSTEMD}" -eq 1 ]]; then
  log "Installing systemd unit edge-ai-vision.service"
  sudo cp "${REPO_ROOT}/deploy/jetson/edge-ai-vision.service" \
    /etc/systemd/system/edge-ai-vision.service
  sudo systemctl daemon-reload
  sudo systemctl enable --now edge-ai-vision.service
  log "systemd status:"
  systemctl --no-pager --full status edge-ai-vision.service || true
else
  log "Starting with docker compose (no systemd)"
  docker compose -f "${COMPOSE_FILE}" up -d --remove-orphans
fi

HOST_PORT="$(grep -E '^HOST_PORT=' .env 2>/dev/null | cut -d= -f2- || true)"
HOST_PORT="${HOST_PORT:-5000}"

cat <<EOF

Install complete.

  health:  curl -sf http://localhost:${HOST_PORT}/healthz
  stream:  http://<jetson-ip>:${HOST_PORT}/video_feed
  metrics: http://<jetson-ip>:${HOST_PORT}/metrics

  config:  ${INSTALL_DIR}/.env
  compose: ${INSTALL_DIR}/${COMPOSE_FILE}
  logs:    docker logs -f fish-detection-app
  service: sudo systemctl status edge-ai-vision

GPU telemetry on Jetson: use jtop/tegrastats on the host (not DCGM).
EOF

if [[ "${WITH_MONITORING}" -eq 1 ]]; then
  log "Deploying monitoring agents (--with-monitoring)"
  if [[ -z "${HOMELAB_IP:-}" ]]; then
    die "HOMELAB_IP is required with --with-monitoring (LAN IP of homelab monitoring server)"
  fi
  HOMELAB_IP="${HOMELAB_IP}" DEVICE_NAME="${DEVICE_NAME:-jetson-orin-nano}" \
    "${REPO_ROOT}/scripts/install-jetson-monitoring.sh" --dir="${INSTALL_DIR}"
fi
