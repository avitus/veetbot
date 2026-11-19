FROM python:3.12-slim-bookworm@sha256:935c3c5b12357a7e10caa9c1e6ca5692344d97cf229832ef584be2cec9fc03df

# The glibc image supports the pinned plotting wheels on amd64 and arm64.
# Install at image build, never from a model's network-denied runtime.
RUN apt-get update && apt-get install -y --no-install-recommends graphviz fonts-dejavu-core tini \
    && rm -rf /var/lib/apt/lists/*
COPY execution/visualization-requirements.txt /opt/agent/visualization-requirements.txt
RUN python -m pip install --no-cache-dir pip==26.3 \
    && pip install --no-cache-dir --require-hashes --only-binary=:all: \
    -r /opt/agent/visualization-requirements.txt

RUN mkdir -p /opt/agent/agent_core/execution
COPY src/agent_core/__init__.py /opt/agent/agent_core/__init__.py
COPY src/agent_core/execution/__init__.py /opt/agent/agent_core/execution/__init__.py
COPY src/agent_core/execution/egress_core.py /opt/agent/agent_core/execution/egress_core.py
COPY src/agent_core/execution/proxy.py /opt/agent/agent_core/execution/proxy.py
COPY src/agent_core/execution/bridge_relay.py /opt/agent/agent_core/execution/bridge_relay.py
COPY src/agent_core/execution/job_sdk.py /opt/agent/agent_core/execution/job_sdk.py
COPY src/agent_core/execution/job_runner.py /opt/agent/agent_core/execution/job_runner.py
COPY src/agent_core/execution/visuals.py /opt/agent/agent_core/execution/visuals.py
COPY src/agent_core/execution/visualization-guide.md /opt/agent/visualization-guide.md
RUN mkdir -p /workspace && chown 65534:65534 /workspace && chmod 0700 /workspace
# Commands run with a scrubbed environment; do not depend on PYTHONPATH.
RUN python -c 'import pathlib, site; (pathlib.Path(site.getsitepackages()[0]) / "agent_core.pth").write_text("/opt/agent\n")'
ENV PYTHONPATH=/opt/agent
WORKDIR /workspace
USER 65534:65534
# Reap orphaned command children so the PID quota does not retain zombies.
ENTRYPOINT ["/usr/bin/tini", "--"]
