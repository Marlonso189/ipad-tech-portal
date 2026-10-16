#!/bin/sh
# Push a multi-arch image to GHCR from any machine that has Docker Buildx.
# This script does not contain a token. Export one before you run it:
#
#   export GHCR_USERNAME=Marlonso189
#   export GHCR_TOKEN=...   # classic PAT with write:packages and read:packages
#   ./scripts/publish-image.sh
#
# Tags:
#   :<full git sha> always
#   :latest when the current branch is main
#   :X.Y.Z when HEAD is exactly the tag vX.Y.Z

set -eu

if [ -z "${GHCR_TOKEN:-}" ]; then
  echo "Set GHCR_TOKEN to a GitHub personal access token with write:packages." >&2
  exit 1
fi
if [ -z "${GHCR_USERNAME:-}" ]; then
  echo "Set GHCR_USERNAME to the GitHub account that owns the package (Marlonso189)." >&2
  exit 1
fi

root=$(CDPATH= cd -- "$(dirname "$0")/.." && pwd)
cd "$root"

image=ghcr.io/marlonso189/ipad-tech-portal
sha=$(git rev-parse HEAD)
branch=$(git rev-parse --abbrev-ref HEAD)
exact=$(git describe --exact-match --tags HEAD 2>/dev/null || true)

set -- -t "${image}:${sha}"
if [ "$branch" = "main" ]; then
  set -- "$@" -t "${image}:latest"
fi
case "$exact" in
  v[0-9]*.[0-9]*.[0-9]*)
    version=${exact#v}
    case "$version" in
      *[!0-9.]*) ;;
      *.*.*) set -- "$@" -t "${image}:${version}" ;;
    esac
    ;;
esac

echo "$GHCR_TOKEN" | docker login ghcr.io --username "$GHCR_USERNAME" --password-stdin

if ! docker buildx inspect itp-builder >/dev/null 2>&1; then
  docker buildx create --name itp-builder --driver docker-container --use
else
  docker buildx use itp-builder
fi
docker buildx inspect --bootstrap >/dev/null

docker buildx build \
  --platform linux/amd64,linux/arm64 \
  --file Dockerfile \
  --push \
  --label org.opencontainers.image.source=https://github.com/Marlonso189/ipad-tech-portal \
  "$@" \
  .

echo "Pushed ghcr.io/marlonso189/ipad-tech-portal from ${sha}"
