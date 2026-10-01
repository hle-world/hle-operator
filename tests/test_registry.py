"""Registry: initial gate, revision monotonicity, debounce, Secrets, resolver."""

from __future__ import annotations

import pytest

# Agent mode needs the unreleased P3+P4 hle-client (protocol 1.4). Under an
# older client these modules still collect but skip, so the legacy suite runs.
try:
    from hle_common.agent_protocol import DeclaredAccess  # noqa: F401
except ImportError as exc:  # pragma: no cover
    pytest.skip(f"hle-client P3+P4 required: {exc}", allow_module_level=True)


import asyncio

from hle_common.agent_protocol import DeclaredAck, DeclaredAckEntry, DeclaredEndpoint, EndpointSpec

from hle_operator.registry import DeclarationRegistry, parse_secret_ref


def crd_body(name: str = "grafana", namespace: str = "monitoring", **spec) -> dict:
    base = {"serviceRef": {"name": "grafana", "port": 3000}, "label": "grafana"}
    base.update(spec)
    return {"metadata": {"name": name, "namespace": namespace, "uid": f"uid-{name}"}, "spec": base}


def ingress_body(name: str = "web", namespace: str = "apps") -> dict:
    return {
        "metadata": {"name": name, "namespace": namespace},
        "spec": {
            "ingressClassName": "hle",
            "rules": [
                {
                    "http": {
                        "paths": [{"backend": {"service": {"name": "web", "port": {"number": 80}}}}]
                    }
                }
            ],
        },
    }


class FakeClient:
    def __init__(self) -> None:
        self.sent: list[tuple[list[DeclaredEndpoint], int]] = []

    async def send_declared_endpoints(
        self, endpoints: list[DeclaredEndpoint], revision: int
    ) -> bool:
        self.sent.append((list(endpoints), revision))
        return True

    def last_labels(self) -> list[str]:
        return [e.label for e in self.sent[-1][0]] if self.sent else []


class FakeLister:
    def __init__(self, crds=None, ingresses=None, fail=False) -> None:
        self.crds = crds or []
        self.ingresses = ingresses or []
        self.fail = fail
        self.during_list = None  # called mid-list to simulate a racing event

    def list_hletunnels(self):
        if self.fail:
            raise RuntimeError("api down")
        return self.crds

    def list_hle_ingresses(self):
        if self.fail:
            raise RuntimeError("api down")
        if self.during_list is not None:
            self.during_list()
        return self.ingresses


class ApiError(Exception):
    def __init__(self, status: int) -> None:
        super().__init__(f"HTTP {status}")
        self.status = status


class Harness:
    def __init__(self, lister: FakeLister | None = None) -> None:
        self.client = FakeClient()
        self.lister = lister or FakeLister()
        self.secrets: dict[str, str] = {}
        self.basic: dict[tuple[str, str], str | Exception] = {}
        self.stripped: list[tuple[str, str, list[str], str | None]] = []
        self.secret_reads = 0
        self.registry = DeclarationRegistry(
            self.client,
            lister=self.lister,
            read_secret=self.read_secret,
            read_basic_auth=self.read_basic_auth,
            strip_finalizer=self.strip,
            debounce=0.01,
            secret_poll_interval=999.0,
            relist_interval=999.0,
        )

    def read_secret(self, ref: str) -> str:
        self.secret_reads += 1
        if ref not in self.secrets:
            raise KeyError(ref)
        return self.secrets[ref]

    def read_basic_auth(self, ns: str, name: str) -> str | None:
        value = self.basic.get((ns, name), ApiError(404))
        if isinstance(value, Exception):
            raise value
        return value

    def strip(self, ns: str, name: str, remaining: list[str], rv: str | None) -> None:
        self.stripped.append((ns, name, remaining, rv))


async def ready(h: Harness) -> None:
    assert await h.registry.try_initial_sync() is True


def test_parse_secret_ref():
    assert parse_secret_ref("ns/name#key") == ("ns", "name", "key")


def test_revision_is_monotonic():
    h = Harness()
    revisions = [h.registry.next_revision() for _ in range(5)]
    assert revisions == sorted(revisions)
    assert len(set(revisions)) == len(revisions)


