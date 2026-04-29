#!/usr/bin/env bash
set -euo pipefail

echo "Starting Postgres..."
docker run -d --name odd-pg --rm \
  -p 5432:5432 \
  -e POSTGRES_PASSWORD=odd \
  -e POSTGRES_USER=oddish \
  -e POSTGRES_DB=oddish \
  postgres:16

echo "Starting MinIO..."
docker run -d --name odd-s3 --rm \
  -p 9000:9000 -p 9001:9001 \
  -e MINIO_ROOT_USER=minio \
  -e MINIO_ROOT_PASSWORD=miniosecret \
  minio/minio server /data --console-address :9001

echo "Waiting for Postgres..."
until docker exec odd-pg pg_isready -U oddish >/dev/null 2>&1; do sleep 1; done

echo "Waiting for MinIO..."
until curl -fs http://localhost:9000/minio/health/live >/dev/null 2>&1; do sleep 1; done

echo "Creating bucket oddish-dev via MinIO console-API..."
docker run --rm --network host \
  -e MC_HOST_local=http://minio:miniosecret@localhost:9000 \
  minio/mc mb local/oddish-dev || true

echo
echo "Postgres:  postgresql+asyncpg://oddish:odd@localhost/oddish"
echo "MinIO:     http://localhost:9001  (user: minio / pass: miniosecret)"
echo "Bucket:    oddish-dev"
