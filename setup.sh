#!/usr/bin/env bash
# Cerberus setup. Idempotent. Installs Python deps, Playwright Chromium, and Lighthouse CLI.
set -euo pipefail

cd "$(dirname "$0")"

PY=${PY:-python3.11}
if ! command -v "$PY" >/dev/null 2>&1; then
  PY=python3
fi
echo "[cerberus] Using $($PY --version) at $(command -v $PY)"

if ! command -v uv >/dev/null 2>&1; then
  echo "[cerberus] uv not found. Install via: curl -LsSf https://astral.sh/uv/install.sh | sh"
  exit 1
fi

echo "[cerberus] Creating venv + installing Python deps..."
uv venv --python "$PY" .venv
# shellcheck disable=SC1091
source .venv/bin/activate
uv pip install -e .

echo "[cerberus] Installing Playwright Chromium..."
python -m playwright install chromium

if ! command -v node >/dev/null 2>&1; then
  echo "[cerberus] node not found — install Node 18+ then re-run setup."
  exit 1
fi

if ! command -v lighthouse >/dev/null 2>&1; then
  echo "[cerberus] Installing Lighthouse CLI globally..."
  npm i -g lighthouse
else
  echo "[cerberus] Lighthouse already installed: $(lighthouse --version)"
fi

if ! command -v xmllint >/dev/null 2>&1; then
  echo "[cerberus] (warning) xmllint not found — used by F10. apt: sudo apt install -y libxml2-utils"
fi

echo "[cerberus] Setup complete. Run: source .venv/bin/activate && python run.py"
