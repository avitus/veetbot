"""Convention entry point for the shared surface contract.

The in-memory suite runs once, in
``tests/contract/test_surface_repositories_contract.py``; the PostgreSQL suite
in ``tests/integration/test_surface_persistence_m14.py``. This module exists so the
port keeps a contract module of its own (``contract_coverage_errors``).
"""
