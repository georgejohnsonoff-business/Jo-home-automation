#!/usr/bin/env bash
# One-time setup on a new machine. Never touches real secrets -- those are
# never committed to this (public) repo. Run this once, fill in .env when
# prompted, then use run.sh (or the docker command it prints) from then on.
set -e

if [ ! -f .env ]; then
  cp .env.example .env
  echo "Created .env from template -- edit it now with your real Tuya/Groq credentials:"
  echo "  ${EDITOR:-nano} .env"
  exit 0
fi

echo ".env already exists. Starting the container..."
docker run -d --name climate-brain --restart=always --env-file .env -p 8010:8010 \
  ghcr.io/georgejohnsonoff-business/climate-brain:latest

echo "Running at http://localhost:8010"
