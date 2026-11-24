"""Convention entry point for the hosted browser session control-plane contract.

The contract is the wire-compatibility cases in
``tests/contract/test_profile_service_http_contract.py`` (every lease and
data-plane operation agrees between the client and the isolated service, and an
older worker still parses a response that carries facts, ADR-0129 D26); they
run once, there. This module exists so the ``BrowserSessionControlPlane`` port
keeps a contract module of its own (``contract_coverage_errors``).
"""
