FROM python:3.12-slim@sha256:5e2bdb1b0d542d5039cffb8181c65930093949194a65c365dfb1b0fe13a4fa5b

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
RUN python -m pip install --no-cache-dir pip==26.3 uv==0.13.9 \
    && uv sync --frozen --no-dev --no-editable --no-install-project \
    && rm -rf /root/.cache
# The user's login ceremony runs headed Chromium on a private Xvfb display
# (ADR-0106); the package is named here rather than left to Playwright's list.
RUN /opt/veetbot/.venv/bin/playwright install --with-deps chromium \
    && apt-get install -y --no-install-recommends xvfb zlib1g \
    && chmod -R a+rX /ms-playwright \
    && rm -rf /root/.cache /var/lib/apt/lists/*
# Headed Chromium downloads a spelling dictionary from its vendor once a text
# field takes focus, and no launch switch stops it (ADR-0146). Playwright
# installs Chrome for Testing on x86_64 and its own Chromium build on arm64,
# and each reads managed policy from its own directory.
RUN install -d /etc/opt/chrome_for_testing/policies/managed /etc/chromium/policies/managed \
    && printf '%s\n' '{"SpellcheckLanguageBlocklist": ["en-US"]}' \
        | tee /etc/opt/chrome_for_testing/policies/managed/veetbot.json \
            > /etc/chromium/policies/managed/veetbot.json

# Only the layers below are rebuilt when the application source changes.
COPY README.md /opt/veetbot/
COPY src /opt/veetbot/src
RUN uv sync --frozen --no-dev --no-editable \
    && rm -rf /root/.cache
ENV PATH="/opt/veetbot/.venv/bin:$PATH"

WORKDIR /var/lib/veetbot/browser-profiles
USER 65532:65532
ENTRYPOINT ["browser-profile-service"]
