# The Cockpit widget builder (docs/plan/cockpit.md, ADR-0240). It holds main's
# cockpit/ workspace, built, and runs widget builds under the execution
# service with no network, a read-only root and no credentials. Debian rather
# than the sandbox's Alpine base, because the later preview renderer's browser
# needs glibc; Python stays, because the execution service manages every
# sandbox workspace with it.
FROM node:22.23.1-bookworm-slim@sha256:53ada149d435c38b14476cb57e4a7da73c15595aba79bd6971b547ceb6d018bf

RUN apt-get update \
  && apt-get install --yes --no-install-recommends python3 \
  && rm -rf /var/lib/apt/lists/* \
  && ln -s /usr/bin/python3 /usr/local/bin/python

WORKDIR /opt/cockpit
COPY cockpit/package.json cockpit/package-lock.json ./
RUN npm ci --omit=dev --no-audit --no-fund && npm cache clean --force
COPY cockpit/tsconfig.json ./
COPY cockpit/builder ./builder
COPY cockpit/kit ./kit
COPY cockpit/host ./host
COPY cockpit/schema ./schema
COPY cockpit/scripts ./scripts
COPY cockpit/examples/widgets ./examples/widgets
RUN node scripts/build-runtime.mjs

RUN mkdir -p /workspace && chown nobody:nogroup /workspace && chmod 0700 /workspace
ENV NODE_ENV=production
WORKDIR /workspace
USER nobody:nogroup
