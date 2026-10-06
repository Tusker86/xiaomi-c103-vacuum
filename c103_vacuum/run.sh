#!/usr/bin/with-contenv bashio
set -euo pipefail

# The Xiaomi session, the robot list and the saved maps live in the HA config folder.
export C103_DATA=/homeassistant/vacuum_app/data
export C103_PORT=8099

cd /app/src
exec /venv/bin/python -u -m c103app.server
