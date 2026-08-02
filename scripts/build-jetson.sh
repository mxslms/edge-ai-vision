#!/usr/bin/env bash
# Optional on-device rebuild of the Jetson Orin image.
#
# CI already builds and publishes :jetson from main on ubuntu-24.04-arm
# (see .github/workflows/ai-pipeline.yml). Prefer:
#   docker pull ghcr.io/mxslms/edge-ai-vision:jetson
#
# Use this script for local iteration on the Orin, or to push a custom build.
#
# Usage on the Jetson:
#   ./scripts/build-jetson.sh              # local tag only
#   ./scripts/build-jetson.sh --push       # also push :jetson to GHCR
set -euo pipefail

IMAGE="${IMAGE:-ghcr.io/mxslms/edge-ai-vision:jetson}"
PUSH=0

for arg in "$@"; do
  case "$arg" in
    --push) PUSH=1 ;;
    -h|--help)
      sed -n '2,12p' "$0"
      exit 0
      ;;
    *)
      echo "Unknown argument: $arg" >&2
      exit 1
      ;;
  esac
done

arch="$(uname -m)"
if [[ "$arch" != "aarch64" ]]; then
  echo "Refusing to build: expected aarch64 Jetson host, got ${arch}." >&2
  echo "Build this image on the Orin Nano, not on the x86 server." >&2
  exit 1
fi

echo "Building ${IMAGE} from Dockerfile.jetson ..."
docker build -f Dockerfile.jetson -t "${IMAGE}" .

if [[ "$PUSH" -eq 1 ]]; then
  echo "Pushing ${IMAGE} ..."
  docker push "${IMAGE}"
fi

echo "Done. Start with:"
echo "  docker compose -f docker-compose.jetson.yml up -d"
