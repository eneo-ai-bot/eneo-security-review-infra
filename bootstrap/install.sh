#!/bin/sh
set -eu
export HERMES_HOME="${HERMES_HOME:-/opt/data}"
if [ "$(id -u)" = "0" ] && [ -x /command/s6-setuidgid ]; then
  exec /command/s6-setuidgid hermes /opt/hermes/.venv/bin/python /opt/eneo-bootstrap/install.py "$@"
fi
exec /opt/hermes/.venv/bin/python /opt/eneo-bootstrap/install.py "$@"
