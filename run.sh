#!/usr/bin/env bash
# Один прогон car-finder. Используется и вручную, и из launchd.
set -euo pipefail
cd "$(dirname "$0")"
exec .venv/bin/python -m carfinder run "$@"
