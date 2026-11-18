"""DashboardOverride condition, transition Events and the hello capability."""

from __future__ import annotations

import pytest

try:
    from hle_common.agent_protocol import DECLARED_OVERRIDE_CAPABILITY
except ImportError as exc:  # pragma: no cover
    pytest.skip(f"hle-client >= 2611.4 required: {exc}", allow_module_level=True)

from hle_common.agent_protocol import AgentHello, DeclaredAck, DeclaredAckEntry

from hle_operator.capabilities import OperatorHello
from tests.test_status import CRD, INGRESS, RecordingPatcher, build, phase


class RecordingEvents:
    def __init__(self) -> None:
        self.events: list[dict] = []

    def record_event(self, namespace, name, **kwargs) -> None:
        self.events.append({"namespace": namespace, "name": name, **kwargs})


def override_ack(overridden: bool, revision: int = 1, label: str = "grafana") -> DeclaredAck:
    return DeclaredAck(
        endpoints=[DeclaredAckEntry(label=label, overridden=overridden)], revision=revision
    )


async def build_with_events(body: dict = CRD):
    publisher, _, registry, patcher, _ = await build(body)
    events = RecordingEvents()
    publisher._recorder = events
    return publisher, registry, patcher, events


def override_condition(patcher: RecordingPatcher) -> dict | None:
    conditions = phase(patcher).get("conditions")
    return conditions[0] if conditions else None


async def test_override_true_sets_condition_and_one_warning_event(caplog):
    publisher, registry, patcher, events = await build_with_events()
    registry.record_ack(override_ack(True, 1))
    with caplog.at_level("INFO", logger="hle_operator.status"):
        await publisher.publish_once()
    cond = override_condition(patcher)
    assert cond["type"] == "DashboardOverride"
    assert cond["status"] == "True"
    assert cond["reason"] == "ManagedInDashboard"
    assert cond["message"].startswith("Settings for this tunnel are managed in the HLE dashboard")
    assert len(events.events) == 1
    event = events.events[0]
    assert (event["event_type"], event["reason"]) == ("Warning", "DashboardOverride")
    assert event["kind"] == "HLETunnel"
    assert (event["namespace"], event["name"]) == ("monitoring", "grafana")
    assert "monitoring/grafana" in caplog.text


async def test_repeated_acks_do_not_re_emit_or_repatch():
    publisher, registry, patcher, events = await build_with_events()
    registry.record_ack(override_ack(True, 1))
    await publisher.publish_once()
    patches = len(patcher.calls)
    for revision in (2, 3, 4):
        registry.record_ack(override_ack(True, revision))
        await publisher.publish_once()
    assert len(events.events) == 1
    assert len(patcher.calls) == patches


async def test_back_to_false_sets_false_and_normal_event():
    publisher, registry, patcher, events = await build_with_events()
    registry.record_ack(override_ack(True, 1))
    await publisher.publish_once()
    registry.record_ack(override_ack(False, 2))
    await publisher.publish_once()
    cond = override_condition(patcher)
    assert (cond["status"], cond["reason"]) == ("False", "ManagedByCluster")
    assert [(e["event_type"], e["reason"]) for e in events.events] == [
        ("Warning", "DashboardOverride"),
        ("Normal", "DashboardOverrideEnded"),
    ]
    registry.record_ack(override_ack(False, 3))
    await publisher.publish_once()
    assert len(events.events) == 2


async def test_older_server_never_overridden_is_silent():
    publisher, registry, patcher, events = await build_with_events()
    registry.record_ack(DeclaredAck(endpoints=[DeclaredAckEntry(label="grafana")], revision=1))
    await publisher.publish_once()
    assert "conditions" not in phase(patcher)
    assert events.events == []


async def test_stored_true_condition_resumes_after_restart():
    body = {
        **CRD,
        "status": {
            "conditions": [
                {
                    "type": "DashboardOverride",
                    "status": "True",
                    "lastTransitionTime": "2026-11-01T00:00:00Z",
                }
            ]
        },
    }
    publisher, registry, patcher, events = await build_with_events(body)
    registry.record_ack(override_ack(True, 1))
    await publisher.publish_once()
    assert events.events == []  # not re-announced
    assert override_condition(patcher)["lastTransitionTime"] == "2026-11-01T00:00:00Z"
    registry.record_ack(override_ack(False, 2))
    await publisher.publish_once()
    assert events.events[-1]["reason"] == "DashboardOverrideEnded"


async def test_ingress_override_is_an_event_on_the_ingress_without_condition():
    publisher, registry, patcher, events = await build_with_events(INGRESS)
    label = registry.declared()[0].label
    registry.record_ack(override_ack(True, 1, label=label))
    await publisher.publish_once()
    assert len(events.events) == 1
    event = events.events[0]
    assert (event["kind"], event["api_version"]) == ("Ingress", "networking.k8s.io/v1")
    assert (event["namespace"], event["name"]) == ("apps", "web")
    assert "conditions" not in phase(patcher)


def test_operator_hello_advertises_declared_override():
    hello = OperatorHello(token="t", agent_version="1", capabilities=["logs"])
    assert hello.capabilities == ["logs", DECLARED_OVERRIDE_CAPABILITY]
    again = OperatorHello(token="t", agent_version="1", capabilities=[DECLARED_OVERRIDE_CAPABILITY])
    assert again.capabilities == [DECLARED_OVERRIDE_CAPABILITY]
    assert AgentHello(token="t", agent_version="1").capabilities == []
