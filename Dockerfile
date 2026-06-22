ARG HERMES_IMAGE=nousresearch/hermes-agent:latest
FROM ${HERMES_IMAGE}

USER root
RUN apt-get update \
    && apt-get install -y --no-install-recommends ca-certificates curl gh \
    && rm -rf /var/lib/apt/lists/*

COPY --chown=hermes:hermes bootstrap/ /opt/eneo-bootstrap/
COPY --chown=root:root tools/eneo_review_memory.py /usr/local/bin/eneo-review-memory
RUN chmod 0755 /opt/eneo-bootstrap/install.sh \
    /opt/eneo-bootstrap/install.py \
    /usr/local/bin/eneo-review-memory

USER hermes
