# A local Linux environment for the real-browser partition on a Mac
# (ADR-0236). scripts/run_in_linux_container.sh builds it for linux/amd64, the
# architecture of the sidecar and hosted CI, and mounts the checkout at run
# time; nothing here is a release image.
FROM node:22.23.1-bookworm-slim@sha256:53ada149d435c38b14476cb57e4a7da73c15595aba79bd6971b547ceb6d018bf AS node
FROM python:3.12-slim@sha256:aa943ce411423c7582288f18707ff66eddcbaeb510502d747a72aeb34f30d159

ENV PLAYWRIGHT_BROWSERS_PATH=/ms-playwright \
    UV_PROJECT_ENVIRONMENT=/opt/venv \
    UV_FROZEN=1 \
    PYTHONDONTWRITEBYTECODE=1
COPY --from=node /usr/local/bin/node /usr/local/bin/node
COPY --from=node /usr/local/lib/node_modules /usr/local/lib/node_modules
RUN ln -s ../lib/node_modules/npm/bin/npm-cli.js /usr/local/bin/npm \
    && ln -s ../lib/node_modules/npm/bin/npx-cli.js /usr/local/bin/npx
WORKDIR /work

# The dependency set and Chromium change only with the lock file, so these
# layers stay cached between runs; the checkout itself is never copied in.
COPY pyproject.toml uv.lock /work/
RUN apt-get update \
    && apt-get install -y --no-install-recommends make git pandoc docker-cli \
    && rm -rf /var/lib/apt/lists/*
RUN python -m pip install --no-cache-dir uv==0.8.6 \
    && uv sync --all-groups --no-install-project \
    && rm -rf /root/.cache
# Hosted browsers are headed and draw to a private Xvfb display (ADR-0145).
RUN /opt/venv/bin/playwright install --with-deps chromium webkit \
    && apt-get install -y --no-install-recommends xvfb \
    && rm -rf /root/.cache /var/lib/apt/lists/*
# GHC's large virtual reservation can exhaust QEMU's address translation on
# Apple silicon. Limit only Pandoc's guest address space, not browser processes.
RUN printf '#!/bin/sh\nQEMU_RESERVED_VA=0x4000000000 exec /usr/bin/pandoc "$@"\n' > /usr/local/bin/pandoc \
    && chmod +x /usr/local/bin/pandoc
