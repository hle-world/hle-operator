"""The periodic target recheck lives in ManagedTunnel, so HLETunnels and Ingresses share it."""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import kopf
import pytest

from hle_operator import handlers
from hle_operator import tunnel_manager as tm
from hle_operator.tunnel_manager import (
    TRANSIENT_REFUSAL_LIMIT,
    ManagedTunnel,
    ManagedTunnelSpec,
    TargetRefused,
    is_transient_refusal,
)
from tests.test_review_fixes import MovingGuard

INTERVAL = 0.01
TRANSIENT_REASON = (
    "target not allowed on kubernetes agents: cannot resolve web.ns.svc.cluster.local"
)


async def settle(condition: Callable[[], bool], timeout: float = 2.0) -> None:
    async with asyncio.timeout(timeout):
        while not condition():  # noqa: ASYNC110 — polling a plain predicate
            await asyncio.sleep(0.005)


def started_client():
    tunnel_cls = patch("hle_operator.tunnel_manager.Tunnel")
    config_cls = patch("hle_operator.tunnel_manager.TunnelConfig")
    return tunnel_cls, config_cls


class FlakyGuard:
    """A guard whose verdict can be flipped mid-test."""

    def __init__(self) -> None:
        self.allowed = True
        self.reason: str | None = None
        self.calls = 0

    def allow(self) -> None:
        self.allowed = True
        self.reason = None

    def refuse(self, reason: str) -> None:
        self.allowed = False
        self.reason = reason

    async def check(self, service_url: str):
        self.calls += 1
        if self.allowed:
            return SimpleNamespace(
                allowed=True,
                reason=None,
                url="http://web.ns.svc.cluster.local.:8080",
                address="10.0.0.7",
            )
        return SimpleNamespace(allowed=False, reason=self.reason, url=None, address=None)


def build(guard, on_refused, on_recovered=None) -> ManagedTunnel:
    spec = ManagedTunnelSpec(service_url="http://web.ns.svc:8080", label="web")
    return ManagedTunnel(
        spec,
        "hle_key",
        MagicMock(),
        guard=guard,
        on_refused=on_refused,
        on_recovered=on_recovered,
        recheck_interval=INTERVAL,
    )


class TestRecheckLoop:
    async def test_moved_address_is_redialled_without_any_handler(self):
        tunnel = build(MovingGuard("10.0.0.7", "10.0.0.8"), None)
        tunnel_cls, config_cls = started_client()
        with tunnel_cls as mock_tunnel, config_cls as mock_config:
            mock_tunnel.return_value.connect = AsyncMock()
            mock_tunnel.return_value.disconnect = AsyncMock()
            await tunnel.start()
            await settle(lambda: mock_config.call_count >= 2)
            urls = [c.kwargs["service_url"] for c in mock_config.call_args_list]
            assert urls[:2] == ["http://10.0.0.7:8080", "http://10.0.0.8:8080"]
            await tunnel.stop()

    async def test_refusal_stops_the_tunnel_and_calls_back_once(self):
        calls: list[tuple[ManagedTunnel, str]] = []

        async def refused(tunnel: ManagedTunnel, message: str) -> None:
            calls.append((tunnel, message))

        tunnel = build(MovingGuard("10.0.0.7", None), refused)
        tunnel_cls, config_cls = started_client()
        with tunnel_cls as mock_tunnel, config_cls:
            mock_tunnel.return_value.connect = AsyncMock()
            mock_tunnel.return_value.disconnect = AsyncMock()
            await tunnel.start()
            await settle(lambda: bool(calls))
            await asyncio.sleep(INTERVAL * 5)
            assert len(calls) == 1
            assert calls[0][0] is tunnel
            assert "kubernetes API" in calls[0][1]
            assert tunnel._tunnel is None
            await tunnel.stop()

    async def test_stop_cancels_the_loop(self):
        guard = MovingGuard("10.0.0.7")
        tunnel = build(guard, None)
        tunnel_cls, config_cls = started_client()
        with tunnel_cls as mock_tunnel, config_cls:
            mock_tunnel.return_value.connect = AsyncMock()
            mock_tunnel.return_value.disconnect = AsyncMock()
            await tunnel.start()
            await settle(lambda: guard.calls >= 3)
            await tunnel.stop()
            calls = guard.calls
            await asyncio.sleep(INTERVAL * 5)
            assert guard.calls == calls
            assert tunnel._recheck_task is None

    async def test_a_guard_error_does_not_end_the_loop(self):
        guard = MovingGuard("10.0.0.7")
        real_check = guard.check
        failures = {"n": 0}

        async def flaky(url: str):
            if guard.calls == 1 and failures["n"] == 0:
                failures["n"] += 1
                raise OSError("dns blip")
            return await real_check(url)

        guard.check = flaky  # type: ignore[method-assign]
        tunnel = build(guard, None)
        tunnel_cls, config_cls = started_client()
        with tunnel_cls as mock_tunnel, config_cls:
            mock_tunnel.return_value.connect = AsyncMock()
            mock_tunnel.return_value.disconnect = AsyncMock()
            await tunnel.start()
            await settle(lambda: failures["n"] == 1 and guard.calls >= 3)
            assert tunnel._recheck_task is not None and not tunnel._recheck_task.done()
            await tunnel.stop()


