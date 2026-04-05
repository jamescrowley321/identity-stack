"""Unit tests for integration test infrastructure (AC-1.5.5).

This file previously contained AST-based tests that parsed the integration
conftest.py source code to verify fixture names and imports. Those tests
verified string presence in source rather than application behavior and
have been removed. Integration fixture correctness is validated by
actually running the integration tests.
"""
