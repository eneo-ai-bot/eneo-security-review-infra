ARG HERMES_IMAGE=nousresearch/hermes-agent@sha256:cd5d617d794b86ac7ac6ea084359aab53797b87ececcc19db4de210ec1e49cdc
FROM ${HERMES_IMAGE}

USER root
RUN apt-get update \
    && apt-get install -y --no-install-recommends ca-certificates curl gh \
    && rm -rf /var/lib/apt/lists/*

COPY --chown=hermes:hermes bootstrap/ /opt/eneo-bootstrap/
# Offline operator helpers imported by eneo-review-memory. The webhook agent
# cannot reach them because file, terminal, and code execution are disabled.
COPY --chown=root:root tools/eneo_review_*.py /usr/local/bin/
RUN cp /usr/local/bin/eneo_review_memory.py /usr/local/bin/eneo-review-memory \
    && cp /usr/local/bin/eneo_review_feedback_bridge.py /usr/local/bin/eneo-review-feedback-bridge \
    && chmod 0755 /opt/eneo-bootstrap/install.sh \
    /opt/eneo-bootstrap/install.py \
    /usr/local/bin/eneo-review-memory \
    /usr/local/bin/eneo-review-feedback-bridge

# Hermes runs s6-overlay as PID 1, which must start as root to initialize /run
# and then drops privileges to the unprivileged hermes user (uid 10000) on its
# own. Do NOT add `USER hermes` here: a non-root PID 1 leaves s6 unable to chown
# /run, and the container crash-loops at preinit (exit code 100). The gateway
# still runs unprivileged via that s6-managed privilege drop.
