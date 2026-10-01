"""Agent-mode kopf handlers: watch, declare, mirror status, never finalize.

Only ``on.event`` handlers are registered. Agent mode writes no finalizers and
no progress annotations; it only reads cluster objects and, on sight, removes
the finalizer legacy mode used to pin. The actual tunnels run inside
``AgentClient``, so nothing here starts or stops one.
"""

from __future__ import annotations

import logging
from collections.abc import Mapping
from typing import Any

import kopf

from hle_operator.declared import (
    INGRESS_CLASS,
    crd_source_ref,
    ingress_source_ref,
)
from hle_operator.registry import DeclarationRegistry

logger = logging.getLogger(__name__)

DELETED = "DELETED"


def _deleting(body: Mapping[str, Any]) -> bool:
    return bool((body.get("metadata") or {}).get("deletionTimestamp"))


async def handle_crd_event(
    registry: DeclarationRegistry, event_type: str, body: Mapping[str, Any]
) -> None:
    source_ref = crd_source_ref(body)
    if event_type != DELETED:
        # Legacy finalizers would otherwise hang a delete now that no delete
        # handler runs to remove them.
        await registry.strip_legacy_finalizer(body)
    if event_type == DELETED or _deleting(body):
        registry.remove(source_ref)
        logger.info("HLETunnel removed: %s", source_ref)
        return
    await registry.apply_crd(body)


async def handle_ingress_event(
    registry: DeclarationRegistry, event_type: str, body: Mapping[str, Any]
) -> None:
    source_ref = ingress_source_ref(body)
    spec = body.get("spec") or {}
    if event_type == DELETED or _deleting(body) or spec.get("ingressClassName") != INGRESS_CLASS:
        registry.remove(source_ref)
        return
    await registry.apply_ingress(body)


def register(registry: DeclarationRegistry, kopf_registry: kopf.OperatorRegistry) -> None:
    """Register the on.event handlers into *kopf_registry*.

    A registry of its own rather than kopf's default one, so agent mode runs
    exactly these handlers and nothing a stray import added.
    """

    @kopf.on.event("hletunnels", group="hle.world", version="v1alpha1", registry=kopf_registry)
    async def _on_crd_event(event: Mapping[str, Any], body: Mapping[str, Any], **_: Any) -> None:
        await handle_crd_event(registry, str(event.get("type", "")), body)

    @kopf.on.event("ingresses", group="networking.k8s.io", version="v1", registry=kopf_registry)
    async def _on_ingress_event(
        event: Mapping[str, Any], body: Mapping[str, Any], **_: Any
    ) -> None:
        await handle_ingress_event(registry, str(event.get("type", "")), body)
