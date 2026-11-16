#!/bin/sh
# Local service only. Does not start model requests or edit Sub2API.
set -eu
cd "$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)"
if [ ! -x .venv/bin/python ]; then
  echo 'Create a Python 3.10+ environment at .venv and install detector/requirements.txt.' >&2
  exit 1
fi
if [ ! -f web/dist/index.html ]; then
  echo 'Build the dashboard first: cd web && npm ci && npm run build' >&2
  exit 1
fi
export DROOL_DATA_DIR="${DROOL_DATA_DIR:-./data}"
exec .venv/bin/python -m detector.server --host 127.0.0.1 --port "${DROOL_WEB_PORT:-4191}"
