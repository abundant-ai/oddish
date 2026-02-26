#!/usr/bin/env bash
set -euo pipefail

# Start frontend locally on an HTTPS oddish.app subdomain so Clerk prod keys work.
# Usage:
#   ./run-prod-clerk-local.sh
# Optional env overrides:
#   SUBDOMAIN=local.oddish.app PORT=443 ./run-prod-clerk-local.sh

FRONTEND_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

SUBDOMAIN="${SUBDOMAIN:-local.oddish.app}"
PORT="${PORT:-443}"
if [[ "${PORT}" == "443" ]]; then
  APP_URL="https://${SUBDOMAIN}"
else
  APP_URL="https://${SUBDOMAIN}:${PORT}"
fi

CERT_DIR="${FRONTEND_DIR}/.certs"
CERT_FILE="${CERT_DIR}/${SUBDOMAIN}.pem"
KEY_FILE="${CERT_DIR}/${SUBDOMAIN}-key.pem"
ENV_FILE="${FRONTEND_DIR}/.env.local"

if ! command -v pnpm >/dev/null 2>&1; then
  echo "Error: pnpm is not installed or not on PATH."
  exit 1
fi

if ! command -v mkcert >/dev/null 2>&1; then
  echo "Error: mkcert is required."
  echo "Install: brew install mkcert"
  exit 1
fi

if [[ "${PORT}" -lt 1024 && "${EUID}" -ne 0 ]]; then
  echo "Port ${PORT} requires elevated privileges. Re-running with sudo..."
  exec sudo -E SUBDOMAIN="${SUBDOMAIN}" PORT="${PORT}" "$0" "$@"
fi

if ! grep -Eq "^[[:space:]]*127\.0\.0\.1[[:space:]]+${SUBDOMAIN}([[:space:]]|$)" /etc/hosts; then
  echo "Missing /etc/hosts entry for ${SUBDOMAIN}."
  echo "Run:"
  echo "  echo \"127.0.0.1 ${SUBDOMAIN}\" | sudo tee -a /etc/hosts"
  exit 1
fi

mkdir -p "${CERT_DIR}"

if [[ ! -f "${CERT_FILE}" || ! -f "${KEY_FILE}" ]]; then
  echo "Generating TLS cert for ${SUBDOMAIN} with mkcert..."
  (
    cd "${CERT_DIR}"
    mkcert -install
    mkcert "${SUBDOMAIN}"
  )
fi

if [[ -f "${ENV_FILE}" ]]; then
  if ! rg -n "^NEXT_PUBLIC_APP_URL=" "${ENV_FILE}" >/dev/null 2>&1; then
    echo "Warning: NEXT_PUBLIC_APP_URL is not set in ${ENV_FILE}"
    echo "Suggested:"
    echo "  NEXT_PUBLIC_APP_URL=${APP_URL}"
  elif ! rg -n "^NEXT_PUBLIC_APP_URL=${APP_URL}$" "${ENV_FILE}" >/dev/null 2>&1; then
    echo "Warning: NEXT_PUBLIC_APP_URL in ${ENV_FILE} does not match ${APP_URL}"
    echo "Current value:"
    rg -n "^NEXT_PUBLIC_APP_URL=" "${ENV_FILE}" || true
  fi
fi

echo "Starting frontend at ${APP_URL}"
echo "Ensure your .env.local uses Clerk production keys for oddish.app."

cd "${FRONTEND_DIR}"
pnpm exec next dev \
  --hostname "${SUBDOMAIN}" \
  --port "${PORT}" \
  --experimental-https \
  --experimental-https-cert "${CERT_FILE}" \
  --experimental-https-key "${KEY_FILE}"