class TestTransientRefusal:
    def test_is_transient_refusal_only_matches_unresolved_names(self):
        assert is_transient_refusal("target refused: cannot resolve web.ns.svc.cluster.local")
        assert not is_transient_refusal("target refused: kubernetes API")
        assert not is_transient_refusal("target refused: namespace team-a is excluded")
        assert not is_transient_refusal("target refused: cross-namespace reference")

    async def test_transient_refusals_below_the_limit_keep_the_tunnel(self):
        guard = FlakyGuard()
        refused: list[str] = []

        async def on_refused(tunnel: ManagedTunnel, message: str) -> None:
            refused.append(message)

        tunnel = build(guard, on_refused)
        tunnel_cls, config_cls = started_client()
        with tunnel_cls as mock_tunnel, config_cls:
            mock_tunnel.return_value.connect = AsyncMock()
            mock_tunnel.return_value.disconnect = AsyncMock()
            await tunnel._start_connection()
            guard.refuse(TRANSIENT_REASON)
            await tunnel.recheck_target()
            await tunnel.recheck_target()
            assert tunnel._tunnel is not None
            assert tunnel._transient_failures == TRANSIENT_REFUSAL_LIMIT - 1
            assert refused == []
            await tunnel.stop()

    async def test_three_transient_refusals_in_a_row_stop_and_report(self):
        guard = FlakyGuard()
        refused: list[str] = []

        async def on_refused(tunnel: ManagedTunnel, message: str) -> None:
            refused.append(message)

        tunnel = build(guard, on_refused)
        tunnel_cls, config_cls = started_client()
        with tunnel_cls as mock_tunnel, config_cls:
            mock_tunnel.return_value.connect = AsyncMock()
            mock_tunnel.return_value.disconnect = AsyncMock()
            await tunnel.start()
            guard.refuse(TRANSIENT_REASON)
            await settle(lambda: bool(refused))
            assert tunnel._tunnel is None
            assert "cannot resolve" in refused[0]
            await tunnel.stop()

    async def test_an_allowed_recheck_resets_the_failure_count(self):
        guard = FlakyGuard()

        async def on_refused(tunnel: ManagedTunnel, message: str) -> None:
            return None

        tunnel = build(guard, on_refused)
        tunnel_cls, config_cls = started_client()
        with tunnel_cls as mock_tunnel, config_cls:
            mock_tunnel.return_value.connect = AsyncMock()
            mock_tunnel.return_value.disconnect = AsyncMock()
            await tunnel._start_connection()
            guard.refuse(TRANSIENT_REASON)
            await tunnel.recheck_target()
            assert tunnel._transient_failures == 1
            guard.allow()
            await tunnel.recheck_target()
            assert tunnel._transient_failures == 0
            guard.refuse(TRANSIENT_REASON)
            await tunnel.recheck_target()
            assert tunnel._tunnel is not None
            assert tunnel._transient_failures == 1
            await tunnel.stop()

    async def test_a_permanent_refusal_stops_immediately(self):
        guard = FlakyGuard()

        async def on_refused(tunnel: ManagedTunnel, message: str) -> None:
            return None

        tunnel = build(guard, on_refused)
        tunnel_cls, config_cls = started_client()
        with tunnel_cls as mock_tunnel, config_cls:
            mock_tunnel.return_value.connect = AsyncMock()
            mock_tunnel.return_value.disconnect = AsyncMock()
            await tunnel._start_connection()
            guard.refuse("kubernetes API")
            with pytest.raises(TargetRefused):
                await tunnel.recheck_target()
            assert tunnel._tunnel is None

    async def test_a_refused_tunnel_recovers_when_the_target_returns(self):
        guard = FlakyGuard()
        refused: list[str] = []
        recovered: list[ManagedTunnel] = []

        async def on_refused(tunnel: ManagedTunnel, message: str) -> None:
            refused.append(message)

        async def on_recovered(tunnel: ManagedTunnel) -> None:
            recovered.append(tunnel)

        tunnel = build(guard, on_refused, on_recovered)
        tunnel_cls, config_cls = started_client()
        with (
            tunnel_cls as mock_tunnel,
            config_cls as mock_config,
            patch.object(tm, "TRANSIENT_RECOVERY_DELAYS", (INTERVAL,)),
            patch.object(tm, "PERMANENT_RECOVERY_DELAY", INTERVAL),
        ):
            mock_tunnel.return_value.connect = AsyncMock()
            mock_tunnel.return_value.disconnect = AsyncMock()
            await tunnel.start()
            guard.refuse(TRANSIENT_REASON)
            await settle(lambda: bool(refused))
            guard.allow()
            await settle(lambda: bool(recovered))
            assert recovered[0] is tunnel
            assert tunnel._tunnel is not None
            assert mock_config.call_count >= 2
            await tunnel.stop()


