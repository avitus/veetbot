FROM python:3.12-slim@sha256:2c941e860699f878900b0edc2403613c234d4b32eda3cc9fa7036991a2a63c4a

RUN groupadd --gid 65532 browser-profile \
    && useradd --uid 65532 --gid 65532 --no-create-home --shell /usr/sbin/nologin browser-profile \
    && mkdir -p /opt/veetbot /var/lib/veetbot/browser-profiles \
    && chown -R 65532:65532 /opt/veetbot /var/lib/veetbot \
    && chmod 0700 /var/lib/veetbot/browser-profiles
COPY pyproject.toml uv.lock README.md /opt/veetbot/
COPY src /opt/veetbot/src
WORKDIR /opt/veetbot
ENV PLAYWRIGHT_BROWSERS_PATH=/ms-playwright
RUN python -m pip install --no-cache-dir uv==0.8.6 \
    && uv sync --frozen --no-dev --no-editable \
    && uv run playwright install --with-deps chromium \
    && chmod -R a+rX /ms-playwright \
    && rm -rf /root/.cache
ENV PATH="/opt/veetbot/.venv/bin:$PATH"

WORKDIR /var/lib/veetbot/browser-profiles
USER 65532:65532
ENTRYPOINT ["browser-profile-service"]
