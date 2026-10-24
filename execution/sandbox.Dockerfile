FROM python:3.12-slim-bookworm@sha256:6a0e06f5ba0b373d051aabd3fffb9b873f27f9a2a3a14bcbe37dee6f0e3bbc70

# The glibc image supports the pinned plotting wheels on amd64 and arm64.
# Install at image build, never from a model's network-denied runtime.
RUN apt-get update && apt-get install -y --no-install-recommends graphviz fonts-dejavu-core tini \
    && rm -rf /var/lib/apt/lists/*
COPY execution/visualization-requirements.txt /opt/agent/visualization-requirements.txt
RUN pip install --no-cache-dir --require-hashes --only-binary=:all: \
    -r /opt/agent/visualization-requirements.txt

RUN mkdir -p /opt/agent/agent_core/execution
COPY src/agent_core/__init__.py /opt/agent/agent_core/__init__.py
COPY src/agent_core/execution/__init__.py /opt/agent/agent_core/execution/__init__.py
COPY src/agent_core/execution/egress_core.py /opt/agent/agent_core/execution/egress_core.py
COPY src/agent_core/execution/proxy.py /opt/agent/agent_core/execution/proxy.py
COPY src/agent_core/execution/bridge_relay.py /opt/agent/agent_core/execution/bridge_relay.py
COPY src/agent_core/execution/visuals.py /opt/agent/agent_core/execution/visuals.py
COPY execution/visualization-guide.md /opt/agent/visualization-guide.md
RUN mkdir -p /workspace && chown 65534:65534 /workspace && chmod 0700 /workspace
# Commands run with a scrubbed environment; do not depend on PYTHONPATH.
RUN python -c 'import pathlib, site; (pathlib.Path(site.getsitepackages()[0]) / "agent_core.pth").write_text("/opt/agent\n")'
ENV PYTHONPATH=/opt/agent
WORKDIR /workspace
USER 65534:65534
# Reap orphaned command children so the PID quota does not retain zombies.
ENTRYPOINT ["/usr/bin/tini", "--"]
