"""Mirror each declared endpoint's status onto its CR or Ingress.

The agent owns the tunnels and reports status; kopf only writes it back. One
pass every ten seconds reads ``AgentClient.endpoint_statuses()`` (which is
``None`` while this process does not hold the control session) plus the last
``declared_ack``, computes a phase, and patches only what changed. A failed
patch is retried with a per-object backoff, and state for objects that left
the registry is dropped, so neither a broken API nor churn grows anything.

The same pass surfaces a dashboard override: when the relay's acknowledgement
carries ``overridden`` for an endpoint, the CR gets a ``DashboardOverride``
condition and one Event per transition. An Ingress has no conditions, so only
the Event is posted there.

A pod that is shutting down must not write ``Disconnected``: during a rollout
the terminating replica is draining while a newer one already owns the tunnel,
and its last write would otherwise win. ``stop()`` puts the loop off duty, and
even so a pass re-asserts a ``Connected`` phase another writer stored over it,
so a stale ``Disconnected`` heals on the next tick.
"""

from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Protocol, runtime_checkable
from urllib.parse import urlparse

import kubernetes.client
from hle_common.agent_protocol import EndpointStatus

from hle_operator.declared import CRD_KIND, INGRESS_KIND, PUBLIC_URL_ANNOTATION
from hle_operator.registry import (
    CUSTOM_GROUP,
    CUSTOM_PLURAL,
    CUSTOM_VERSION,
    DeclarationRegistry,
    DeclarationTarget,
)

logger = logging.getLogger(__name__)

STATUS_INTERVAL = 10.0
MAX_BACKOFF = 300.0

# Dashboard override: the HLE dashboard owns the tunnel's settings, so edits to
# the declaring object are recorded but not applied. The condition lives on the
# CR; the two Events mark the transitions in and out of it.
OVERRIDE_TYPE = "DashboardOverride"
OVERRIDE_STARTED_REASON = "ManagedInDashboard"
OVERRIDE_ENDED_REASON = "ManagedByCluster"
OVERRIDE_EVENT_REASON = "DashboardOverride"
OVERRIDE_ENDED_EVENT_REASON = "DashboardOverrideEnded"
OVERRIDE_MESSAGE = (
    "Settings for this tunnel are managed in the HLE dashboard; changes to this "
    "resource are recorded but not applied until the override is turned off."
)
OVERRIDE_ENDED_MESSAGE = (
    "The HLE dashboard override was turned off; settings for this tunnel are "
    "managed by the cluster again."
)

# The kind half of a source_ref plus the apiVersion/kind an Event points at.
_CRD_API_VERSION = "hle.world/v1alpha1"
_CRD_OBJECT_KIND = "HLETunnel"
_INGRESS_API_VERSION = "networking.k8s.io/v1"
_INGRESS_OBJECT_KIND = "Ingress"


class StatusClient(Protocol):
    def endpoint_statuses(self) -> list[EndpointStatus] | None: ...


class StatusPatcher(Protocol):
    """Where a computed status goes. Injected so tests never touch the API."""

    def patch_cr(self, namespace: str, name: str, status: dict[str, Any]) -> None: ...

    def patch_ingress(self, namespace: str, name: str, status: dict[str, Any]) -> None: ...


@runtime_checkable
class EventRecorder(Protocol):
    """Where a transition Event goes. Injected so tests never touch the API."""

    def record_event(
        self,
        namespace: str,
        name: str,
        *,
        api_version: str,
        kind: str,
        uid: str,
        event_type: str,
        reason: str,
        message: str,
    ) -> None: ...


class KubernetesStatusPatcher:
    """Write status to the real API: CR status subresource and Ingress status."""

    def patch_cr(self, namespace: str, name: str, status: dict[str, Any]) -> None:
        api = kubernetes.client.CustomObjectsApi()
        api.patch_namespaced_custom_object_status(
            CUSTOM_GROUP, CUSTOM_VERSION, namespace, CUSTOM_PLURAL, name, {"status": status}
        )

    def patch_ingress(self, namespace: str, name: str, status: dict[str, Any]) -> None:
        public_url = status.get("publicUrl") or ""
        hostname = urlparse(public_url).hostname or "" if public_url else ""
        api = kubernetes.client.NetworkingV1Api()
        # Ingress status is a subresource: a patch of the object itself
        # silently drops it, so the two halves go to their own endpoints.
        api.patch_namespaced_ingress_status(
            name,
            namespace,
            {"status": {"loadBalancer": {"ingress": [{"hostname": hostname}] if hostname else []}}},
        )
        api.patch_namespaced_ingress(
            name,
            namespace,
            {"metadata": {"annotations": {PUBLIC_URL_ANNOTATION: public_url or None}}},
        )


