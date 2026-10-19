"""Security behaviour: namespace tenancy, label ownership, legacy-mode hardening."""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from hle_operator import handlers
from hle_operator.config import allow_cross_namespace
from hle_operator.declared import (
    DeclarationError,
    crd_to_declared,
    normalise_secret_ref,
)
from hle_operator.tunnel_manager import (
    ManagedTunnel,
    ManagedTunnelSpec,
    SpecError,
    TargetRefused,
    build_service_url,
    parse_crd_spec,
)
from tests.test_registry import Harness, crd_body, ingress_body, ready


async def started() -> Harness:
    """A registry past its initial sync, as it is for every real watch event."""
    h = Harness()
    await ready(h)
    return h


def tunnel_body(namespace: str = "team-a", name: str = "x", **spec: Any) -> dict:
    base: dict[str, Any] = {"serviceRef": {"name": "web", "port": 80}, "label": "web"}
    base.update(spec)
    return {"metadata": {"name": name, "namespace": namespace, "uid": f"uid-{name}"}, "spec": base}


# -- cross-namespace serviceRef ----------------------------------------------------


class TestCrossNamespaceServiceRef:
    def body(self) -> dict:
        return tunnel_body(serviceRef={"name": "vault", "namespace": "vault", "port": 8200})

    def test_agent_mode_refuses_another_namespace(self):
        with pytest.raises(DeclarationError, match="cross-namespace"):
            crd_to_declared(self.body())

    def test_agent_mode_allows_own_namespace_spelled_out(self):
        body = tunnel_body(serviceRef={"name": "web", "namespace": "team-a", "port": 80})
        assert crd_to_declared(body).target.namespace == "team-a"

    def test_agent_mode_opt_in_allows_it(self):
        result = crd_to_declared(self.body(), allow_cross_namespace=True)
        assert result.target.namespace == "vault"

    def test_legacy_mode_refuses_another_namespace(self):
        spec = self.body()["spec"]
        with pytest.raises(SpecError, match="cross-namespace"):
            parse_crd_spec(spec, "team-a")

    def test_legacy_mode_defaults_to_the_cr_namespace(self):
        spec = {"serviceRef": {"name": "web", "port": 80}, "label": "web"}
        assert parse_crd_spec(spec, "team-a").service_url == "http://web.team-a.svc:80"

    def test_legacy_mode_opt_in(self):
        spec = self.body()["spec"]
        parsed = parse_crd_spec(spec, "team-a", allow_cross_namespace=True)
        assert parsed.service_url == "http://vault.vault.svc:8200"

    async def test_registry_fails_the_tunnel_and_declares_nothing(self):
        h = await started()
        await h.registry.apply_crd(self.body())
        assert h.registry.declared() == []
        (target,) = h.registry.targets()
        assert "cross-namespace" in (target.error or "")

    async def test_registry_opt_in(self):
        h = await started()
        h.registry._allow_cross_namespace = True
        await h.registry.apply_crd(self.body())
        assert [e.label for e in h.registry.declared()] == ["web"]

    def test_env_flag_defaults_off(self):
        assert allow_cross_namespace({}) is False
        assert allow_cross_namespace({"HLE_ALLOW_CROSS_NAMESPACE": "true"}) is True


# -- apiKeyRef in agent mode ------------------------------------------------------------


async def test_api_key_ref_fails_in_agent_mode():
    body = tunnel_body(apiKeyRef={"name": "team-key", "key": "api-key"})
    with pytest.raises(DeclarationError, match="apiKeyRef is not supported"):
        crd_to_declared(body)
    h = await started()
    await h.registry.apply_crd(body)
    assert h.registry.declared() == []
    (target,) = h.registry.targets()
    assert "apiKeyRef" in (target.error or "")


# -- upstreamBasicAuthSecret namespace ---------------------------------------------------


