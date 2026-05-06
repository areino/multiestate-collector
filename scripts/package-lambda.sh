#!/usr/bin/env bash
# Build build/function.zip using the official Lambda Python image (Linux wheels for pydantic_core).
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
RUNTIME="${RUNTIME:-3.12}"
ARCH="${ARCH:-x86_64}"

if [[ "$ARCH" == "arm64" ]]; then
  IMAGE="public.ecr.aws/lambda/python:${RUNTIME}-arm64"
else
  IMAGE="public.ecr.aws/lambda/python:${RUNTIME}"
fi

cd "$ROOT"
mkdir -p build

if [[ -n "${CONFIG_PATH:-}" ]]; then
  cp "$CONFIG_PATH" build/_lambda_config.json
  echo "Staged config for zip: $CONFIG_PATH"
fi

echo "Image: $IMAGE"
echo "Repo:  $ROOT"

docker run --rm \
  --entrypoint /bin/bash \
  -v "$ROOT:/workspace" \
  -w /workspace \
  "$IMAGE" \
  /workspace/scripts/docker-pack-inner.sh

echo "Lambda: Runtime Python $RUNTIME, Architecture $ARCH, Handler multiestate_collector.lambda_handler"
