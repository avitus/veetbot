# The code sandbox behind code.test (ADR-0281). It holds a release's tracked
# source at /source and the locked tools that check it, and runs model-written
# code under the execution service with no network, a read-only root and no
# credentials. Each COPY names its source, because a release's build context also
# holds .release.env and the virtual environment. The project itself is not
# installed: a check imports the draft from its workspace, never a baked copy.
FROM node:22.23.1-bookworm-slim@sha256:53ada149d435c38b14476cb57e4a7da73c15595aba79bd6971b547ceb6d018bf AS node_runtime

FROM python:3.12-slim-bookworm@sha256:6a0e06f5ba0b373d051aabd3fffb9b873f27f9a2a3a14bcbe37dee6f0e3bbc70

# tini reaps what a test run orphans, so the PID quota does not retain zombies.
RUN apt-get update && apt-get install -y --no-install-recommends tini \
    && rm -rf /var/lib/apt/lists/*
COPY --from=node_runtime /usr/local/bin/node /usr/local/bin/node
COPY --from=node_runtime /usr/local/lib/node_modules/npm /opt/code-sandbox/npm

# Layer order bounds disk usage on the release host: these change only with the
# lock files, so every release image shares them.
COPY pyproject.toml uv.lock /opt/code-sandbox/project/
RUN python -m pip install --no-cache-dir uv==0.8.6 \
    && cd /opt/code-sandbox/project \
    && UV_PROJECT_ENVIRONMENT=/opt/code-sandbox/venv UV_PYTHON_DOWNLOADS=never \
        uv sync --frozen --no-install-project --no-default-groups \
            --group dev --group test --compile-bytecode \
    && python -m pip uninstall --yes uv \
    && rm -rf /root/.cache
COPY cockpit/package.json cockpit/package-lock.json /opt/code-sandbox/cockpit/
RUN cd /opt/code-sandbox/cockpit \
    && node /opt/code-sandbox/npm/bin/npm-cli.js ci --omit=dev --no-audit --no-fund \
    && rm -rf /opt/code-sandbox/npm /root/.npm

# Only the layers below are rebuilt when the source changes.
COPY src/agent_core/execution/code_checks.py /opt/code-sandbox/code_checks.py
COPY src /source/src
COPY tests /source/tests
COPY execution /source/execution
COPY scripts /source/scripts
COPY client /source/client
COPY clients /source/clients
COPY migrations /source/migrations
COPY evals /source/evals
COPY docs /source/docs
COPY samples /source/samples
COPY security /source/security
COPY deploy /source/deploy
COPY nginx /source/nginx
COPY website /source/website
COPY .circleci /source/.circleci
COPY .github /source/.github
COPY cockpit/README.md cockpit/eslint.config.mjs cockpit/package.json cockpit/package-lock.json cockpit/tsconfig.json /source/cockpit/
COPY cockpit/builder /source/cockpit/builder
COPY cockpit/examples /source/cockpit/examples
COPY cockpit/host /source/cockpit/host
COPY cockpit/kit /source/cockpit/kit
COPY cockpit/schema /source/cockpit/schema
COPY cockpit/scripts /source/cockpit/scripts
COPY cockpit/tests /source/cockpit/tests
COPY .env.example .python-version AGENTS.md CLAUDE.md Makefile README.md alembic.ini docker-compose.yml docs-manifest.yaml mkdocs.yml pyproject.toml uv.lock /source/
# The link is copied as a link, so a check resolves the locked packages without
# copying them into its workspace.
RUN ln -s /opt/code-sandbox/cockpit/node_modules /source/cockpit/node_modules \
    && chmod -R a+rX /source /opt/code-sandbox

RUN mkdir -p /workspace && chown 65534:65534 /workspace && chmod 0700 /workspace
WORKDIR /workspace
USER 65534:65534
ENTRYPOINT ["/usr/bin/tini", "--"]
