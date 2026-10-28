"""Shared standing-authorization fail-closed contract.

The contract is the five cases in ``tests/unit/test_browser_grants.py`` and
``tests/unit/test_browser_composition.py`` (exact grant authorizes without an
interactive approval; a revoked grant falls back to it); they run in the unit
partition. This module exists so the ``StandingAuthorizer`` port keeps a contract
module of its own (``contract_coverage_errors``).
"""
