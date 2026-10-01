"""Status phase matrix, diff-only patching, retry backoff and pruning."""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

# Agent mode needs the unreleased P3+P4 hle-client (protocol 1.4). Under an
# older client these modules still collect but skip, so the legacy suite runs.
try:
    from hle_common.agent_protocol import DeclaredAccess  # noqa: F401
except ImportError as exc:  # pragma: no cover
    pytest.skip(f"hle-client P3+P4 required: {exc}", allow_module_level=True)

from hle_common.agent_protocol import DeclaredAck, DeclaredAckEntry, EndpointStatus

from hle_operator.registry import DeclarationRegistry
from hle_operator.status import KubernetesStatusPatcher, StatusPublisher

CRD = {
    "metadata": {"name": "grafana", "namespace": "monitoring", "uid": "u1"},
    "spec": {"serviceRef": {"name": "grafana", "port": 3000}, "label": "grafana"},
}
INGRESS = {
    "metadata": {"name": "web", "namespace": "apps"},
    "spec": {
        "ingressClassName": "hle",
        "rules": [
            {"http": {"paths": [{"backend": {"service": {"name": "web", "port": {"number": 80}}}}]}}
        ],
    },
}


class FakeClient:
    def __init__(self) -> None:
        self.statuses: list[EndpointStatus] | None = []

    def endpoint_statuses(self) -> list[EndpointStatus] | None:
        return self.statuses

    async def send_declared_endpoints(self, endpoints, revision):
        return True


class ApiError(Exception):
    def __init__(self, status: int) -> None:
        super().__init__(f"HTTP {status}")
        self.status = status


class RecordingPatcher:
    def __init__(self) -> None:
        self.calls: list[tuple[str, str, str, dict[str, str]]] = []
        self.fail: Exception | None = None

    def patch_cr(self, namespace: str, name: str, status: dict[str, str]) -> None:
        self.calls.append(("cr", namespace, name, status))
        if self.fail:
            raise self.fail

    def patch_ingress(self, namespace: str, name: str, status: dict[str, str]) -> None:
        self.calls.append(("ingress", namespace, name, status))
        if self.fail:
            raise self.fail


class Clock:
    def __init__(self) -> None:
        self.now = 0.0

    def __call__(self) -> float:
        return self.now


async def build(
    body: dict = CRD,
) -> tuple[StatusPublisher, FakeClient, DeclarationRegistry, RecordingPatcher, Clock]:
    client = FakeClient()
    registry = DeclarationRegistry(client, read_secret=lambda ref: "")
    if body is INGRESS:
        await registry.apply_ingress(body, schedule=False)
    else:
        await registry.apply_crd(body, schedule=False)
    patcher = RecordingPatcher()
    clock = Clock()
    publisher = StatusPublisher(client, registry, patcher=patcher, clock=clock)
    return publisher, client, registry, patcher, clock


def phase(patcher: RecordingPatcher) -> dict[str, str]:
    return patcher.calls[-1][3]


async def test_status_none_skips_cycle():
    publisher, client, _, patcher, _ = await build()
    client.statuses = None
    await publisher.publish_once()
    assert patcher.calls == []


async def test_no_ack_is_pending_waiting_for_channel():
    publisher, _, _, patcher, _ = await build()
    await publisher.publish_once()
    assert phase(patcher)["phase"] == "Pending"
    assert phase(patcher)["message"] == "waiting for control channel"


async def test_ack_accepted_absent_is_pending():
    publisher, _, registry, patcher, _ = await build()
    registry.record_ack(DeclaredAck(endpoints=[DeclaredAckEntry(label="grafana")], revision=1))
    await publisher.publish_once()
    assert phase(patcher)["phase"] == "Pending"
    assert phase(patcher)["message"] == "waiting for endpoint to start"


async def test_conflict_is_failed_with_server_message():
    publisher, _, registry, patcher, _ = await build()
    registry.record_ack(
        DeclaredAck(
            endpoints=[
                DeclaredAckEntry(
                    label="grafana",
                    status="conflict",
                    message="label is already used by a dashboard-owned endpoint",
                )
            ],
            revision=1,
        )
    )
    await publisher.publish_once()
    assert phase(patcher)["phase"] == "Failed"
    assert phase(patcher)["message"] == "label is already used by a dashboard-owned endpoint"


async def test_connected_carries_url_and_subdomain():
    publisher, client, _, patcher, _ = await build()
    client.statuses = [
        EndpointStatus(label="grafana", connected=True, public_url="https://grafana-x7k.hle.world")
    ]
    await publisher.publish_once()
    assert phase(patcher)["phase"] == "Connected"
    assert phase(patcher)["publicUrl"] == "https://grafana-x7k.hle.world"
    assert phase(patcher)["subdomain"] == "grafana-x7k"