class TestUpstreamSecretNamespace:
    def test_bare_name_uses_the_cr_namespace(self):
        assert normalise_secret_ref("api#password", "team-a") == "team-a/api#password"

    def test_own_namespace_is_accepted(self):
        assert normalise_secret_ref("team-a/api#password", "team-a") == "team-a/api#password"

    def test_other_namespace_is_refused(self):
        with pytest.raises(DeclarationError, match="differs"):
            normalise_secret_ref("kube-system/bootstrap-token#token", "team-a")

    @pytest.mark.parametrize("ref", ["api", "api#", "#key", "team-a/#key"])
    def test_malformed_is_refused(self, ref: str):
        with pytest.raises(DeclarationError):
            normalise_secret_ref(ref, "team-a")

    async def test_cross_namespace_secret_is_never_read(self):
        h = await started()
        h.secrets["kube-system/bootstrap-token#token"] = "stolen"
        body = tunnel_body(
            namespace="team-a", upstreamBasicAuthSecret="kube-system/bootstrap-token#token"
        )
        await h.registry.apply_crd(body)
        assert h.secret_reads == 0
        assert h.registry.declared() == []
        (target,) = h.registry.targets()
        assert "differs" in (target.error or "")


# -- duplicate labels: first come keeps it ---------------------------------------------


def with_created(body: dict, stamp: str) -> dict:
    body["metadata"]["creationTimestamp"] = stamp
    return body


class TestLabelOwnership:
    async def test_newcomer_that_sorts_first_does_not_take_the_label(self):
        h = await started()
        incumbent = ingress_body(name="grafana", namespace="monitoring")
        incumbent["metadata"]["annotations"] = {"hle.world/label": "grafana"}
        incumbent["metadata"]["uid"] = "uid-ing"
        await h.registry.apply_ingress(incumbent)
        assert [e.source_ref for e in h.registry.declared()] == ["ingress:monitoring/grafana"]

        attacker = crd_body(name="x", namespace="aaa")
        await h.registry.apply_crd(attacker)

        assert [e.source_ref for e in h.registry.declared()] == ["ingress:monitoring/grafana"]
        failed = [t for t in h.registry.targets() if t.endpoint is None]
        assert [t.source_ref for t in failed] == ["hletunnel:aaa/x"]
        assert "already declared by ingress:monitoring/grafana" in (failed[0].error or "")

    async def test_object_edited_to_take_a_served_label_fails(self):
        h = await started()
        await h.registry.apply_crd(crd_body(name="a", namespace="aaa", label="other"))
        await h.registry.apply_crd(crd_body(name="b", namespace="monitoring", label="grafana"))
        # aaa/a is renamed onto grafana; it sorts first but arrived second.
        await h.registry.apply_crd(crd_body(name="a", namespace="aaa", label="grafana"))
        assert [e.source_ref for e in h.registry.declared()] == ["hletunnel:monitoring/b"]

    async def test_holder_keeps_label_after_a_relist(self):
        h = await started()
        incumbent = with_created(crd_body(name="z", namespace="zzz"), "2026-01-01T00:00:00Z")
        newcomer = with_created(crd_body(name="a", namespace="aaa"), "2026-02-01T00:00:00Z")
        h.lister.crds = [incumbent, newcomer]
        await h.registry.try_initial_sync()
        assert [e.source_ref for e in h.registry.declared()] == ["hletunnel:zzz/z"]
        await h.registry.try_initial_sync()
        assert [e.source_ref for e in h.registry.declared()] == ["hletunnel:zzz/z"]

    async def test_cold_start_prefers_the_older_object(self):
        h = await started()
        old = with_created(crd_body(name="z", namespace="zzz"), "2026-01-01T00:00:00Z")
        new = with_created(crd_body(name="a", namespace="aaa"), "2026-03-01T00:00:00Z")
        h.lister.crds = [new, old]
        await h.registry.try_initial_sync()
        assert [e.source_ref for e in h.registry.declared()] == ["hletunnel:zzz/z"]

    async def test_label_is_released_when_the_holder_is_deleted(self):
        h = await started()
        await h.registry.apply_crd(crd_body(name="a", namespace="aaa"))
        await h.registry.apply_crd(crd_body(name="b", namespace="bbb"))
        h.registry.remove("hletunnel:aaa/a")
        assert [e.source_ref for e in h.registry.declared()] == ["hletunnel:bbb/b"]

    async def test_recreated_object_is_a_newcomer(self):
        h = await started()
        first = with_created(crd_body(name="a", namespace="aaa"), "2026-01-01T00:00:00Z")
        await h.registry.apply_crd(first)
        await h.registry.apply_crd(
            with_created(crd_body(name="b", namespace="bbb"), "2026-02-01T00:00:00Z")
        )
        recreated = with_created(crd_body(name="a", namespace="aaa"), "2026-03-01T00:00:00Z")
        recreated["metadata"]["uid"] = "uid-new"
        await h.registry.apply_crd(recreated)
        assert [e.source_ref for e in h.registry.declared()] == ["hletunnel:bbb/b"]

    async def test_same_label_in_different_zones_does_not_conflict(self):
        h = await started()
        await h.registry.apply_crd(crd_body(name="a", namespace="aaa"))
        await h.registry.apply_crd(crd_body(name="b", namespace="bbb", zone="zone.example"))
        assert len(h.registry.declared()) == 2
        assert h.registry.targets()[0].error is None

    async def test_apex_and_label_in_one_zone_are_different_hostnames(self):
        h = await started()
        await h.registry.apply_crd(crd_body(name="a", namespace="aaa", zone="zone.example"))
        await h.registry.apply_crd(
            crd_body(name="b", namespace="bbb", zone="zone.example", apex=True)
        )
        assert len(h.registry.declared()) == 2

    async def test_a_failing_holder_keeps_its_label(self):
        h = await started()
        h.basic[("monitoring", "ba")] = "u:p"
        holder = crd_body(name="a", namespace="monitoring")
        holder["spec"]["accessControl"] = {"basicAuth": {"secretRef": {"name": "ba"}}}
        await h.registry.apply_crd(holder)
        assert len(h.registry.declared()) == 1
        # The Secret disappears: the holder fails, and a newcomer must not slip in.
        h.registry._visitor_cache[("monitoring", "ba")] = None
        h.registry._rebuild("hletunnel:monitoring/a")
        await h.registry.apply_crd(crd_body(name="b", namespace="aaa"))
        assert h.registry.declared() == []
        errors = {t.source_ref: t.error for t in h.registry.targets()}
        assert "already declared by hletunnel:monitoring/a" in (errors["hletunnel:aaa/b"] or "")