def test_revision_monotonic_when_clock_steps_back(monkeypatch):
    import hle_operator.registry as mod

    h = Harness()
    now = [2_000_000_000_000_000_000]
    monkeypatch.setattr(mod.time, "time_ns", lambda: now[0])
    first = h.registry.next_revision()
    now[0] -= 3_600 * 1_000_000_000  # NTP steps the clock back an hour
    second = h.registry.next_revision()
    assert second == first + 1


# -- initial gate ------------------------------------------------------------


async def test_initial_sync_gate_blocks_empty_frame_on_failure():
    h = Harness(FakeLister(fail=True))
    assert await h.registry.try_initial_sync() is False
    # Events that arrive before the list succeeds still must not send.
    await h.registry.apply_crd(crd_body())
    assert await h.registry.send_if_ready() is False
    await asyncio.sleep(0.05)
    assert h.client.sent == []


async def test_initial_sync_sends_complete_set_once():
    h = Harness(FakeLister(crds=[crd_body()], ingresses=[ingress_body()]))
    await ready(h)
    assert len(h.client.sent) == 1
    assert sorted(h.client.last_labels()) == ["grafana", "web"]


async def test_initial_sync_with_empty_cluster_sends_explicit_empty_frame():
    h = Harness()
    await ready(h)
    assert h.client.sent and h.client.sent[-1][0] == []


async def test_initial_sync_discarded_when_an_event_races_the_list():
    lister = FakeLister(crds=[crd_body(name="gone")])
    h = Harness(lister)
    # The CR is deleted while the list is in flight: the list may predate it.
    lister.during_list = lambda: h.registry.remove("hletunnel:monitoring/gone")
    assert await h.registry.try_initial_sync() is False
    assert h.client.sent == []
    lister.during_list = None
    lister.crds = []
    await ready(h)
    assert h.client.sent[-1][0] == []


async def test_initial_sync_transient_secret_error_retries_not_shrinks():
    body = crd_body(accessControl={"basicAuth": {"secretRef": {"name": "ba"}}})
    h = Harness(FakeLister(crds=[body]))
    h.basic[("monitoring", "ba")] = ApiError(500)
    assert await h.registry.try_initial_sync() is False
    assert h.client.sent == []


async def test_poll_before_initial_sync_never_sends():
    h = Harness(FakeLister(fail=True))
    body = crd_body(accessControl={"basicAuth": {"secretRef": {"name": "ba"}}})
    await h.registry.apply_crd(body, schedule=False)
    h.basic[("monitoring", "ba")] = "u:one"
    assert await h.registry.poll_secrets_once() is True
    assert h.client.sent == []


async def test_relist_failure_keeps_set_and_sends_nothing():
    lister = FakeLister(crds=[crd_body()])
    h = Harness(lister)
    await ready(h)
    lister.fail = True
    assert await h.registry.relist() is False
    assert len(h.client.sent) == 1
    assert [e.label for e in h.registry.declared()] == ["grafana"]


async def test_relist_corrects_drift():
    lister = FakeLister(crds=[crd_body()])
    h = Harness(lister)
    await ready(h)
    lister.crds = []
    assert await h.registry.relist() is True
    assert h.client.sent[-1][0] == []


# -- debounce / change detection -----------------------------------------------


async def test_debounce_coalesces_events():
    h = Harness()
    await ready(h)
    await h.registry.apply_crd(crd_body(name="a", label="a"))
    await h.registry.apply_crd(crd_body(name="b", label="b"))
    await h.registry.apply_crd(crd_body(name="c", label="c"))
    await asyncio.sleep(0.05)
    assert len(h.client.sent) == 2  # initial empty + one debounced frame
    assert sorted(h.client.last_labels()) == ["a", "b", "c"]


async def test_change_during_send_is_not_lost():
    h = Harness()
    await ready(h)
    gate = asyncio.Event()
    original = h.client.send_declared_endpoints

    async def slow_send(endpoints, revision):
        await gate.wait()
        return await original(endpoints, revision)

    h.client.send_declared_endpoints = slow_send  # type: ignore[method-assign]
    await h.registry.apply_crd(crd_body(name="a", label="a"))
    await asyncio.sleep(0.03)  # the debounced send is now blocked mid-send
    await h.registry.apply_crd(crd_body(name="b", label="b"))
    gate.set()
    await asyncio.sleep(0.05)
    assert sorted(h.client.last_labels()) == ["a", "b"]


