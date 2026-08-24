#!/usr/bin/env bash
# GemmaSim launcher (macOS / Linux). Creates a venv on first run, installs
# requirements, and starts the dev server.
set -euo pipefail
cd "$(dirname "$0")"

PY="${PYTHON:-python3}"
if ! command -v "$PY" >/dev/null 2>&1; then
  echo "ERROR: $PY not found. Install Python 3.10+." >&2
  exit 1
fi

if [ ! -d .venv ]; then
  echo "Creating virtual environment in .venv ..."
  "$PY" -m venv .venv
fi

# shellcheck disable=SC1091
source .venv/bin/activate

echo "Installing dependencies ..."
python -m pip install --upgrade pip --quiet --disable-pip-version-check
python -m pip install -r requirements.txt --quiet --disable-pip-version-check

echo
echo "GemmaSim is starting on http://127.0.0.1:5000"
echo "Press CTRL+C to stop."
echo

# GEMMASIM_LAUNCHER tells the app to request a restart by exiting with code 42
# (the "Update & restart" button) instead of re-exec'ing itself. We relaunch
# here whenever that code comes back — a clean restart on the freshly pulled code.
export GEMMASIM_LAUNCHER=1
while true; do
  # `if` suppresses `set -e` for the call so we can read the exit code ourselves.
  if python run.py; then code=0; else code=$?; fi
  [ "$code" -eq 42 ] || exit "$code"
  echo
  echo "Restarting GemmaSim..."
  echo
done
