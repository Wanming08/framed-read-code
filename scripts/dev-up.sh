#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT_DIR"

for command in docker curl awk od tr; do
  command -v "$command" >/dev/null 2>&1 || {
    echo "Missing required command: $command" >&2
    exit 1
  }
done
docker compose version >/dev/null
docker info >/dev/null

random_secret() {
  od -An -N24 -tx1 /dev/urandom | tr -d ' \n'
}

if [[ ! -f .env ]]; then
  umask 077
  db_password="$(random_secret)"
  mysql_root_password="$(random_secret)"
  redis_password="$(random_secret)"
  minio_secret_key="$(random_secret)"
  qdrant_api_key="$(random_secret)"
  awk \
    -v db_password="$db_password" \
    -v mysql_root_password="$mysql_root_password" \
    -v redis_password="$redis_password" \
    -v minio_secret_key="$minio_secret_key" \
    -v qdrant_api_key="$qdrant_api_key" '
      /^DB_PASSWORD=/ {print "DB_PASSWORD=" db_password; next}
      /^MYSQL_ROOT_PASSWORD=/ {print "MYSQL_ROOT_PASSWORD=" mysql_root_password; next}
      /^REDIS_PASSWORD=/ {print "REDIS_PASSWORD=" redis_password; next}
      /^MINIO_SECRET_KEY=/ {print "MINIO_SECRET_KEY=" minio_secret_key; next}
      /^QDRANT_API_KEY=/ {print "QDRANT_API_KEY=" qdrant_api_key; next}
      {print}
    ' .env.example > .env
  echo "Created private .env with generated infrastructure credentials."
fi

set -a
# shellcheck disable=SC1091
source .env
set +a

for variable in DB_PASSWORD MYSQL_ROOT_PASSWORD REDIS_PASSWORD MINIO_SECRET_KEY QDRANT_API_KEY; do
  value="${!variable:-}"
  if [[ -z "$value" || "$value" == change-* ]]; then
    echo "Set a non-example value for $variable in .env" >&2
    exit 1
  fi
done

if [[ ! -d mysql/data/mysql && "${DB_USERNAME:-dovideo}" != "${MYSQL_APP_USER:-dovideo}" ]]; then
  echo "DB_USERNAME and MYSQL_APP_USER must match for a fresh database." >&2
  exit 1
fi

docker compose --env-file .env config --quiet
docker compose --env-file .env build python-api
docker compose --env-file .env up -d --wait --wait-timeout 180 \
  mysql redis minio qdrant rmqnamesrv rmqbroker

# The database is migrated before the API starts. The connection is created
# inside the Compose network, so host Python and a host MySQL port are optional.
docker compose --env-file .env run --rm -T --no-deps --entrypoint python python-api - <<'PY'
from alembic import command
from alembic.config import Config
from sqlalchemy import create_engine

from app.config import Settings

engine = create_engine(Settings().mysql_url, pool_pre_ping=True)
try:
    with engine.connect() as connection:
        config = Config("alembic.ini")
        config.attributes["connection"] = connection
        command.upgrade(config, "head")
finally:
    engine.dispose()
PY

./scripts/init_rocketmq.sh
docker compose --env-file .env up -d python-api

curl --fail --silent --show-error --retry 30 --retry-all-errors --retry-delay 1 \
  http://127.0.0.1:9090/health >/dev/null

if [[ -n "${SILICONFLOW_API_KEY:-}" ]]; then
  docker compose --env-file .env --profile worker up -d python-worker
  echo "Model credential present: Python analysis worker started."
else
  echo "No SILICONFLOW_API_KEY: API and infrastructure are ready; model analysis worker is not started."
fi

docker compose --env-file .env ps
echo "API: http://127.0.0.1:9090/health"
echo "Vue: cd frontend && npm ci && npm run dev -- --host 127.0.0.1"
