# A local Linux environment for the real-browser partition on a Mac
# (ADR-0236). scripts/run_in_linux_container.sh builds it for linux/amd64, the
# architecture of the sidecar and hosted CI, and mounts the checkout at run
# time; nothing here is a release image.
FROM python:3.12-slim@sha256:aa943ce411423c7582288f18707ff66eddcbaeb510502d747a72aeb34f30d159

ENV PLAYWRIGHT_BROWSERS_PATH=/ms-playwright \
    UV_PROJECT_ENVIRONMENT=/opt/venv \
    UV_FROZEN=1 \
    PYTHONDONTWRITEBYTECODE=1
WORKDIR /work

# The dependency set and Chromium change only with the lock file, so these
# layers stay cached between runs; the checkout itself is never copied in.
COPY pyproject.toml uv.lock /work/
RUN apt-get update \
    && apt-get install -y --no-install-recommends make git \
    && rm -rf /var/lib/apt/lists/*
RUN python -m pip install --no-cache-dir uv==0.8.6 \
    && uv sync --all-groups --no-install-project \
    && rm -rf /root/.cache
# Hosted browsers are headed and draw to a private Xvfb display (ADR-0145).
RUN /opt/venv/bin/playwright install --with-deps chromium \
    && apt-get install -y --no-install-recommends xvfb \
    && rm -rf /root/.cache /var/lib/apt/lists/*
