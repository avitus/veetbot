FROM python:3.12-alpine@sha256:6d43704baacd1bfbe7c295d7f13079d5d8104ed33568873133f8fc69980419df

RUN mkdir -p /opt/agent/agent_core/execution
COPY src/agent_core/__init__.py /opt/agent/agent_core/__init__.py
COPY src/agent_core/execution/__init__.py /opt/agent/agent_core/execution/__init__.py
COPY src/agent_core/execution/egress_core.py /opt/agent/agent_core/execution/egress_core.py
COPY src/agent_core/execution/proxy.py /opt/agent/agent_core/execution/proxy.py
COPY src/agent_core/execution/bridge_relay.py /opt/agent/agent_core/execution/bridge_relay.py
ENV PYTHONPATH=/opt/agent
WORKDIR /workspace
USER nobody:nobody
