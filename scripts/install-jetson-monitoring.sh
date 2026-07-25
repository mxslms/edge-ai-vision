#!/usr/bin/env bash
# Deploy monitoring agents on a Jetson so the homelab Prometheus stack can
# scrape metrics and Loki can ingest container logs.
#
# Run on the Jetson after install-jetson.sh (or alongside it with --with-monitoring).
#
# Usage:
#   export HOMELAB_IP=192.168.1.10          # machine running homelab monitoring
#   export DEVICE_NAME=jetson-orin-nano     # label used in logs / Prometheus
#   ./scripts/install-jetson-monitoring.sh
#
# Flags:
#   --dir DIR            install directory (default: /opt/edge-ai-vision)
#   --skip-gpu-exporter  skip jtop + jetson-orin-exporter systemd setup
#   --no-systemd         only start docker compose agents
set -euo pipefail

INSTALL_DIR="/opt/edge-ai-vision"
INSTALL_GPU_EXPORTER=1
INSTALL_SYSTEMD=1
COMPOSE_FILE="docker-compose.jetson-monitoring.yml"
JETSON_EXPORTER_PORT="${JETSON_EXPORTER_PORT:-9101}"

log()  { printf '==> %s\n' "$*"; }
warn() { printf 'warning: %s\n' "$*" >&2; }
die()  { printf 'error: %s\n' "$*" >&2; exit 1; }

need_cmd() {
  command -v "$1" >/dev/null 2>&1 || die "missing required command: $1"
}

for arg in "$@"; do
  case "$arg" in
    --dir=*) INSTALL_DIR="${arg#--dir=}" ;;
    --skip-gpu-exporter) INSTALL_GPU_EXPORTER=0 ;;
    --no-systemd) INSTALL_SYSTEMD=0 ;;
    -h|--help)
      sed -n '2,18p' "$0"
      exit 0
      ;;
    *)
      die "unknown argument: $arg"
      ;;
  esac
done

[[ "$(uname -m)" == "aarch64" ]] || die "expected aarch64 Jetson host, got $(uname -m)"

need_cmd docker
need_cmd sudo

if ! docker compose version >/dev/null 2>&1; then
  die "docker compose plugin not found"
fi

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
[[ -f "${REPO_ROOT}/${COMPOSE_FILE}" ]] || die "compose file not found: ${REPO_ROOT}/${COMPOSE_FILE}"

HOMELAB_IP="${HOMELAB_IP:-}"
DEVICE_NAME="${DEVICE_NAME:-jetson-orin-nano}"

if [[ -z "${HOMELAB_IP}" ]]; then
  cat >&2 <<'EOF'
error: HOMELAB_IP is required (LAN address of the homelab monitoring server).

  export HOMELAB_IP=192.168.1.10
  ./scripts/install-jetson-monitoring.sh
EOF
  exit 1
fi

LOKI_URL="${LOKI_URL:-http://${HOMELAB_IP}:3100/loki/api/v1/push}"

log "Installing monitoring files into ${INSTALL_DIR}"
sudo mkdir -p "${INSTALL_DIR}"
sudo cp "${REPO_ROOT}/${COMPOSE_FILE}" "${INSTALL_DIR}/${COMPOSE_FILE}"
sudo cp "${REPO_ROOT}/deploy/jetson/promtail-config.yml" "${INSTALL_DIR}/promtail-config.yml"

if [[ ! -f "${INSTALL_DIR}/monitoring.env" ]]; then
  sudo cp "${REPO_ROOT}/deploy/jetson/monitoring.env.example" "${INSTALL_DIR}/monitoring.env"
fi

# Keep monitoring.env in sync with the values passed on the command line.
sudo sed -i \
  -e "s|^HOMELAB_IP=.*|HOMELAB_IP=${HOMELAB_IP}|" \
  -e "s|^LOKI_URL=.*|LOKI_URL=${LOKI_URL}|" \
  -e "s|^DEVICE_NAME=.*|DEVICE_NAME=${DEVICE_NAME}|" \
  -e "s|^JETSON_EXPORTER_PORT=.*|JETSON_EXPORTER_PORT=${JETSON_EXPORTER_PORT}|" \
  "${INSTALL_DIR}/monitoring.env"

