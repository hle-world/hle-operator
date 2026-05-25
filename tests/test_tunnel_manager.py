"""Tests for CRD and Ingress spec parsing + ManagedTunnel lifecycle."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from hle_operator.tunnel_manager import (
    AccessControlSpec,
    AccessRule,
    ManagedTunnel,
    ManagedTunnelSpec,
    parse_crd_spec,
    parse_ingress_annotations,
)

# ---------------------------------------------------------------------------
# parse_crd_spec
# ---------------------------------------------------------------------------


class TestParseCrdSpec:
    def test_minimal_spec(self):
        spec = {
            "serviceRef": {"name": "grafana", "port": 3000},
            "label": "grafana",
        }
        result = parse_crd_spec(spec)
        assert result.service_url == "http://grafana.default.svc:3000"
        assert result.label == "grafana"
        assert result.auth_mode == "sso"
        assert result.websocket_enabled is True
        assert result.forward_host is False
        assert result.verify_ssl is False
        assert result.sync_policy == "strict"
        assert result.access_control.allowed_users == []
        assert result.access_control.pin is None
        assert result.access_control.basic_auth is None

    def test_full_spec(self):
        spec = {
            "serviceRef": {
                "name": "argocd",
                "namespace": "argocd",
                "port": 8080,
                "protocol": "https",
            },
            "label": "argocd",
            "authMode": "none",
            "websocketEnabled": False,
            "forwardHost": True,
            "verifySSL": True,
            "syncPolicy": "initial",
            "accessControl": {
                "allowedUsers": [
                    {"email": "admin@co.com", "provider": "google"},
                    {"email": "dev@co.com"},
                ],
                "pin": "9876",
            },
        }
        result = parse_crd_spec(spec)
        assert result.service_url == "https://argocd.argocd.svc:8080"
        assert result.label == "argocd"
        assert result.auth_mode == "none"
        assert result.websocket_enabled is False
        assert result.forward_host is True
        assert result.verify_ssl is True
        assert result.sync_policy == "initial"
        assert len(result.access_control.allowed_users) == 2
        assert result.access_control.allowed_users[0].email == "admin@co.com"
        assert result.access_control.allowed_users[0].provider == "google"
        assert result.access_control.allowed_users[1].provider == "any"
        assert result.access_control.pin == "9876"

    def test_defaults_namespace_to_default(self):
        spec = {
            "serviceRef": {"name": "svc", "port": 80},
            "label": "svc",
        }
        result = parse_crd_spec(spec)
        assert "default" in result.service_url

    def test_label_falls_back_to_service_name(self):
        spec = {
            "serviceRef": {"name": "my-app", "port": 8080},
        }
        result = parse_crd_spec(spec)
        assert result.label == "my-app"


# ---------------------------------------------------------------------------
# parse_ingress_annotations
# ---------------------------------------------------------------------------


class TestParseIngressAnnotations:
    def _make_rules(self, svc_name: str = "web", port: int = 80):
        return [
            {
                "http": {
                    "paths": [
                        {
                            "path": "/",
                            "pathType": "Prefix",
                            "backend": {
                                "service": {
                                    "name": svc_name,
                                    "port": {"number": port},
                                }
                            },
                        }
                    ]
                }
            }
        ]

    def test_minimal_annotations(self):
        result = parse_ingress_annotations({}, self._make_rules())
        assert result is not None
        assert result.label == "web"
        assert result.auth_mode == "sso"
        assert result.sync_policy == "strict"

    def test_full_annotations(self):
        annotations = {
            "hle.world/label": "my-web",
            "hle.world/auth-mode": "none",
            "hle.world/sync-policy": "initial",
            "hle.world/websocket-enabled": "false",
            "hle.world/forward-host": "true",
            "hle.world/verify-ssl": "true",
            "hle.world/pin": "4321",
            "hle.world/allowed-users": "a@b.com:google,c@d.com:github,e@f.com",
        }
        result = parse_ingress_annotations(annotations, self._make_rules())
        assert result is not None
        assert result.label == "my-web"
        assert result.auth_mode == "none"
        assert result.sync_policy == "initial"
        assert result.websocket_enabled is False
        assert result.forward_host is True
        assert result.verify_ssl is True
        assert result.access_control.pin == "4321"
        assert len(result.access_control.allowed_users) == 3
        assert result.access_control.allowed_users[0] == AccessRule("a@b.com", "google")
        assert result.access_control.allowed_users[1] == AccessRule("c@d.com", "github")
        assert result.access_control.allowed_users[2] == AccessRule("e@f.com", "any")

    def test_empty_rules_returns_none(self):
        assert parse_ingress_annotations({}, []) is None

    def test_no_paths_returns_none(self):
        rules = [{"http": {"paths": []}}]
        assert parse_ingress_annotations({}, rules) is None

    def test_no_service_name_returns_none(self):
        rules = [
            {"http": {"paths": [{"backend": {"service": {"name": "", "port": {"number": 80}}}}]}}
        ]
        assert parse_ingress_annotations({}, rules) is None


# ---------------------------------------------------------------------------
# ManagedTunnel
# ---------------------------------------------------------------------------


class TestManagedTunnel:
    def _make_spec(self, **overrides) -> ManagedTunnelSpec:
        defaults = {
            "service_url": "http://grafana.monitoring.svc:3000",
            "label": "grafana",
        }
        defaults.update(overrides)
        return ManagedTunnelSpec(**defaults)

    @pytest.mark.asyncio
    async def test_start_creates_tunnel_task(self):
        spec = self._make_spec()
        tunnel = ManagedTunnel(spec=spec, api_key="hle_test", logger=MagicMock())

        with (
            patch("hle_operator.tunnel_manager.Tunnel") as MockTunnel,
            patch("hle_operator.tunnel_manager.TunnelConfig"),
        ):
            mock_instance = MockTunnel.return_value
            mock_instance.connect = AsyncMock()
            mock_instance.disconnect = AsyncMock()
            mock_instance.public_url = None
            mock_instance.is_connected = False

            await tunnel.start()
            assert tunnel._task is not None
            assert not tunnel._task.done()

            await tunnel.stop()
            assert tunnel._tunnel is None

    @pytest.mark.asyncio
    async def test_managed_by_set_for_strict(self):
        spec = self._make_spec(sync_policy="strict")
        tunnel = ManagedTunnel(spec=spec, api_key="hle_test", logger=MagicMock())

        with (
            patch("hle_operator.tunnel_manager.Tunnel") as MockTunnel,
            patch("hle_operator.tunnel_manager.TunnelConfig") as MockConfig,
        ):
            mock_instance = MockTunnel.return_value
            mock_instance.connect = AsyncMock()
            mock_instance.disconnect = AsyncMock()

            await tunnel.start()

            # Check TunnelConfig was called with managed_by="hle-operator"
            MockConfig.assert_called_once()
            call_kwargs = MockConfig.call_args[1]
            assert call_kwargs["managed_by"] == "hle-operator"

            await tunnel.stop()

    @pytest.mark.asyncio
    async def test_managed_by_none_for_initial(self):
        spec = self._make_spec(sync_policy="initial")
        tunnel = ManagedTunnel(spec=spec, api_key="hle_test", logger=MagicMock())

        with (
            patch("hle_operator.tunnel_manager.Tunnel") as MockTunnel,
            patch("hle_operator.tunnel_manager.TunnelConfig") as MockConfig,
        ):
            mock_instance = MockTunnel.return_value
            mock_instance.connect = AsyncMock()
            mock_instance.disconnect = AsyncMock()

            await tunnel.start()

            MockConfig.assert_called_once()
            call_kwargs = MockConfig.call_args[1]
            assert call_kwargs["managed_by"] is None

            await tunnel.stop()

    @pytest.mark.asyncio
    async def test_wait_connected_timeout(self):
        spec = self._make_spec()
        tunnel = ManagedTunnel(spec=spec, api_key="hle_test", logger=MagicMock())
        result = await tunnel.wait_connected(timeout=0.1)
        assert result is False

    @pytest.mark.asyncio
    async def test_reconcile_skipped_for_initial(self):
        spec = self._make_spec(sync_policy="initial")
        tunnel = ManagedTunnel(spec=spec, api_key="hle_test", logger=MagicMock())
        tunnel._subdomain = "grafana-x7k"

        with patch("hle_operator.tunnel_manager.ApiClient") as MockClient:
            await tunnel.reconcile_access_control()
            # Should not have created a client at all
            MockClient.assert_not_called()

    @pytest.mark.asyncio
    async def test_reconcile_adds_and_removes_rules(self):
        spec = self._make_spec(
            access_control=AccessControlSpec(
                allowed_users=[
                    AccessRule("keep@test.com", "google"),
                    AccessRule("new@test.com", "github"),
                ],
            ),
        )
        tunnel = ManagedTunnel(spec=spec, api_key="hle_test", logger=MagicMock())
        tunnel._subdomain = "grafana-x7k"

        mock_client = AsyncMock()
        mock_client.list_access_rules.return_value = [
            {"allowed_email": "keep@test.com", "provider": "google", "id": 1},
            {"allowed_email": "old@test.com", "provider": "any", "id": 2},
        ]

        with patch("hle_operator.tunnel_manager.ApiClient", return_value=mock_client):
            await tunnel.reconcile_access_control()

        mock_client.add_access_rule.assert_called_once_with("grafana-x7k", "new@test.com", "github")
        mock_client.delete_access_rule.assert_called_once_with("grafana-x7k", 2)

    @pytest.mark.asyncio
    async def test_reconcile_sets_pin(self):
        spec = self._make_spec(
            access_control=AccessControlSpec(pin="5678"),
        )
        tunnel = ManagedTunnel(spec=spec, api_key="hle_test", logger=MagicMock())
        tunnel._subdomain = "grafana-x7k"

        mock_client = AsyncMock()
        mock_client.list_access_rules.return_value = []
        mock_client.get_tunnel_pin_status.return_value = {"enabled": False}
        mock_client.get_tunnel_basic_auth_status.return_value = {"enabled": False}

        with patch("hle_operator.tunnel_manager.ApiClient", return_value=mock_client):
            await tunnel.reconcile_access_control()

        mock_client.set_tunnel_pin.assert_called_once_with("grafana-x7k", "5678")

    @pytest.mark.asyncio
    async def test_reconcile_removes_pin(self):
        spec = self._make_spec()  # no pin
        tunnel = ManagedTunnel(spec=spec, api_key="hle_test", logger=MagicMock())
        tunnel._subdomain = "grafana-x7k"

        mock_client = AsyncMock()
        mock_client.list_access_rules.return_value = []
        mock_client.get_tunnel_pin_status.return_value = {"enabled": True}
        mock_client.get_tunnel_basic_auth_status.return_value = {"enabled": False}

        with patch("hle_operator.tunnel_manager.ApiClient", return_value=mock_client):
            await tunnel.reconcile_access_control()

        mock_client.remove_tunnel_pin.assert_called_once_with("grafana-x7k")