class KubernetesEventRecorder:
    """Post Events to the real API, one per transition."""

    def record_event(
        self,
        namespace: str,
        name: str,
        *,
        api_version: str,
        kind: str,
        uid: str,
        event_type: str,
        reason: str,
        message: str,
    ) -> None:
        now = datetime.now(timezone.utc).isoformat()
        body: dict[str, Any] = {
            "metadata": {"generateName": f"{name}.", "namespace": namespace},
            "involvedObject": {
                "apiVersion": api_version,
                "kind": kind,
                "namespace": namespace,
                "name": name,
                "uid": uid,
            },
            "reason": reason,
            "message": message,
            "type": event_type,
            "source": {"component": "hle-operator"},
            "firstTimestamp": now,
            "lastTimestamp": now,
            "count": 1,
        }
        kubernetes.client.CoreV1Api().create_namespaced_event(namespace, body)


@dataclass
class _Retry:
    at: float
    delay: float


class StatusPublisher:
    """The ten-second status loop."""

    def __init__(
        self,
        client: StatusClient,
        registry: DeclarationRegistry,
        *,
        interval: float = STATUS_INTERVAL,
        patcher: StatusPatcher | None = None,
        recorder: EventRecorder | None = None,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._client = client
        self._registry = registry
        self._interval = interval
        self._patcher = patcher or KubernetesStatusPatcher()
        if recorder is not None:
            self._recorder: EventRecorder = recorder
        elif isinstance(self._patcher, EventRecorder):
            # A test patcher that also records events is the whole sink.
            self._recorder = self._patcher
        else:
            self._recorder = KubernetesEventRecorder()
        self._clock = clock
        # source_ref -> (uid, last status written). Bounded by the registry.
        self._last: dict[str, tuple[str, dict[str, Any]]] = {}
        self._retry: dict[str, _Retry] = {}
        # source_ref -> last dashboard-override state we reported. Absent means
        # the relay has never said anything, so no condition and no event.
        self._overrides: dict[str, bool] = {}
        # Set once the pod starts shutting down: a draining replica never writes
        # status, so it cannot overwrite the replica taking the tunnel over.
        self._stopping = False

    def stop(self) -> None:
        """Take the loop off duty; a shutting-down pod writes no status."""
        self._stopping = True

    async def run(self) -> None:
        while not self._stopping:
            await asyncio.sleep(self._interval)
            try:
                await self.publish_once()
            except Exception:  # noqa: BLE001 — status must never take the process down
                logger.exception("Status publish failed")

    async def publish_once(self) -> None:
        if self._stopping:
            return
        statuses = self._client.endpoint_statuses()
        if statuses is None:
            return
        by_label = {status.label: status for status in statuses}
        targets = self._registry.targets()
        live = {target.source_ref for target in targets}
        for gone in self._last.keys() - live:
            del self._last[gone]
        for gone in self._retry.keys() - live:
            del self._retry[gone]
        for gone in self._overrides.keys() - live:
            del self._overrides[gone]

        now = self._clock()
        for target in targets:
            await self._record_override(target)
            computed: dict[str, Any] = self._compute(target, by_label)
            if target.source_ref.startswith(f"{CRD_KIND}:"):
                conditions = _conditions(self._overrides.get(target.source_ref))
                if conditions is not None:
                    computed["conditions"] = conditions
            last = self._last.get(target.source_ref)
            if last == (target.uid, computed) and not self._stale(target, computed):
                self._retry.pop(target.source_ref, None)
                continue
            retry = self._retry.get(target.source_ref)
            if retry is not None and now < retry.at:
                continue
            if await self._patch(target.source_ref, computed):
                self._last[target.source_ref] = (target.uid, computed)
                self._retry.pop(target.source_ref, None)
            else:
                delay = min(MAX_BACKOFF, retry.delay * 2) if retry else self._interval
                self._retry[target.source_ref] = _Retry(at=now + delay, delay=delay)

    async def _record_override(self, target: DeclarationTarget) -> None:
        """Turn the ack's ``overridden`` into a condition and one Event.

        A transition is the only thing worth reporting: a repeated ack with the
        same value, and an older relay that never sends the field, are silent.
        """
        if target.endpoint is None:
            return
        ack = self._registry.ack_for_label(target.endpoint.label)
        if ack is None:
            return
        overridden = bool(getattr(ack, "overridden", False))
        previous = self._overrides.get(target.source_ref)
        if overridden:
            if previous is True:
                return
            self._overrides[target.source_ref] = True
            self._log_override(target, overridden=True)
            await self._emit_event(
                target, "Warning", OVERRIDE_EVENT_REASON, OVERRIDE_MESSAGE
            )
        elif previous is True:
            self._overrides[target.source_ref] = False
            self._log_override(target, overridden=False)
            await self._emit_event(
                target, "Normal", OVERRIDE_ENDED_EVENT_REASON, OVERRIDE_ENDED_MESSAGE
            )

    async def _emit_event(
        self, target: DeclarationTarget, event_type: str, reason: str, message: str
    ) -> None:
        kind, _, path = target.source_ref.partition(":")
        namespace, _, name = path.partition("/")
        if kind == CRD_KIND:
            api_version, object_kind = _CRD_API_VERSION, _CRD_OBJECT_KIND
        else:
            api_version, object_kind = _INGRESS_API_VERSION, _INGRESS_OBJECT_KIND
        try:
            await asyncio.to_thread(
                self._recorder.record_event,
                namespace,
                name,
                api_version=api_version,
                kind=object_kind,
                uid=target.uid,
                event_type=event_type,
                reason=reason,
                message=message,
            )
        except Exception:  # noqa: BLE001 — an Event must never break status
            logger.warning("Could not record %s Event for %s", reason, target.source_ref)

    @staticmethod
    def _log_override(target: DeclarationTarget, *, overridden: bool) -> None:
        _, _, path = target.source_ref.partition(":")
        namespace, _, name = path.partition("/")
        label = target.endpoint.label if target.endpoint is not None else ""
        logger.info(
            "Dashboard override %s for %s/%s (label %s)",
            "started" if overridden else "ended",
            namespace,
            name,
            label,
        )

    @staticmethod
    def _stale(target: DeclarationTarget, computed: Mapping[str, Any]) -> bool:
        """True when the cluster stored a phase that disagrees with *computed*.

        This is how a stale writer heals: once the watch sees a phase other
        than ours (for example a terminating pod wrote ``Disconnected`` over a
        working ``Connected``), the next pass re-asserts ours.
        """
        return bool(target.stored_phase) and target.stored_phase != computed["phase"]

    def _compute(
        self,
        target: DeclarationTarget,
        by_label: Mapping[str, EndpointStatus],
    ) -> dict[str, str]:
        if target.endpoint is None:
            return _status("Failed", target.error or "invalid declaration")
        endpoint = target.endpoint
        ack = self._registry.ack_for_label(endpoint.label)
        if ack is not None and ack.status == "conflict":
            return _status("Failed", ack.message or "label is already in use")

        reported = by_label.get(endpoint.label)
        if reported is not None:
            if reported.error:
                return _status("Failed", reported.error)
            if reported.connected:
                return _status("Connected", "", public_url=reported.public_url or "")
            return _status("Disconnected", "endpoint is not connected")

        if not self._registry.ack_seen():
            return _status("Pending", "waiting for control channel")
        return _status("Pending", "waiting for endpoint to start")

    async def _patch(self, source_ref: str, status: dict[str, Any]) -> bool:
        kind, _, path = source_ref.partition(":")
        namespace, _, name = path.partition("/")
        try:
            if kind == CRD_KIND:
                await asyncio.to_thread(self._patcher.patch_cr, namespace, name, status)
            elif kind == INGRESS_KIND:
                await asyncio.to_thread(self._patcher.patch_ingress, namespace, name, status)
        except Exception as exc:  # noqa: BLE001 — retried with backoff
            if getattr(exc, "status", None) == 404:
                # Deleted between the watch event and this pass; the DELETED
                # event drops it from the registry.
                return True
            logger.warning("Failed to patch status for %s: %s", source_ref, _reason(exc))
            return False
        return True


def _reason(exc: BaseException) -> str:
    status = getattr(exc, "status", None)
    reason = getattr(exc, "reason", None)
    if status is not None:
        return f"{status} {reason or ''}".strip()
    return type(exc).__name__


def _status(
    phase: str,
    message: str = "",
    *,
    public_url: str = "",
) -> dict[str, str]:
    host = urlparse(public_url).hostname or "" if public_url else ""
    subdomain = host.split(".", 1)[0] if host else ""
    return {
        "phase": phase,
        "message": message,
        "publicUrl": public_url,
        "subdomain": subdomain,
    }
