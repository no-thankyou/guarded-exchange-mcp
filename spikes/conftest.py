"""Spikes reuse the test-suite network guard (NFR-2): any non-loopback connection fails loudly."""

from tests.conftest import _no_external_network

__all__ = ["_no_external_network"]
