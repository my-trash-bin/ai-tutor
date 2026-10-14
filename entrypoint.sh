#!/bin/sh
set -eu
until docker info >/dev/null 2>&1; do sleep 1; done
docker build -q -t neito-python:local -f /app/worker.Dockerfile /app >/dev/null
exec gunicorn --bind 0.0.0.0:8000 --workers 1 --threads 8 app:app
