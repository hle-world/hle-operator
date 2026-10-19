"""Review follow-ups: pinned upstream address, refused updates stop the tunnel, PIN deletion."""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from hle_operator import handlers
from hle_operator.tunnel_manager import ManagedTunnel, ManagedTunnelSpec, TargetRefused
from tests.test_pin_secret import crd_with_pin_secret
from tests.test_registry import Harness, ready


class ApiError(Exception):
    def __init__(self, status: int) -> None:
        self.status = status


class MovingGuard:
    """A guard whose resolver answers differently each time (DNS rebinding)."""

    def __init__(self, *answers: str | None) -> None:
        self.answers = list(answers)
        self.calls = 0

    async def check(self, service_url: str):
        self.calls += 1
        answer = self.answers[min(self.calls - 1, len(self.answers) - 1)]
        if answer is None:
            return SimpleNamespace(allowed=False, reason="kubernetes API", url=None, address=None)
        return SimpleNamespace(
            allowed=True,
            reason=None,
            url="http://web.ns.svc.cluster.local.:8080",
            address=answer,
        )


def make_tunnel(guard) -> ManagedTunnel:
    spec = ManagedTunnelSpec(service_url="http://web.ns.svc:8080", label="web")
    return ManagedTunnel(spec, "hle_key", MagicMock(), guard=guard)


def patched_client():
    mock_tunnel = patch("hle_operator.tunnel_manager.Tunnel")
    mock_config = patch("hle_operator.tunnel_manager.TunnelConfig")
    return mock_tunnel, mock_config


class TestPinnedUpstream:
    async def test_dials_the_approved_address_not_the_name(self):
        guard = MovingGuard("10.0.0.7")
        tunnel = make_tunnel(guard)
        mt, mc = patched_client()
        with mt as tunnel_cls, mc as config_cls:
            tunnel_cls.return_value.connect = AsyncMock()
            tunnel_cls.return_value.disconnect = AsyncMock()
            await tunnel.start()
            kwargs = config_cls.call_args.kwargs
            assert kwargs["service_url"] == "http://10.0.0.7:8080"
            assert kwargs["upstream_host"] == "web.ns.svc:8080"
            assert kwargs["trust_env"] is False
            await tunnel.stop()
        assert guard.calls == 1

    async def test_ipv6_address_is_bracketed(self):
        tunnel = make_tunnel(MovingGuard("fd00::7"))
        mt, mc = patched_client()
        with mt as tunnel_cls, mc as config_cls:
            tunnel_cls.return_value.connect = AsyncMock()
            tunnel_cls.return_value.disconnect = AsyncMock()
            await tunnel.start()
            assert config_cls.call_args.kwargs["service_url"] == "http://[fd00::7]:8080"
            await tunnel.stop()

    async def test_recheck_redials_when_the_address_moves(self):
        guard = MovingGuard("10.0.0.7", "10.0.0.8")
        tunnel = make_tunnel(guard)
        mt, mc = patched_client()
        with mt as tunnel_cls, mc as config_cls:
            tunnel_cls.return_value.connect = AsyncMock()
            tunnel_cls.return_value.disconnect = AsyncMock()
            await tunnel.start()
            await tunnel.recheck_target()
            urls = [c.kwargs["service_url"] for c in config_cls.call_args_list]
            assert urls == ["http://10.0.0.7:8080", "http://10.0.0.8:8080"]
            await tunnel.stop()

    async def test_recheck_leaves_an_unchanged_tunnel_alone(self):
        tunnel = make_tunnel(MovingGuard("10.0.0.7"))
        mt, mc = patched_client()
        with mt as tunnel_cls, mc as config_cls:
            tunnel_cls.return_value.connect = AsyncMock()
            tunnel_cls.return_value.disconnect = AsyncMock()
            await tunnel.start()
            await tunnel.recheck_target()
            assert config_cls.call_count == 1
            await tunnel.stop()

    async def test_recheck_stops_the_tunnel_when_the_target_becomes_refused(self):
        # The name now resolves somewhere forbidden (rebinding after approval).
        tunnel = make_tunnel(MovingGuard("10.0.0.7", None))
        mt, mc = patched_client()
        with mt as tunnel_cls, mc:
            tunnel_cls.return_value.connect = AsyncMock()
            tunnel_cls.return_value.disconnect = AsyncMock()
            await tunnel.start()
            with pytest.raises(TargetRefused):
                await tunnel.recheck_target()
            assert tunnel._tunnel is None
            assert tunnel._task is None


class TestRefusedUpdateStopsTunnel:
    async def test_access_only_update_that_is_refused_stops_the_running_tunnel(self):
        running = MagicMock()
        running.is_connected = True
        running.stop = AsyncMock()
        handlers._tunnels["team-a/x"] = running
        spec = {
            "serviceRef": {"name": "web", "port": 80},
            "label": "web",
            "accessControl": {"basicAuth": {"secretRef": {"name": "gone"}}},
        }
        diff = [("change", ("spec", "accessControl", "basicAuth"), None, {})]
        patch_obj = SimpleNamespace(status={})
        try:
            with patch("hle_operator.handlers._read_basic_auth_secret", return_value=None):
                result = await handlers.on_tunnel_update(
                    spec=spec,
                    old=None,
                    new=None,
                    diff=diff,
                    name="x",
                    namespace="team-a",
                    patch=patch_obj,
                    logger=MagicMock(),
                )
            assert result == {"phase": "Failed"}
            assert patch_obj.status["phase"] == "Failed"
            running.stop.assert_awaited_once()
            assert "team-a/x" not in handlers._tunnels
        finally:
            handlers._tunnels.pop("team-a/x", None)


class TestDeletedPinSecret:
    async def started_with_pin(self) -> Harness:
        h = Harness()
        await ready(h)
        h.secrets["monitoring/pin-secret#pin"] = "48213"
        await h.registry.apply_crd(crd_with_pin_secret())
        assert h.registry.declared()[0].access.pin == "48213"
        return h

    async def test_deleted_secret_fails_the_tunnel_closed(self):
        h = await self.started_with_pin()
        del h.secrets["monitoring/pin-secret#pin"]  # read_secret raises KeyError
        assert await h.registry.poll_secrets_once() is True
        assert h.registry.declared() == []
        assert h.registry._pin_cache["monitoring/pin-secret#pin"] is None
        (target,) = h.registry.targets()
        assert "pin Secret" in (target.error or "")

    async def test_not_found_api_error_fails_closed(self):
        h = await self.started_with_pin()

        def gone(ref: str) -> str:
            raise ApiError(404)

        h.registry._read_secret = gone
        assert await h.registry.poll_secrets_once() is True
        assert h.registry.declared() == []

    async def test_transient_error_keeps_the_last_pin(self):
        h = await self.started_with_pin()

        def flaky(ref: str) -> str:
            raise ApiError(503)

        h.registry._read_secret = flaky
        assert await h.registry.poll_secrets_once() is False
        assert h.registry.declared()[0].access.pin == "48213"

    async def test_recreated_secret_recovers(self):
        h = await self.started_with_pin()
        del h.secrets["monitoring/pin-secret#pin"]
        await h.registry.poll_secrets_once()
        h.secrets["monitoring/pin-secret#pin"] = "11111"
        assert await h.registry.poll_secrets_once() is True
        assert h.registry.declared()[0].access.pin == "11111"
