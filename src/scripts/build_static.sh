#!/usr/bin/env bash
set -eu

# Resolve directories
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
BACKEND_DIR="$(cd "${SCRIPT_DIR}/../backend" && pwd)"
FRONTEND_DIR="$(cd "${SCRIPT_DIR}/../frontend" && pwd)"
ROOT_DIR="$(cd "${SCRIPT_DIR}/.." && pwd)"
# deploy_assets/ is intentionally NOT named "static/" — .gitignore excludes static/
# directories. Databricks Apps deployment snapshots only include git-tracked files,
# so we commit deploy_assets.tar.gz and extract it here when needed.
DEST_DIR="${ROOT_DIR}/deploy_assets"
SRC_DIR="${FRONTEND_DIR}/static"
ARCHIVE="${ROOT_DIR}/deploy_assets.tar.gz"

build_frontend() {
  if command -v yarn >/dev/null 2>&1; then
    yarn --cwd "${FRONTEND_DIR}" install --frozen-lockfile
    yarn --cwd "${FRONTEND_DIR}" build
  else
    npm --prefix "${FRONTEND_DIR}" ci --silent --no-audit --no-fund || npm --prefix "${FRONTEND_DIR}" install --silent --no-audit --no-fund
    npm --prefix "${FRONTEND_DIR}" run build
  fi
}

ensure_deploy_assets() {
  if [[ -f "${DEST_DIR}/index.html" ]]; then
    return 0
  fi
  if [[ ! -f "${ARCHIVE}" ]]; then
    return 1
  fi
  echo "Extracting prebuilt assets from ${ARCHIVE}..."
  rm -rf "${DEST_DIR}"
  tar -xzf "${ARCHIVE}" -C "${ROOT_DIR}"
}

package_deploy_assets() {
  if [[ ! -f "${DEST_DIR}/index.html" ]]; then
    return 1
  fi
  echo "Packaging ${DEST_DIR} into ${ARCHIVE}..."
  tar -czf "${ARCHIVE}" -C "${ROOT_DIR}" deploy_assets
}

ensure_deploy_assets || true

# Databricks Apps runs this script in its build container (source lives under /app)
# where npm/yarn cannot reach a package registry. Use the committed tarball instead.
if [[ "${SCRIPT_DIR}" == /app/* ]]; then
  if ensure_deploy_assets; then
    echo "Databricks Apps build: using prebuilt deploy_assets."
    exit 0
  fi
  echo "Error: deploy_assets.tar.gz missing or empty in Databricks Apps build." >&2
  exit 1
fi

echo "Building frontend in ${FRONTEND_DIR}..."
if ! build_frontend; then
  if ensure_deploy_assets; then
    echo "Frontend build failed; using prebuilt deploy_assets."
    exit 0
  fi
  echo "Error: frontend build failed and no prebuilt assets are available." >&2
  exit 1
fi

if [[ ! -d "${SRC_DIR}" ]]; then
  echo "Error: build output not found at ${SRC_DIR}"
  exit 1
fi

echo "Copying assets from ${SRC_DIR} to ${DEST_DIR}..."
rm -rf "${DEST_DIR}"
mkdir -p "${DEST_DIR}"
if command -v rsync >/dev/null 2>&1; then
  rsync -a --delete "${SRC_DIR}/" "${DEST_DIR}/"
else
  cp -R "${SRC_DIR}/." "${DEST_DIR}/"
fi

package_deploy_assets
echo "Static assets copied to ${DEST_DIR} and packaged as ${ARCHIVE}"