if [[ -n "${SUDO_USER:-}" ]]; then
  sudo chown "${SUDO_USER}:${SUDO_USER}" "${INSTALL_DIR}/monitoring.env" || true
fi

log "Ensuring Docker network monitoring-net exists"
docker network create monitoring-net 2>/dev/null || true

cd "${INSTALL_DIR}"
set -a
# shellcheck disable=SC1091
source monitoring.env
set +a

log "Starting monitoring agents (node-exporter, cAdvisor, promtail)"
docker compose --env-file monitoring.env -f "${COMPOSE_FILE}" up -d --remove-orphans

install_gpu_exporter() {
  log "Installing jetson-stats (jtop) for GPU / power metrics"
  if ! command -v jtop >/dev/null 2>&1; then
    sudo pip3 install -U jetson-stats prometheus_client
  fi

  log "Installing jetson-orin-exporter on port ${JETSON_EXPORTER_PORT}"
  [[ -f "${REPO_ROOT}/deploy/jetson/jetson_exporter.py" ]] \
    || die "missing deploy/jetson/jetson_exporter.py in repo checkout"
  sudo mkdir -p /opt/jetson_exporter
  sudo cp "${REPO_ROOT}/deploy/jetson/jetson_exporter.py" /opt/jetson_exporter/jetson_exporter.py
  sudo sed -i "s/^PORT = 9101/PORT = ${JETSON_EXPORTER_PORT}/" \
    /opt/jetson_exporter/jetson_exporter.py

  if ! id jetson_exporter >/dev/null 2>&1; then
    sudo useradd -r -s /bin/false -M jetson_exporter
  fi
  sudo usermod -aG jtop jetson_exporter 2>/dev/null || true
  sudo chown -R jetson_exporter:jetson_exporter /opt/jetson_exporter

  sudo cp "${REPO_ROOT}/deploy/jetson/jetson-exporter.service" \
    /etc/systemd/system/jetson-exporter.service

  sudo systemctl daemon-reload
  sudo systemctl enable --now jetson-exporter.service
  systemctl --no-pager --full status jetson-exporter.service || true
}

if [[ "${INSTALL_GPU_EXPORTER}" -eq 1 && "${INSTALL_SYSTEMD}" -eq 1 ]]; then
  install_gpu_exporter
elif [[ "${INSTALL_GPU_EXPORTER}" -eq 1 ]]; then
  warn "GPU exporter requires systemd; re-run without --no-systemd or install manually"
fi

JETSON_IP="$(hostname -I 2>/dev/null | awk '{print $1}')"
HOST_PORT="$(grep -E '^HOST_PORT=' "${INSTALL_DIR}/.env" 2>/dev/null | cut -d= -f2- || true)"
HOST_PORT="${HOST_PORT:-5000}"

cat <<EOF

Jetson monitoring agents are running.

  Hardware:      node-exporter :${NODE_EXPORTER_PORT:-9100}, jetson-gpu-exporter :${JETSON_EXPORTER_PORT}
  AI inference:  fish-detection-app :${HOST_PORT}/metrics  (edge_* — from the vision app)
  Containers:    cAdvisor :${CADVISOR_PORT:-8080}
  Logs → Loki:   ${LOKI_URL}

On the homelab server (does not change existing server AI / DCGM jobs):

  ./scripts/register-jetson-target.sh ${JETSON_IP:-<jetson-ip>} ${DEVICE_NAME}

Local checks:
  curl -sf http://localhost:${NODE_EXPORTER_PORT:-9100}/metrics | head
  curl -sf http://localhost:${JETSON_EXPORTER_PORT}/metrics | head
  curl -sf http://localhost:${HOST_PORT}/metrics | head
EOF
