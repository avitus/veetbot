FROM python:3.12-slim@sha256:2c941e860699f878900b0edc2403613c234d4b32eda3cc9fa7036991a2a63c4a

RUN groupadd --gid 65532 browser-profile \
    && useradd --uid 65532 --gid 65532 --no-create-home --shell /usr/sbin/nologin browser-profile \
    && mkdir -p /opt/veetbot /var/lib/veetbot/browser-profiles \
    && chown -R 65532:65532 /opt/veetbot /var/lib/veetbot \
    && chmod 0700 /var/lib/veetbot/browser-profiles
WORKDIR /opt/veetbot
ENV PLAYWRIGHT_BROWSERS_PATH=/ms-playwright

# Layer order bounds disk usage on the release host. The dependency set,
# Chromium, and its system libraries change only when the lock file changes,
# so they stay above the application source and every release image shares
# them instead of snapshotting them again.
COPY pyproject.toml uv.lock /opt/veetbot/
RUN python -m pip install --no-cache-dir uv==0.8.6 \
    && uv sync --frozen --no-dev --no-editable --no-install-project \
    && rm -rf /root/.cache
RUN /opt/veetbot/.venv/bin/playwright install --with-deps chromium \
    && chmod -R a+rX /ms-playwright \
    && rm -rf /root/.cache /var/lib/apt/lists/*

# Only the layers below are rebuilt when the application source changes.
COPY README.md /opt/veetbot/
COPY src /opt/veetbot/src
RUN uv sync --frozen --no-dev --no-editable \
    && rm -rf /root/.cache
ENV PATH="/opt/veetbot/.venv/bin:$PATH"

WORKDIR /var/lib/veetbot/browser-profiles
USER 65532:65532
ENTRYPOINT ["browser-profile-service"]
