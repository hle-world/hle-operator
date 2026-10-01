"""Mirror each declared endpoint's status onto its CR or Ingress.

The agent owns the tunnels and reports status; kopf only writes it back. One
pass every ten seconds reads ``AgentClient.endpoint_statuses()`` (which is
``None`` while this process does not hold the control session) plus the last
``declared_ack``, computes a phase, and patches only what changed. A failed
patch is retried with a per-object backoff, and state for objects that left
the registry is dropped, so neither a broken API nor churn grows anything.
"""

from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import Protocol
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


class StatusClient(Protocol):
    def endpoint_statuses(self) -> list[EndpointStatus] | None: ...


class StatusPatcher(Protocol):
    """Where a computed status goes. Injected so tests never touch the API."""

    def patch_cr(self, namespace: str, name: str, status: dict[str, str]) -> None: ...

    def patch_ingress(self, namespace: str, name: str, status: dict[str, str]) -> None: ...


class KubernetesStatusPatcher:
    """Write status to the real API: CR status subresource and Ingress status."""

    def patch_cr(self, namespace: str, name: str, status: dict[str, str]) -> None:
        api = kubernetes.client.CustomObjectsApi()
        api.patch_namespaced_custom_object_status(
            CUSTOM_GROUP, CUSTOM_VERSION, namespace, CUSTOM_PLURAL, name, {"status": status}
        )

    def patch_ingress(self, namespace: str, name: str, status: dict[str, str]) -> None:
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
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._client = client
        self._registry = registry
        self._interval = interval
        self._patcher = patcher or KubernetesStatusPatcher()
        self._clock = clock
        # source_ref -> (uid, last status written). Bounded by the registry.
        self._last: dict[str, tuple[str, dict[str, str]]] = {}
        self._retry: dict[str, _Retry] = {}

    async def run(self) -> None:
        while True:
            await asyncio.sleep(self._interval)
            try:
                await self.publish_once()
            except Exception:  # noqa: BLE001 — status must never take the process down
                logger.exception("Status publish failed")

    async def publish_once(self) -> None:
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

        now = self._clock()
        for target in targets:
            computed = self._compute(target, by_label)
            last = self._last.get(target.source_ref)
            if last == (target.uid, computed):
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

    async def _patch(self, source_ref: str, status: dict[str, str]) -> bool:
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
