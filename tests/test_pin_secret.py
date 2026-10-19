"""Visitor PIN from a Secret: pinSecretRef on HLETunnels, hle.world/pin-secret on Ingresses."""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

from hle_operator import handlers
from hle_operator.declared import (
    DeclarationError,
    crd_pin_secret_ref,
    crd_to_declared,
    ingress_pin_secret_ref,
    ingress_to_declared,
)
from tests.test_registry import Harness, crd_body, ingress_body, ready


def crd_with_pin_secret(**ac) -> dict:
    ac = {"pinSecretRef": {"name": "pin-secret", "key": "pin"}, **ac}
    return crd_body(name="a", namespace="monitoring", accessControl=ac)


def ingress_with_pin_secret(value: str = "pin-secret") -> dict:
    body = ingress_body()
    body["metadata"]["annotations"] = {"hle.world/pin-secret": value}
    return body


class TestRefs:
    def test_crd_ref_defaults_key_and_uses_own_namespace(self):
        body = crd_body(
            namespace="monitoring", accessControl={"pinSecretRef": {"name": "pin-secret"}}
        )
        assert crd_pin_secret_ref(body) == "monitoring/pin-secret#pin"

    def test_crd_ref_needs_a_name(self):
        body = crd_body(accessControl={"pinSecretRef": {"key": "pin"}})
        with pytest.raises(DeclarationError):
            crd_pin_secret_ref(body)

    def test_ingress_annotation_forms(self):
        assert ingress_pin_secret_ref(ingress_with_pin_secret("s")) == "apps/s#pin"
        assert ingress_pin_secret_ref(ingress_with_pin_secret("s#code")) == "apps/s#code"
        assert ingress_pin_secret_ref(ingress_body()) is None

    def test_ingress_annotation_cannot_name_another_namespace(self):
        with pytest.raises(DeclarationError, match="differs"):
            ingress_pin_secret_ref(ingress_with_pin_secret("kube-system/s#pin"))


class TestDeclared:
    def test_pin_comes_from_the_secret(self):
        result = crd_to_declared(crd_with_pin_secret(), pin_secret="48213")
        assert result.access is not None
        assert result.access.pin == "48213"

    def test_ingress_pin_comes_from_the_secret(self):
        result = ingress_to_declared(ingress_with_pin_secret(), pin_secret="48213")
        assert result.access is not None
        assert result.access.pin == "48213"

    def test_missing_secret_fails_closed(self):
        with pytest.raises(DeclarationError, match="pin Secret"):
            crd_to_declared(crd_with_pin_secret(), pin_secret=None)
        with pytest.raises(DeclarationError, match="pin Secret"):
            ingress_to_declared(ingress_with_pin_secret(), pin_secret=None)

    def test_inline_and_secret_together_are_refused(self):
        with pytest.raises(DeclarationError, match="not both"):
            crd_to_declared(crd_with_pin_secret(pin="1234"), pin_secret="48213")

    def test_inline_pin_still_works(self):
        body = crd_body(accessControl={"pin": "1234"})
        assert crd_to_declared(body).access.pin == "1234"


class TestRegistry:
    async def test_pin_is_read_from_the_secret_and_not_stored_in_the_object(self):
        h = Harness()
        await ready(h)
        h.secrets["monitoring/pin-secret#pin"] = "48213"
        body = crd_with_pin_secret()
        await h.registry.apply_crd(body)
        (endpoint,) = h.registry.declared()
        assert endpoint.access.pin == "48213"
        assert "48213" not in repr(body)

    async def test_unreadable_secret_fails_the_tunnel(self):
        h = Harness()
        await ready(h)
        await h.registry.apply_crd(crd_with_pin_secret())
        assert h.registry.declared() == []
        (target,) = h.registry.targets()
        assert "pin Secret" in (target.error or "")

    async def test_rotation_redeclares(self):
        h = Harness()
        await ready(h)
        h.secrets["monitoring/pin-secret#pin"] = "48213"
        await h.registry.apply_crd(crd_with_pin_secret())
        h.secrets["monitoring/pin-secret#pin"] = "99999"
        assert await h.registry.poll_secrets_once() is True
        assert h.registry.declared()[0].access.pin == "99999"

    async def test_pin_secret_in_another_namespace_is_never_read(self):
        h = Harness()
        await ready(h)
        h.secrets["kube-system/s#pin"] = "stolen"
        await h.registry.apply_ingress(ingress_with_pin_secret("kube-system/s#pin"))
        assert h.secret_reads == 0
        assert h.registry.declared() == []


class TestLegacyPin:
    async def test_crd_pin_secret_is_resolved(self):
        spec = {
            "serviceRef": {"name": "web", "port": 80},
            "label": "web",
            "accessControl": {"pinSecretRef": {"name": "pin-secret"}},
        }
        patch_obj = SimpleNamespace(status={})
        tunnel = MagicMock()

        async def start() -> None:
            return None

        tunnel.start = start

        async def wait_connected(timeout: float = 0) -> bool:
            return False

        tunnel.wait_connected = wait_connected
        with (
            patch("hle_operator.handlers._read_secret_key", return_value="48213") as read,
            patch("hle_operator.handlers.ManagedTunnel", return_value=tunnel) as mock_tunnel,
            patch("hle_operator.handlers.get_api_key", return_value="hle_key"),
        ):
            await handlers.on_tunnel_create(
                spec=spec, name="x", namespace="team-a", patch=patch_obj, logger=MagicMock()
            )
        read.assert_called_once_with("pin-secret", "team-a", "pin")
        assert mock_tunnel.call_args.kwargs["spec"].access_control.pin == "48213"
        handlers._tunnels.clear()

    async def test_unreadable_pin_secret_fails_closed(self):
        spec = {
            "serviceRef": {"name": "web", "port": 80},
            "label": "web",
            "accessControl": {"pinSecretRef": {"name": "pin-secret"}},
        }
        patch_obj = SimpleNamespace(status={})
        with (
            patch("hle_operator.handlers._read_secret_key", return_value=None),
            patch("hle_operator.handlers.ManagedTunnel") as mock_tunnel,
        ):
            result = await handlers.on_tunnel_create(
                spec=spec, name="x", namespace="team-a", patch=patch_obj, logger=MagicMock()
            )
        assert result == {"phase": "Failed"}
        mock_tunnel.assert_not_called()