# -- legacy mode: URL building and the target guard ------------------------------------


class TestLegacyServiceUrl:
    @pytest.mark.parametrize(
        "name",
        ["169.254.169.254#", "kubernetes.default", "Web", "a/b", "web@evil", "-web", ""],
    )
    def test_service_name_must_be_a_dns_label(self, name: str):
        spec = {"serviceRef": {"name": name, "port": 80}, "label": "x"}
        with pytest.raises(SpecError):
            parse_crd_spec(spec, "default")

    @pytest.mark.parametrize("protocol", ["http://169.254.169.254/#", "ftp", "gopher", ""])
    def test_protocol_must_be_http_or_https(self, protocol: str):
        with pytest.raises(SpecError):
            build_service_url("default", "web", 80, protocol)

    @pytest.mark.parametrize("port", [0, 70000, "80/x", "a b"])
    def test_port_must_be_valid(self, port):
        with pytest.raises(SpecError):
            build_service_url("default", "web", port, "http")

    def test_valid_url(self):
        assert build_service_url("ns", "web", 8080, "https") == "https://web.ns.svc:8080"


def _guard(allowed: bool, reason: str | None = None):
    guard = MagicMock()
    guard.check = AsyncMock(
        return_value=SimpleNamespace(allowed=allowed, reason=reason, url="http://x", address=None)
    )
    return guard


