"""Shared fixtures."""

from __future__ import annotations

from types import SimpleNamespace

import pytest


class AllowAllGuard:
    """Stands in for KubernetesTargetGuard: no DNS in unit tests."""

    async def check(self, service_url: str):
        return SimpleNamespace(allowed=True, reason=None, url=service_url, address=None)


@pytest.fixture(autouse=True)
def _no_dns_target_guard(monkeypatch):
    """ManagedTunnel.start() runs the target guard; tests must not resolve DNS.

    Tests of the guard itself pass their own to ManagedTunnel(guard=...).
    """
    monkeypatch.setattr("hle_operator.tunnel_manager._default_guard", lambda: AllowAllGuard())