async def test_not_connected_is_disconnected():
    publisher, client, _, patcher, _ = await build()
    client.statuses = [EndpointStatus(label="grafana", connected=False)]
    await publisher.publish_once()
    assert phase(patcher)["phase"] == "Disconnected"


async def test_endpoint_error_is_failed():
    publisher, client, _, patcher, _ = await build()
    client.statuses = [
        EndpointStatus(label="grafana", connected=False, error="refused: namespace excluded")
    ]
    await publisher.publish_once()
    assert phase(patcher)["phase"] == "Failed"
    assert phase(patcher)["message"] == "refused: namespace excluded"


async def test_invalid_declaration_is_failed():
    body = {**CRD, "spec": {**CRD["spec"], "label": "Bad Label"}}
    publisher, _, _, patcher, _ = await build(body)
    await publisher.publish_once()
    assert phase(patcher)["phase"] == "Failed"
    assert "invalid label" in phase(patcher)["message"]


async def test_patches_only_on_change():
    publisher, client, _, patcher, _ = await build()
    client.statuses = [EndpointStatus(label="grafana", connected=False)]
    await publisher.publish_once()
    await publisher.publish_once()
    assert len(patcher.calls) == 1


async def test_failed_patch_retries_with_backoff():
    publisher, client, _, patcher, clock = await build()
    patcher.fail = ApiError(500)
    await publisher.publish_once()
    assert len(patcher.calls) == 1
    await publisher.publish_once()  # inside the backoff window: no hammering
    assert len(patcher.calls) == 1
    clock.now += 10.0
    await publisher.publish_once()
    assert len(patcher.calls) == 2
    clock.now += 10.0  # backoff doubled to 20s
    await publisher.publish_once()
    assert len(patcher.calls) == 2
    patcher.fail = None
    clock.now += 10.0
    await publisher.publish_once()
    assert len(patcher.calls) == 3
    await publisher.publish_once()  # written now, so no more patches
    assert len(patcher.calls) == 3


async def test_not_found_is_not_retried():
    publisher, _, _, patcher, _ = await build()
    patcher.fail = ApiError(404)
    await publisher.publish_once()
    await publisher.publish_once()
    assert len(patcher.calls) == 1


async def test_removed_target_state_is_pruned():
    publisher, _, registry, patcher, _ = await build()
    await publisher.publish_once()
    assert publisher._last
    registry.remove("hletunnel:monitoring/grafana", schedule=False)
    await publisher.publish_once()
    assert publisher._last == {} and publisher._retry == {}


async def test_recreated_object_gets_status_again():
    publisher, _, registry, patcher, _ = await build()
    await publisher.publish_once()
    recreated = {**CRD, "metadata": {**CRD["metadata"], "uid": "u2"}}
    await registry.apply_crd(recreated, schedule=False)
    await publisher.publish_once()
    assert len(patcher.calls) == 2


async def test_ingress_status_patched_as_ingress():
    publisher, _, _, patcher, _ = await build(INGRESS)
    await publisher.publish_once()
    assert patcher.calls[-1][0] == "ingress"
    assert patcher.calls[-1][1:3] == ("apps", "web")


def test_kubernetes_patcher_uses_ingress_status_subresource():
    api = MagicMock()
    with patch("hle_operator.status.kubernetes.client.NetworkingV1Api", return_value=api):
        KubernetesStatusPatcher().patch_ingress(
            "apps",
            "web",
            {"phase": "Connected", "publicUrl": "https://web-x.hle.world", "message": ""},
        )
    status_body = api.patch_namespaced_ingress_status.call_args.args[2]
    assert status_body == {
        "status": {"loadBalancer": {"ingress": [{"hostname": "web-x.hle.world"}]}}
    }
    main_body = api.patch_namespaced_ingress.call_args.args[2]
    assert "status" not in main_body
    assert main_body["metadata"]["annotations"]["hle.world/public-url"] == "https://web-x.hle.world"


def test_kubernetes_patcher_clears_ingress_status_without_url():
    api = MagicMock()
    with patch("hle_operator.status.kubernetes.client.NetworkingV1Api", return_value=api):
        KubernetesStatusPatcher().patch_ingress("apps", "web", {"phase": "Failed", "publicUrl": ""})
    status_body = api.patch_namespaced_ingress_status.call_args.args[2]
    assert status_body == {"status": {"loadBalancer": {"ingress": []}}}
    main_body = api.patch_namespaced_ingress.call_args.args[2]
    assert main_body["metadata"]["annotations"]["hle.world/public-url"] is None
