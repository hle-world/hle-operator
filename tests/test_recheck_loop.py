"""The periodic target recheck lives in ManagedTunnel, so HLETunnels and Ingresses share it."""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from unittest.mock import AsyncMock, MagicMock, patch

from hle_operator import handlers
from hle_operator.tunnel_manager import ManagedTunnel, ManagedTunnelSpec
from tests.test_review_fixes import MovingGuard

INTERVAL = 0.01


async def settle(condition: Callable[[], bool], timeout: float = 2.0) -> None:
    async with asyncio.timeout(timeout):
        while not condition():  # noqa: ASYNC110 — polling a plain predicate
            await asyncio.sleep(0.005)


def started_client():
    tunnel_cls = patch("hle_operator.tunnel_manager.Tunnel")
    config_cls = patch("hle_operator.tunnel_manager.TunnelConfig")
    return tunnel_cls, config_cls


def build(guard, on_refused) -> ManagedTunnel:
    spec = ManagedTunnelSpec(service_url="http://web.ns.svc:8080", label="web")
    return ManagedTunnel(
        spec, "hle_key", MagicMock(), guard=guard, on_refused=on_refused, recheck_interval=INTERVAL
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
        handlers._ingress_tunnels.clear()
