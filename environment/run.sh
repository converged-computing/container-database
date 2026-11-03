#!/usr/bin/env bash
# Run a command in the environment image with the host Docker socket and this repository mounted.
set -euo pipefail

root=$(cd "$(dirname "$0")/.." && pwd)
image=${DB_ENV_IMAGE:-container-manifest-db-env}

args=(--rm -i -w /db
      -v /var/run/docker.sock:/var/run/docker.sock
      -v "$root":/db
      -v container-manifest-db-cache:/root/.cache)
[ -t 0 ] && [ -t 1 ] && args+=(-t)

for var in ANTHROPIC_API_KEY SECRETARY_MODEL \
           AWS_ACCESS_KEY_ID AWS_SECRET_ACCESS_KEY AWS_SESSION_TOKEN \
           AWS_REGION AWS_DEFAULT_REGION AWS_PROFILE AWS_BEARER_TOKEN_BEDROCK; do
  if [ -n "${!var:-}" ]; then args+=(-e "$var"); fi
done
if [ -d "$HOME/.aws" ]; then args+=(-v "$HOME/.aws":/root/.aws:ro); fi
if [ -n "${DOCKER_CONFIG_DIR:-}" ]; then args+=(-v "$DOCKER_CONFIG_DIR":/root/.docker:ro); fi

exec docker run "${args[@]}" "$image" "$@"
