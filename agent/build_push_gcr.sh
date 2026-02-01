#!/usr/bin/env bash
set -euo pipefail
SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd -- "${SCRIPT_DIR}/.." && pwd)"
PROJECT_ID="buoyant-episode-386713"
REGION="us-central1"
AR_REPO="sre-agent"
IMAGE_NAME="sre-agent"
TAG="v0.1.2"

CREATE_REPO_IF_MISSING=0
DOCKERFILE_PATH="agent/Dockerfile"

# Build platforms:
# - If you're on an Apple Silicon Mac, your default build is usually linux/arm64.
# - GKE nodes are typically linux/amd64.
# Set this to "linux/amd64" (recommended for GKE) or "linux/amd64,linux/arm64" for multi-arch.
PLATFORMS="linux/amd64"

if [[ "${PROJECT_ID}" == "YOUR_GCP_PROJECT_ID" ]]; then
  echo "ERROR: edit PROJECT_ID at the top of this script"
  exit 1
fi

REGISTRY_HOST="${REGION}-docker.pkg.dev"
IMAGE_REF="${REGISTRY_HOST}/${PROJECT_ID}/${AR_REPO}/${IMAGE_NAME}:${TAG}"

echo "Target Artifact Registry image: ${IMAGE_REF}"

if [[ "${CREATE_REPO_IF_MISSING}" == "1" ]]; then
  echo "Ensuring Artifact Registry repo exists: ${AR_REPO} (${REGION})"
  gcloud artifacts repositories describe "${AR_REPO}" \
    --location "${REGION}" \
    --project "${PROJECT_ID}" >/dev/null 2>&1 \
  || gcloud artifacts repositories create "${AR_REPO}" \
    --repository-format docker \
    --location "${REGION}" \
    --project "${PROJECT_ID}"
fi

gcloud auth configure-docker "${REGISTRY_HOST}" --quiet

cd "${REPO_ROOT}"

echo "Ensuring docker buildx builder exists..."
docker buildx inspect >/dev/null 2>&1 || docker buildx create --use >/dev/null

echo "Building + pushing image with buildx (platforms: ${PLATFORMS})..."
# Use --no-cache to ensure fresh build, or remove if you want cache
docker buildx build \
  --platform "${PLATFORMS}" \
  --no-cache \
  -t "${IMAGE_REF}" \
  -f "${DOCKERFILE_PATH}" \
  --push \
  .

echo "Done."
echo "IMAGE_REF=${IMAGE_REF}"