class TestLegacyTargetGuard:
    async def test_refused_target_never_builds_a_tunnel(self):
        spec = ManagedTunnelSpec(service_url="https://kubernetes.default.svc:443", label="k")
        tunnel = ManagedTunnel(spec, "hle_key", MagicMock(), guard=_guard(False, "kubernetes API"))
        with patch("hle_operator.tunnel_manager.Tunnel") as mock_tunnel:
            with pytest.raises(TargetRefused, match="kubernetes API"):
                await tunnel.start()
            mock_tunnel.assert_not_called()
        assert tunnel._task is None

    async def test_allowed_target_starts(self):
        spec = ManagedTunnelSpec(service_url="http://web.ns.svc:80", label="web")
        guard = _guard(True)
        tunnel = ManagedTunnel(spec, "hle_key", MagicMock(), guard=guard)
        with (
            patch("hle_operator.tunnel_manager.Tunnel") as mock_tunnel,
            patch("hle_operator.tunnel_manager.TunnelConfig"),
        ):
            mock_tunnel.return_value.connect = AsyncMock()
            mock_tunnel.return_value.disconnect = AsyncMock()
            await tunnel.start()
            guard.check.assert_awaited_once_with("http://web.ns.svc:80")
            await tunnel.stop()

    async def test_real_guard_refuses_kube_api_and_metadata(self):
        from hle_client.k8s_targets import KubernetesTargetGuard

        guard = KubernetesTargetGuard.from_env({})
        for url in ("https://kubernetes.default.svc:443", "http://169.254.169.254:80"):
            decision = await guard.check(url)
            assert decision.allowed is False


# -- legacy handlers end to end ----------------------------------------------------------


def _patch():
    return SimpleNamespace(status={})


class TestLegacyHandlers:
    async def test_cross_namespace_crd_ends_failed_and_starts_nothing(self):
        spec = {"serviceRef": {"name": "vault", "namespace": "vault", "port": 8200}, "label": "v"}
        patch_obj = _patch()
        with patch("hle_operator.handlers.ManagedTunnel") as mock_tunnel:
            result = await handlers.on_tunnel_create(
                spec=spec, name="x", namespace="team-a", patch=patch_obj, logger=MagicMock()
            )
        assert result == {"phase": "Failed"}
        assert patch_obj.status["phase"] == "Failed"
        assert "cross-namespace" in patch_obj.status["message"]
        mock_tunnel.assert_not_called()

    async def test_unreadable_basic_auth_secret_fails_closed(self):
        spec = {
            "serviceRef": {"name": "web", "port": 80},
            "label": "web",
            "accessControl": {"basicAuth": {"secretRef": {"name": "nope"}}},
        }
        patch_obj = _patch()
        with (
            patch("hle_operator.handlers._read_basic_auth_secret", return_value=None),
            patch("hle_operator.handlers.ManagedTunnel") as mock_tunnel,
        ):
            result = await handlers.on_tunnel_create(
                spec=spec, name="x", namespace="team-a", patch=patch_obj, logger=MagicMock()
            )
        assert result == {"phase": "Failed"}
        assert "basic-auth Secret team-a/nope" in patch_obj.status["message"]
        mock_tunnel.assert_not_called()

    @pytest.mark.parametrize("protocol", ["http://169.254.169.254/#", "gopher"])
    async def test_ingress_protocol_annotation_is_not_a_url(self, protocol: str):
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
        meta = {"annotations": {"hle.world/protocol": protocol}}
        with patch("hle_operator.handlers.ManagedTunnel") as mock_tunnel:
            result = await handlers.on_ingress_create(
                body={}, spec=spec, meta=meta, name="web", namespace="apps", logger=MagicMock()
            )
        assert result is not None
        assert result["phase"] == "Failed"
        mock_tunnel.assert_not_called()

    async def test_ingress_with_bad_backend_name_is_refused(self):
        spec = {
            "ingressClassName": "hle",
            "rules": [
                {
                    "http": {
                        "paths": [
                            {
                                "backend": {
                                    "service": {"name": "169.254.169.254#", "port": {"number": 80}}
                                }
                            }
                        ]
                    }
                }
            ],
        }
        with patch("hle_operator.handlers.ManagedTunnel") as mock_tunnel:
            result = await handlers.on_ingress_create(
                body={},
                spec=spec,
                meta={"annotations": {}},
                name="web",
                namespace="apps",
                logger=MagicMock(),
            )
        assert result is not None and result["phase"] == "Failed"
        mock_tunnel.assert_not_called()