class TestHandlerReporting:
    async def test_crd_tunnel_refusal_drops_it_and_marks_the_cr_failed(self):
        patched: list[tuple] = []
        key = "team-a/x"
        holder: dict[str, ManagedTunnel] = {}
        callback = handlers._crd_refused("team-a", "x", key)
        tunnel = build(MovingGuard("10.0.0.7", None), callback)
        holder["t"] = tunnel
        handlers._tunnels[key] = tunnel
        tunnel_cls, config_cls = started_client()
        try:
            with (
                tunnel_cls as mock_tunnel,
                config_cls,
                patch(
                    "hle_operator.handlers._patch_crd_status",
                    side_effect=lambda ns, name, status: patched.append((ns, name, status)),
                ),
            ):
                mock_tunnel.return_value.connect = AsyncMock()
                mock_tunnel.return_value.disconnect = AsyncMock()
                await tunnel.start()
                await settle(lambda: bool(patched))
            assert key not in handlers._tunnels
            ns, name, status = patched[0]
            assert (ns, name, status["phase"]) == ("team-a", "x", "Failed")
            assert "target refused" in status["message"]
        finally:
            handlers._tunnels.pop(key, None)
            await tunnel.stop()

    async def test_ingress_tunnel_refusal_drops_it_and_posts_an_event(self):
        key = "apps/web"
        body = {"metadata": {"name": "web", "namespace": "apps"}}
        callback = handlers._ingress_refused(body, key)
        tunnel = build(MovingGuard("10.0.0.7", None), callback)
        handlers._ingress_tunnels[key] = tunnel
        tunnel_cls, config_cls = started_client()
        try:
            with (
                tunnel_cls as mock_tunnel,
                config_cls,
                patch("hle_operator.handlers.kopf.warn") as warn,
            ):
                mock_tunnel.return_value.connect = AsyncMock()
                mock_tunnel.return_value.disconnect = AsyncMock()
                await tunnel.start()
                await settle(lambda: warn.called)
            assert key not in handlers._ingress_tunnels
            assert warn.call_args.kwargs["reason"] == "TargetRefused"
        finally:
            handlers._ingress_tunnels.pop(key, None)
            await tunnel.stop()

    async def test_replaced_tunnel_is_not_popped_by_a_stale_report(self):
        key = "apps/web"
        callback = handlers._ingress_refused({}, key)
        old, new = MagicMock(), MagicMock()
        handlers._ingress_tunnels[key] = new
        try:
            with patch("hle_operator.handlers.kopf.warn"):
                await callback(old, "target refused: x")
            assert handlers._ingress_tunnels[key] is new
        finally:
            handlers._ingress_tunnels.pop(key, None)

    async def test_crd_recovery_reinserts_and_marks_it_connected(self):
        key = "team-a/x"
        patched: list[tuple] = []
        callback = handlers._crd_recovered("team-a", "x", key)
        tunnel = MagicMock()
        tunnel.wait_connected = AsyncMock(return_value=True)
        tunnel.public_url = "https://x.hle.world"
        tunnel.subdomain = "x-abc"
        handlers._tunnels.pop(key, None)
        try:
            with patch(
                "hle_operator.handlers._patch_crd_status",
                side_effect=lambda ns, name, status: patched.append((ns, name, status)),
            ):
                await callback(tunnel)
            assert handlers._tunnels[key] is tunnel
            assert patched[0][2]["phase"] == "Connected"
        finally:
            handlers._tunnels.pop(key, None)

    async def test_ingress_recovery_reinserts_and_posts_a_normal_event(self):
        key = "apps/web"
        body = {"metadata": {"name": "web", "namespace": "apps"}}
        callback = handlers._ingress_recovered(body, key)
        tunnel = MagicMock()
        handlers._ingress_tunnels.pop(key, None)
        try:
            with patch("hle_operator.handlers.kopf.event") as event:
                await callback(tunnel)
            assert handlers._ingress_tunnels[key] is tunnel
            assert event.call_args.kwargs["type"] == "Normal"
            assert event.call_args.kwargs["reason"] == "TargetAllowed"
        finally:
            handlers._ingress_tunnels.pop(key, None)

    async def test_ingress_and_crd_handlers_pass_the_callback(self):
        spec = {
            "ingressClassName": "hle",
            "rules": [
                {
                    "http": {
                        "paths": [{"backend": {"service": {"name": "web", "port": {"number": 80}}}}]
                    }
                }
            ],
        }
        with (
            patch("hle_operator.handlers.ManagedTunnel") as mock_tunnel,
            patch("hle_operator.handlers.get_api_key", return_value="hle_key"),
        ):
            mock_tunnel.return_value.start = AsyncMock()
            mock_tunnel.return_value.wait_connected = AsyncMock(return_value=False)
            await handlers.on_ingress_create(
                body={"metadata": {"name": "web"}},
                spec=spec,
                meta={"annotations": {}},
                name="web",
                namespace="apps",
                logger=MagicMock(),
            )
        assert callable(mock_tunnel.call_args.kwargs["on_refused"])
        assert callable(mock_tunnel.call_args.kwargs["on_recovered"])
        handlers._ingress_tunnels.clear()