async def test_status_only_modified_event_neither_sends_nor_counts():
    h = Harness()
    await ready(h)
    body = crd_body()
    await h.registry.apply_crd(body)
    await asyncio.sleep(0.03)
    sent = len(h.client.sent)
    mutations = h.registry._mutations
    body = crd_body()
    body["status"] = {"phase": "Connected"}
    body["metadata"]["resourceVersion"] = "99"
    await h.registry.apply_crd(body)
    await asyncio.sleep(0.03)
    assert len(h.client.sent) == sent
    assert h.registry._mutations == mutations


async def test_apply_invalid_label_records_error():
    h = Harness()
    await h.registry.apply_crd(crd_body(label="Bad Label"))
    targets = h.registry.targets()
    assert len(targets) == 1
    assert targets[0].endpoint is None
    assert targets[0].error is not None


async def test_duplicate_label_first_wins_other_fails():
    h = Harness()
    await h.registry.apply_crd(crd_body(name="a"))
    await h.registry.apply_crd(crd_body(name="b"))
    assert [e.source_ref for e in h.registry.declared()] == ["hletunnel:monitoring/a"]
    failed = [t for t in h.registry.targets() if t.endpoint is None]
    assert [t.source_ref for t in failed] == ["hletunnel:monitoring/b"]
    assert "already declared by hletunnel:monitoring/a" in (failed[0].error or "")


async def test_remove_drops_endpoint():
    h = Harness()
    await h.registry.apply_crd(crd_body(), schedule=False)
    assert h.registry.declared()
    h.registry.remove("hletunnel:monitoring/grafana", schedule=False)
    assert h.registry.declared() == []
    assert h.registry.targets() == []


# -- finalizer ---------------------------------------------------------------


async def test_strip_legacy_finalizer_keeps_others_and_pins_rv():
    h = Harness()
    body = crd_body()
    body["metadata"]["finalizers"] = ["hle.world/operator-finalizer", "other"]
    body["metadata"]["resourceVersion"] = "42"
    assert await h.registry.strip_legacy_finalizer(body) is True
    assert h.stripped == [("monitoring", "grafana", ["other"], "42")]
    assert await h.registry.strip_legacy_finalizer(crd_body()) is False
    assert len(h.stripped) == 1


async def test_strip_failure_is_swallowed():
    h = Harness()

    def boom(*_):
        raise ApiError(409)

    h.registry._strip_finalizer = boom
    body = crd_body()
    body["metadata"]["finalizers"] = ["hle.world/operator-finalizer"]
    assert await h.registry.strip_legacy_finalizer(body) is False


# -- Secrets -----------------------------------------------------------------


async def test_resolve_spec_fills_upstream_basic_auth_from_cache_only():
    h = Harness()
    h.secrets["sec/api#password"] = "u:p"
    await h.registry.apply_crd(crd_body(upstreamBasicAuthSecret="sec/api#password"))
    reads = h.secret_reads

    spec = EndpointSpec(id=1, label="grafana", service_url="http://grafana:3000")
    assert h.registry.resolve_spec(spec).upstream_basic_auth == "u:p"
    assert h.secret_reads == reads  # never an API call on the event loop
    # A label the cluster does not declare is passed through untouched.
    other = EndpointSpec(id=2, label="dashboard", service_url="http://x")
    assert h.registry.resolve_spec(other) == other


async def test_resolve_spec_missing_secret_raises_value_error_without_value():
    h = Harness()
    await h.registry.apply_crd(crd_body(upstreamBasicAuthSecret="sec/api#password"))
    spec = EndpointSpec(id=1, label="grafana", service_url="http://grafana:3000")
    with pytest.raises(ValueError, match="sec/api#password"):
        h.registry.resolve_spec(spec)


