#!/bin/sh
set -eu
export HERMES_HOME="${HERMES_HOME:-/opt/data}"
if [ "$(id -u)" = "0" ] && [ -x /command/s6-setuidgid ]; then
  # The base image seeds HERMES_HOME defaults (SOUL.md, config.yaml, bundled
  # skills) as root and read-only on first boot. Hand ownership to the
  # unprivileged hermes service user AND restore the owner write bit before
  # dropping privileges, so the installer can overwrite the managed files and
  # the gateway can read/write its own state under /opt/data. (chown alone is
  # not enough: a hermes-owned but mode-0444 file still rejects an overwrite.)
  chown -R hermes:hermes "$HERMES_HOME"
  chmod -R u+rwX "$HERMES_HOME"
  exec /command/s6-setuidgid hermes /opt/hermes/.venv/bin/python /opt/eneo-bootstrap/install.py "$@"
fi
exec /opt/hermes/.venv/bin/python /opt/eneo-bootstrap/install.py "$@"
