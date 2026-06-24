ARG HERMES_IMAGE=nousresearch/hermes-agent:latest
FROM ${HERMES_IMAGE}

USER root
RUN apt-get update \
    && apt-get install -y --no-install-recommends ca-certificates curl gh \
    && rm -rf /var/lib/apt/lists/*

COPY --chown=hermes:hermes bootstrap/ /opt/eneo-bootstrap/
COPY --chown=root:root tools/eneo_review_memory.py /usr/local/bin/eneo-review-memory
# Offline operator report helper imported by eneo-review-memory. The webhook
# agent cannot reach it because file, terminal, and code execution are disabled.
COPY --chown=root:root tools/eneo_review_learning.py /usr/local/bin/eneo_review_learning.py
RUN chmod 0755 /opt/eneo-bootstrap/install.sh \
    /opt/eneo-bootstrap/install.py \
    /usr/local/bin/eneo-review-memory

# Hermes runs s6-overlay as PID 1, which must start as root to initialize /run
# and then drops privileges to the unprivileged hermes user (uid 10000) on its
# own. Do NOT add `USER hermes` here: a non-root PID 1 leaves s6 unable to chown
# /run, and the container crash-loops at preinit (exit code 100). The gateway
# still runs unprivileged via that s6-managed privilege drop.
