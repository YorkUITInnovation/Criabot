#!/bin/bash
set -euo pipefail

TAG="uitadmin/criabot:rf_v0.0.1"
PUSH=false
REBUILD=false

for arg in "$@"; do
  case "$arg" in
    --push)
      PUSH=true
      ;;
    --rebuild)
      REBUILD=true
      ;;
    -h|--help)
      echo "Usage: $0 [--push] [--rebuild]"
      exit 0
      ;;
    *)
      echo "Unknown argument: $arg" >&2
      echo "Usage: $0 [--push] [--rebuild]" >&2
      exit 1
      ;;
  esac
done

NO_CACHE_FLAG=""
if [ "$REBUILD" = true ]; then
  NO_CACHE_FLAG="--no-cache"
fi

if [ "$PUSH" = true ]; then
  docker buildx build \
    --platform linux/amd64,linux/arm64 \
    --tag "$TAG" \
    --push \
    $NO_CACHE_FLAG \
    .
else
  docker buildx build \
    --tag "$TAG" \
    --load \
    $NO_CACHE_FLAG \
    .
fi