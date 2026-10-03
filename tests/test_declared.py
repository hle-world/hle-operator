"""Mapping tests for CRD/Ingress -> DeclaredEndpoint."""

from __future__ import annotations

import pytest

# Agent mode needs the unreleased P3+P4 hle-client (protocol 1.4). Under an
# older client these modules still collect but skip, so the legacy suite runs.
try:
    from hle_common.agent_protocol import DeclaredAccess  # noqa: F401
except ImportError as exc:  # pragma: no cover
    pytest.skip(f"hle-client P3+P4 required: {exc}", allow_module_level=True)


from hle_operator.declared import (
    DeclarationError,
    crd_source_ref,
    crd_to_declared,
    crd_visitor_secret_ref,
    ingress_source_ref,
    ingress_to_declared,
    ingress_visitor_secret_ref,
    normalise_label,
)


def crd(name: str = "grafana", namespace: str = "monitoring", **spec) -> dict:
    base = {"serviceRef": {"name": "grafana", "port": 3000}, "label": "grafana"}
    base.update(spec)
    return {"metadata": {"name": name, "namespace": namespace}, "spec": base}


def ingress(name: str = "web", namespace: str = "apps", **spec) -> dict:
    body = {
        "metadata": {"name": name, "namespace": namespace},
        "spec": {
            "ingressClassName": "hle",
            "rules": [
                {
                    "http": {
                        "paths": [
                            {"backend": {"service": {"name": "web", "port": {"number": 8080}}}}
                        ]
                    }
                }
            ],
        },
    }
    body["spec"].update(spec)
    return body


class TestLabel:
    def test_lowercases(self):
        assert normalise_label("My-Web") == "my-web"

    @pytest.mark.parametrize("bad", ["", "-lead", "trail-", "has space", "a" * 64])
    def test_rejects(self, bad):
        with pytest.raises(DeclarationError):
            normalise_label(bad)


class TestCrdToDeclared:
    def test_minimal(self):
        result = crd_to_declared(crd())
        assert result.label == "grafana"
        assert result.source_ref == "hletunnel:monitoring/grafana"
        assert result.target is not None
        assert result.target.namespace == "monitoring"
        assert result.target.name == "grafana"
        assert result.target.port == 3000
        assert result.target.scheme == "http"
        assert result.sync_policy == "strict"
        assert result.access is None

    def test_explicit_target_and_upstream_secret(self):
        body = crd(
            serviceRef={
                "name": "api",
                "namespace": "backend",
                "port": 8443,
                "protocol": "https",
            },
            upstreamBasicAuthSecret="secrets/api#password",
        )
        result = crd_to_declared(body)
        assert result.target is not None
        assert result.target.namespace == "backend"
        assert result.target.scheme == "https"
        assert result.upstream_basic_auth_secret == "secrets/api#password"

    def test_access_control(self):
        body = crd(
            syncPolicy="initial",
            accessControl={
                "allowedUsers": [
                    {"email": "a@x.com", "provider": "google"},
                    {"email": "b@x.com"},
                ],
                "pin": "1234",
            },
        )
        result = crd_to_declared(body, basic_auth="user:pass")
        assert result.sync_policy == "initial"
        assert result.access is not None
        assert [u.email for u in result.access.allowed_users] == ["a@x.com", "b@x.com"]
        assert result.access.allowed_users[1].provider == "any"
        assert result.access.pin == "1234"
        assert result.access.basic_auth == "user:pass"

    def test_invalid_label(self):
        with pytest.raises(DeclarationError):
            crd_to_declared(crd(label="Bad Label"))

    def test_zone_defaults_to_base_domain(self):
        result = crd_to_declared(crd())
        assert result.zone is None
        assert result.apex is False

    def test_zone_and_apex(self):
        result = crd_to_declared(crd(zone="T00t.us.", apex=True))
        assert result.zone == "t00t.us"
        assert result.apex is True

    def test_apex_requires_zone(self):
        with pytest.raises(DeclarationError, match="apex requires zone"):
            crd_to_declared(crd(apex=True))

    def test_missing_service_name(self):
        with pytest.raises(DeclarationError):
            crd_to_declared(crd(serviceRef={"port": 80}))

    def test_visitor_secret_ref(self):
        body = crd(accessControl={"basicAuth": {"secretRef": {"name": "ba"}}})
        assert crd_visitor_secret_ref(body) == ("monitoring", "ba")
        assert crd_visitor_secret_ref(crd()) is None

    def test_source_ref_defaults_namespace(self):
        body = crd()
        body["metadata"].pop("namespace")
        assert crd_source_ref(body) == "hletunnel:default/grafana"


class TestIngressToDeclared:
    def test_zone_annotation(self):
        body = ingress()
        body["metadata"]["annotations"] = {"hle.world/zone": "t00t.us"}
        result = ingress_to_declared(body)
        assert result.zone == "t00t.us"
        assert result.apex is False

    def test_minimal(self):
        result = ingress_to_declared(ingress())
        assert result.label == "web"
        assert result.source_ref == "ingress:apps/web"
        assert result.target is not None
        assert result.target.namespace == "apps"
        assert result.target.name == "web"
        assert result.target.port == 8080

    def test_annotations(self):
        body = ingress()
        body["metadata"]["annotations"] = {
            "hle.world/label": "My-Web",
            "hle.world/protocol": "https",
            "hle.world/auth-mode": "none",
            "hle.world/sync-policy": "initial",
            "hle.world/websocket-enabled": "false",
            "hle.world/forward-host": "true",
            "hle.world/verify-ssl": "true",
            "hle.world/allowed-users": "a@b.com:google,c@d.com",
            "hle.world/pin": "9999",
        }
        result = ingress_to_declared(body, basic_auth="u:p")
        assert result.label == "my-web"
        assert result.target is not None
        assert result.target.scheme == "https"
        assert result.auth_mode == "none"
        assert result.sync_policy == "initial"
        assert result.websocket_enabled is False
        assert result.forward_host is True
        assert result.verify_ssl is True
        assert result.access is not None
        assert [u.provider for u in result.access.allowed_users] == ["google", "any"]
        assert result.access.pin == "9999"
        assert result.access.basic_auth == "u:p"

    def test_named_port(self):
        body = ingress()
        body["spec"]["rules"][0]["http"]["paths"][0]["backend"]["service"]["port"] = {
            "name": "http"
        }
        result = ingress_to_declared(body)
        assert result.target is not None
        assert result.target.port == "http"

    def test_no_rules(self):
        body = ingress()
        body["spec"]["rules"] = []
        with pytest.raises(DeclarationError):
            ingress_to_declared(body)

    def test_visitor_secret_ref(self):
        body = ingress()
        body["metadata"]["annotations"] = {"hle.world/basic-auth-secret": "ba"}
        assert ingress_visitor_secret_ref(body) == ("apps", "ba")
        assert ingress_visitor_secret_ref(ingress()) is None

    def test_source_ref_defaults_namespace(self):
        body = ingress()
        body["metadata"].pop("namespace")
        assert ingress_source_ref(body) == "ingress:default/web"