async def test_upstream_rotation_forces_frame_even_with_identical_set():
    h = Harness()
    h.secrets["sec/api#password"] = "u:one"
    h.registry._lister.crds = [crd_body(upstreamBasicAuthSecret="sec/api#password")]
    await ready(h)
    sent = len(h.client.sent)
    assert await h.registry.poll_secrets_once() is False  # unchanged
    assert len(h.client.sent) == sent
    h.secrets["sec/api#password"] = "u:two"
    assert await h.registry.poll_secrets_once() is True
    assert len(h.client.sent) == sent + 1
    assert h.client.sent[-1][0] == h.client.sent[-2][0]  # same set, new revision
    assert h.client.sent[-1][1] > h.client.sent[-2][1]


async def test_visitor_secret_missing_fails_closed():
    h = Harness()
    body = crd_body(accessControl={"basicAuth": {"secretRef": {"name": "ba"}}})
    await h.registry.apply_crd(body, schedule=False)
    assert h.registry.declared() == []
    [target] = h.registry.targets()
    assert target.endpoint is None and "monitoring/ba" in (target.error or "")


async def test_visitor_rotation_redeclares_only_on_change():
    h = Harness()
    h.basic[("monitoring", "ba")] = "u:one"
    body = crd_body(accessControl={"basicAuth": {"secretRef": {"name": "ba"}}})
    h.registry._lister.crds = [body]
    await ready(h)
    assert h.registry.declared()[0].access.basic_auth == "u:one"
    sent = len(h.client.sent)

    assert await h.registry.poll_secrets_once() is False
    assert len(h.client.sent) == sent

    h.basic[("monitoring", "ba")] = "u:two"
    assert await h.registry.poll_secrets_once() is True
    assert len(h.client.sent) == sent + 1
    assert h.client.sent[-1][0][0].access.basic_auth == "u:two"

    # A transient error keeps the last good value: no frame, no weakening.
    h.basic[("monitoring", "ba")] = ApiError(500)
    assert await h.registry.poll_secrets_once() is False
    assert len(h.client.sent) == sent + 1
    assert h.registry.declared()[0].access.basic_auth == "u:two"


async def test_visitor_secret_appearing_later_declares_endpoint():
    h = Harness()
    body = crd_body(accessControl={"basicAuth": {"secretRef": {"name": "ba"}}})
    h.registry._lister.crds = [body]
    await ready(h)
    assert h.client.sent[-1][0] == []
    h.basic[("monitoring", "ba")] = "u:p"
    assert await h.registry.poll_secrets_once() is True
    assert h.client.last_labels() == ["grafana"]


async def test_secret_values_are_not_logged(caplog):
    h = Harness()
    h.basic[("monitoring", "ba")] = "user:s3cr3t-value"
    body = crd_body(accessControl={"basicAuth": {"secretRef": {"name": "ba"}}})
    h.registry._lister.crds = [body]
    caplog.set_level("DEBUG")
    await ready(h)
    h.basic[("monitoring", "ba")] = ApiError(500)
    await h.registry.poll_secrets_once()
    assert "s3cr3t-value" not in caplog.text
    assert "s3cr3t-value" not in repr(h.registry.declared())


async def test_secret_cache_pruned_when_unreferenced():
    h = Harness()
    h.basic[("monitoring", "ba")] = "u:p"
    body = crd_body(accessControl={"basicAuth": {"secretRef": {"name": "ba"}}})
    h.registry._lister.crds = [body]
    await ready(h)
    h.registry.remove("hletunnel:monitoring/grafana")
    await h.registry.poll_secrets_once()
    assert h.registry._visitor_cache == {}


# -- acks --------------------------------------------------------------------


def test_record_ack():
    h = Harness()
    h.registry.record_ack(
        DeclaredAck(endpoints=[DeclaredAckEntry(label="grafana", status="conflict")], revision=1)
    )
    assert h.registry.ack_seen() is True
    entry = h.registry.ack_for_label("grafana")
    assert entry is not None and entry.status == "conflict"


def test_stale_ack_ignored():
    h = Harness()
    h.registry.record_ack(DeclaredAck(endpoints=[DeclaredAckEntry(label="grafana")], revision=5))
    h.registry.record_ack(
        DeclaredAck(endpoints=[DeclaredAckEntry(label="grafana", status="conflict")], revision=4)
    )
    entry = h.registry.ack_for_label("grafana")
    assert entry is not None and entry.status == "accepted"
