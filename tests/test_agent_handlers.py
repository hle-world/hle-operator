"""Agent-mode event handlers: declaration, removal and finalizer stripping."""

from __future__ import annotations

import pytest

# Agent mode needs the unreleased P3+P4 hle-client (protocol 1.4). Under an
# older client these modules still collect but skip, so the legacy suite runs.
try:
    from hle_common.agent_protocol import DeclaredAccess  # noqa: F401
except ImportError as exc:  # pragma: no cover
    pytest.skip(f"hle-client P3+P4 required: {exc}", allow_module_level=True)


import kopf

from hle_operator.agent_handlers import handle_crd_event, handle_ingress_event, register
from hle_operator.registry import DeclarationRegistry


class FakeClient:
    def __init__(self) -> None:
        self.sent = []

    async def send_declared_endpoints(self, endpoints, revision):
        self.sent.append((list(endpoints), revision))
        return True


def make_registry() -> tuple[DeclarationRegistry, list]:
    stripped: list[tuple[str, str, list[str], str | None]] = []
    registry = DeclarationRegistry(
        FakeClient(),
        read_secret=lambda ref: "",
        read_basic_auth=lambda ns, name: None,
        strip_finalizer=lambda ns, name, remaining, rv: stripped.append((ns, name, remaining, rv)),
    )
    registry._initial_done = True
    return registry, stripped


def crd_body(**spec) -> dict:
    base = {"serviceRef": {"name": "grafana", "port": 3000}, "label": "grafana"}
    base.update(spec)
    return {"metadata": {"name": "grafana", "namespace": "monitoring"}, "spec": base}


def ingress_body(ingress_class: str = "hle") -> dict:
    return {
        "metadata": {"name": "web", "namespace": "apps"},
        "spec": {
            "ingressClassName": ingress_class,
            "rules": [
                {
                    "http": {
                        "paths": [{"backend": {"service": {"name": "web", "port": {"number": 80}}}}]
                    }
                }
            ],
        },
    }


async def test_crd_added_declares_and_strips_finalizer():
    registry, stripped = make_registry()
    body = crd_body()
    body["metadata"]["finalizers"] = ["hle.world/operator-finalizer"]
    await handle_crd_event(registry, "ADDED", body)
    assert [e.label for e in registry.declared()] == ["grafana"]
    assert stripped == [("monitoring", "grafana", [], None)]


async def test_crd_being_deleted_is_removed_and_unblocked():
    registry, stripped = make_registry()
    await handle_crd_event(registry, "ADDED", crd_body())
    body = crd_body()
    body["metadata"]["finalizers"] = ["hle.world/operator-finalizer"]
    body["metadata"]["deletionTimestamp"] = "2026-10-01T00:00:00Z"
    await handle_crd_event(registry, "MODIFIED", body)
    assert registry.declared() == []
    assert stripped == [("monitoring", "grafana", [], None)]


async def test_crd_deleted_removes():
    registry, _ = make_registry()
    await handle_crd_event(registry, "ADDED", crd_body())
    await handle_crd_event(registry, "DELETED", crd_body())
    assert registry.declared() == []


async def test_crd_invalid_is_error_not_endpoint():
    registry, _ = make_registry()
    await handle_crd_event(registry, "ADDED", crd_body(label="Bad Label"))
    targets = registry.targets()
    assert len(targets) == 1
    assert targets[0].endpoint is None


async def test_ingress_hle_declares():
    registry, _ = make_registry()
    await handle_ingress_event(registry, "ADDED", ingress_body("hle"))
    assert [e.source_ref for e in registry.declared()] == ["ingress:apps/web"]


async def test_ingress_other_class_removes():
    registry, _ = make_registry()
    await handle_ingress_event(registry, "ADDED", ingress_body("hle"))
    await handle_ingress_event(registry, "MODIFIED", ingress_body("nginx"))
    assert registry.declared() == []


async def test_ingress_deleted_removes():
    registry, _ = make_registry()
    await handle_ingress_event(registry, "ADDED", ingress_body("hle"))
    await handle_ingress_event(registry, "DELETED", ingress_body("hle"))
    assert registry.declared() == []


def test_register_adds_only_event_handlers_to_given_registry():
    registry, _ = make_registry()
    kopf_registry = kopf.OperatorRegistry()
    register(registry, kopf_registry)
    plurals = {s.any_name for s in kopf_registry._watching.get_all_selectors()}
    assert plurals == {"hletunnels", "ingresses"}
    # No change-detecting handlers: kopf therefore adds no finalizer.
    assert not kopf_registry._changing.get_all_handlers()