class TestCreateDuringDnsOutage:
    def _spec(self) -> dict:
        return {"serviceRef": {"name": "web", "port": 80}, "label": "web"}

    async def test_create_retries_a_transient_dns_refusal(self):
        patch_obj = SimpleNamespace(status={})
        refusal = TargetRefused(
            "target refused: target not allowed on kubernetes agents: "
            "cannot resolve web.team-a.svc.cluster.local"
        )
        with (
            patch("hle_operator.handlers.ManagedTunnel") as mock_tunnel,
            patch("hle_operator.handlers.get_api_key", return_value="hle_key"),
        ):
            mock_tunnel.return_value.start = AsyncMock(side_effect=refusal)
            with pytest.raises(kopf.TemporaryError) as err:
                await handlers.on_tunnel_create(
                    spec=self._spec(),
                    name="x",
                    namespace="team-a",
                    patch=patch_obj,
                    logger=MagicMock(),
                )
        assert err.value.delay == 30
        assert patch_obj.status["phase"] == "Pending"
        assert patch_obj.status["message"].startswith("waiting for DNS")
        assert "team-a/x" not in handlers._tunnels

    async def test_create_fails_permanently_on_a_policy_refusal(self):
        patch_obj = SimpleNamespace(status={})
        refusal = TargetRefused("target refused: kubernetes API")
        with (
            patch("hle_operator.handlers.ManagedTunnel") as mock_tunnel,
            patch("hle_operator.handlers.get_api_key", return_value="hle_key"),
        ):
            mock_tunnel.return_value.start = AsyncMock(side_effect=refusal)
            result = await handlers.on_tunnel_create(
                spec=self._spec(),
                name="x",
                namespace="team-a",
                patch=patch_obj,
                logger=MagicMock(),
            )
        assert result == {"phase": "Failed"}
        assert patch_obj.status["phase"] == "Failed"
        assert "team-a/x" not in handlers._tunnels

