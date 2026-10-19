"""Kopf handlers for HLETunnel CRDs and annotated Ingress resources."""

from __future__ import annotations

import asyncio
import base64
import logging
from collections.abc import Mapping
from typing import Any

import kopf
import kubernetes.client

from hle_operator.config import allow_cross_namespace, get_api_key, load_api_key, load_config
from hle_operator.tunnel_manager import (
    ManagedTunnel,
    ManagedTunnelSpec,
    RefusedCallback,
    SpecError,
    build_service_url,
    parse_crd_spec,
    parse_ingress_annotations,
)

logger = logging.getLogger(__name__)

# Registry of active tunnels keyed by "namespace/name".
_tunnels: dict[str, ManagedTunnel] = {}

# Ingress-managed tunnels keyed by "namespace/ingress-name".
_ingress_tunnels: dict[str, ManagedTunnel] = {}


# ---------------------------------------------------------------------------
# Startup / shutdown
# ---------------------------------------------------------------------------


@kopf.on.startup()
async def startup(settings: kopf.OperatorSettings, **kwargs) -> None:
    settings.persistence.finalizer = "hle.world/operator-finalizer"
    settings.persistence.progress_storage = kopf.AnnotationsProgressStorage(prefix="hle.world")
    settings.persistence.diffbase_storage = kopf.AnnotationsDiffBaseStorage(prefix="hle.world")

    load_config()
    load_api_key()

    api_key = get_api_key()
    if not api_key:
        logger.error("No HLE API key configured — tunnels will fail to connect")


@kopf.on.cleanup()
async def cleanup(**kwargs) -> None:
    logger.info("Shutting down — stopping all tunnels")
    tasks = []
    for tunnel in list(_tunnels.values()):
        tasks.append(tunnel.stop())
    for tunnel in list(_ingress_tunnels.values()):
        tasks.append(tunnel.stop())
    if tasks:
        await asyncio.gather(*tasks, return_exceptions=True)
    _tunnels.clear()
    _ingress_tunnels.clear()


# ---------------------------------------------------------------------------
# HLETunnel CRD handlers
# ---------------------------------------------------------------------------


@kopf.on.create("hletunnels", group="hle.world", version="v1alpha1")
@kopf.on.resume("hletunnels", group="hle.world", version="v1alpha1")
async def on_tunnel_create(
    spec: kopf.Spec,
    name: str,
    namespace: str | None,
    patch: kopf.Patch,
    logger: logging.Logger,
    **kwargs,
) -> dict[str, str]:
    ns = namespace or "default"
    key = f"{ns}/{name}"

    # Stop existing tunnel if resuming
    existing = _tunnels.pop(key, None)
    if existing:
        await existing.stop()

    try:
        managed_spec = parse_crd_spec(dict(spec), ns, allow_cross_namespace=allow_cross_namespace())
        _resolve_basic_auth(managed_spec, spec, ns)
    except SpecError as exc:
        patch.status["phase"] = "Failed"
        patch.status["message"] = str(exc)
        logger.error("HLETunnel %s refused: %s", key, exc)
        return {"phase": "Failed"}

    # Per-tunnel API key override
    api_key_ref = spec.get("apiKeyRef")
    if api_key_ref:
        api_key = (
            _read_secret_key(api_key_ref["name"], ns, api_key_ref.get("key", "api-key"))
            or get_api_key()
        )
    else:
        api_key = get_api_key()

    if not api_key:
        patch.status["phase"] = "Failed"
        patch.status["message"] = "No HLE API key configured"
        return {"phase": "Failed"}

    tunnel = ManagedTunnel(
        spec=managed_spec,
        api_key=api_key,
        logger=logger,
        on_refused=_crd_refused(ns, name, key),
    )
    _tunnels[key] = tunnel

    patch.status["phase"] = "Pending"
    patch.status["message"] = "Starting tunnel"
    try:
        await tunnel.start()
    except SpecError as exc:
        _tunnels.pop(key, None)
        patch.status["phase"] = "Failed"
        patch.status["message"] = str(exc)
        logger.error("HLETunnel %s refused: %s", key, exc)
        return {"phase": "Failed"}

    # Wait for connection
    connected = await tunnel.wait_connected(timeout=30.0)
    if connected:
        patch.status["phase"] = "Connected"
        patch.status["publicUrl"] = tunnel.public_url or ""
        patch.status["subdomain"] = tunnel.subdomain or ""
        patch.status["message"] = ""
        return {"phase": "Connected"}

    patch.status["phase"] = "Pending"
    patch.status["message"] = "Connecting (tunnel registered but not yet confirmed)"
    return {"phase": "Pending"}


@kopf.on.update("hletunnels", group="hle.world", version="v1alpha1")
async def on_tunnel_update(
    spec: kopf.Spec,
    old: kopf.BodyEssence,
    new: kopf.BodyEssence,
    diff: kopf.Diff,
    name: str,
    namespace: str | None,
    patch: kopf.Patch,
    logger: logging.Logger,
    **kwargs,
) -> dict[str, str]:
    ns = namespace or "default"
    key = f"{ns}/{name}"

    # Check if only access control changed (no tunnel restart needed)
    tunnel_config_changed = any(
        field[1]
        and len(field[1]) >= 2
        and field[1][0] == "spec"
        and field[1][1] not in ("accessControl",)
        for field in diff
    )

    existing = _tunnels.get(key)

    if not tunnel_config_changed and existing and existing.is_connected:
        # Only access control changed — reconcile without restarting tunnel
        try:
            managed_spec = parse_crd_spec(
                dict(spec), ns, allow_cross_namespace=allow_cross_namespace()
            )
            _resolve_basic_auth(managed_spec, spec, ns)
        except SpecError as exc:
            # A refused update must not leave the tunnel running on its old
            # access control: stop it and forget it.
            _tunnels.pop(key, None)
            await existing.stop()
            patch.status["phase"] = "Failed"
            patch.status["message"] = str(exc)
            logger.error("HLETunnel %s refused: %s", key, exc)
            return {"phase": "Failed"}
        existing._spec = managed_spec
        await existing.reconcile_access_control()
        patch.status["message"] = "Access control updated"
        logger.info("Reconciled access control without tunnel restart")
        return {"phase": "Connected"}

    # Full tunnel restart
    if existing:
        await existing.stop()
        _tunnels.pop(key, None)

    # Reuse the create handler logic
    return await on_tunnel_create(
        spec=spec,
        name=name,
        namespace=namespace,
        patch=patch,
        logger=logger,
        **kwargs,
    )


@kopf.on.delete("hletunnels", group="hle.world", version="v1alpha1")
async def on_tunnel_delete(
    name: str,
    namespace: str | None,
    logger: logging.Logger,
    **kwargs,
) -> None:
    ns = namespace or "default"
    key = f"{ns}/{name}"
    tunnel = _tunnels.pop(key, None)
    if tunnel:
        await tunnel.stop()
        logger.info("Tunnel stopped: %s", key)


# ---------------------------------------------------------------------------
# Periodic reconciliation (strict sync policy)
# ---------------------------------------------------------------------------


@kopf.timer("hletunnels", group="hle.world", version="v1alpha1", interval=60)
async def reconcile_access(
    spec: kopf.Spec,
    name: str,
    namespace: str | None,
    patch: kopf.Patch,
    logger: logging.Logger,
    **kwargs,
) -> None:
    ns = namespace or "default"
    key = f"{ns}/{name}"
    tunnel = _tunnels.get(key)

    if not tunnel or not tunnel.is_connected:
        return

    sync_policy = spec.get("syncPolicy", "strict")
    if sync_policy != "strict":
        return

    await tunnel.reconcile_access_control()

    # Update status with current connection info
    if tunnel.is_connected:
        patch.status["phase"] = "Connected"
        patch.status["publicUrl"] = tunnel.public_url or ""
        patch.status["subdomain"] = tunnel.subdomain or ""
    else:
        patch.status["phase"] = "Disconnected"
        patch.status["message"] = "Tunnel lost connection, reconnecting..."


# ---------------------------------------------------------------------------
# Ingress handlers (ingressClassName: hle)
# ---------------------------------------------------------------------------


@kopf.on.create("ingresses", group="networking.k8s.io")
@kopf.on.resume("ingresses", group="networking.k8s.io")
async def on_ingress_create(
    body: kopf.Body,
    spec: kopf.Spec,
    meta: kopf.Meta,
    name: str,
    namespace: str | None,
    logger: logging.Logger,
    **kwargs,
) -> dict[str, str] | None:
    ingress_class = spec.get("ingressClassName", "")
    if ingress_class != "hle":
        return None

    ns = namespace or "default"
    key = f"{ns}/{name}"
    annotations = dict(meta.get("annotations") or {})
    rules = list(spec.get("rules") or [])

    managed_spec = parse_ingress_annotations(annotations, rules)
    if not managed_spec:
        logger.warning("Ingress %s has ingressClassName=hle but no valid rules", name)
        return None

    # Build service URL from the Ingress backend + namespace
    rule = rules[0]
    paths = rule.get("http", {}).get("paths", [])
    backend = paths[0].get("backend", {})
    svc = backend.get("service", {})
    svc_name = svc.get("name", "")
    svc_port = svc.get("port", {}).get("number", 80)
    protocol = annotations.get("hle.world/protocol", "http")
    try:
        # Validated field by field: the annotation is a scheme, not a URL.
        managed_spec.service_url = build_service_url(ns, svc_name, svc_port, protocol)

        # Resolve basicAuth Secret if specified
        ba_secret_name = annotations.get("hle.world/basic-auth-secret")
        if ba_secret_name:
            managed_spec.access_control.basic_auth = _require_basic_auth(ba_secret_name, ns)

        pin_secret = (annotations.get("hle.world/pin-secret") or "").strip()
        if pin_secret:
            pin_name, _, pin_key = pin_secret.partition("#")
            _resolve_pin(managed_spec, pin_name, pin_key or "pin", ns)
    except SpecError as exc:
        logger.error("Ingress %s refused: %s", key, exc)
        existing = _ingress_tunnels.pop(key, None)
        if existing:
            await existing.stop()
        return {"phase": "Failed", "message": str(exc)}

    # Stop existing if resuming
    existing = _ingress_tunnels.pop(key, None)
    if existing:
        await existing.stop()

    api_key = get_api_key()
    if not api_key:
        logger.error("No HLE API key — cannot create tunnel for Ingress %s", name)
        return {"phase": "Failed"}

    tunnel = ManagedTunnel(
        spec=managed_spec,
        api_key=api_key,
        logger=logger,
        on_refused=_ingress_refused(body, key),
    )
    _ingress_tunnels[key] = tunnel
    try:
        await tunnel.start()
    except SpecError as exc:
        _ingress_tunnels.pop(key, None)
        logger.error("Ingress %s refused: %s", key, exc)
        return {"phase": "Failed", "message": str(exc)}

    connected = await tunnel.wait_connected(timeout=30.0)
    if connected:
        logger.info("Ingress tunnel connected: %s -> %s", name, tunnel.public_url)
        return {"phase": "Connected", "publicUrl": tunnel.public_url or ""}

    return {"phase": "Pending"}


@kopf.on.update("ingresses", group="networking.k8s.io")
async def on_ingress_update(
    body: kopf.Body,
    spec: kopf.Spec,
    meta: kopf.Meta,
    name: str,
    namespace: str | None,
    logger: logging.Logger,
    **kwargs,
) -> dict[str, str] | None:
    ingress_class = spec.get("ingressClassName", "")
    if ingress_class != "hle":
        # If it was previously managed, stop the tunnel
        ns = namespace or "default"
        key = f"{ns}/{name}"
        existing = _ingress_tunnels.pop(key, None)
        if existing:
            await existing.stop()
            logger.info("Ingress %s no longer uses ingressClassName=hle, tunnel stopped", name)
        return None

    # Restart the tunnel with new config
    return await on_ingress_create(
        body=body,
        spec=spec,
        meta=meta,
        name=name,
        namespace=namespace,
        logger=logger,
        **kwargs,
    )


@kopf.on.delete("ingresses", group="networking.k8s.io", optional=True)
async def on_ingress_delete(
    spec: kopf.Spec,
    name: str,
    namespace: str | None,
    logger: logging.Logger,
    **kwargs,
) -> None:
    ns = namespace or "default"
    key = f"{ns}/{name}"
    tunnel = _ingress_tunnels.pop(key, None)
    if tunnel:
        await tunnel.stop()
        logger.info("Ingress tunnel stopped: %s", key)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _crd_refused(namespace: str, name: str, key: str) -> RefusedCallback:
    """Report a running HLETunnel whose target became refused: drop it, status Failed."""

    async def report(tunnel: ManagedTunnel, message: str) -> None:
        if _tunnels.get(key) is tunnel:
            _tunnels.pop(key, None)
        await asyncio.to_thread(
            _patch_crd_status, namespace, name, {"phase": "Failed", "message": message}
        )

    return report


def _ingress_refused(body: Any, key: str) -> RefusedCallback:
    """Same for an Ingress: drop it and surface the reason as an Event."""

    async def report(tunnel: ManagedTunnel, message: str) -> None:
        if _ingress_tunnels.get(key) is tunnel:
            _ingress_tunnels.pop(key, None)
        try:
            kopf.warn(body, reason="TargetRefused", message=message)
        except Exception:  # noqa: BLE001 — the log line is the fallback
            logger.error("Ingress %s refused: %s", key, message)

    return report


def _patch_crd_status(namespace: str, name: str, status: dict[str, str]) -> None:
    try:
        kubernetes.client.CustomObjectsApi().patch_namespaced_custom_object_status(
            "hle.world", "v1alpha1", namespace, "hletunnels", name, {"status": status}
        )
    except kubernetes.client.ApiException:
        logger.exception("Could not patch status of HLETunnel %s/%s", namespace, name)


def _require_basic_auth(secret_name: str, namespace: str) -> tuple[str, str]:
    """Visitor basic auth from a Secret, failing closed.

    A policy that names a Secret is never applied without it: a missing,
    unreadable or incomplete Secret must not leave the tunnel open.
    """
    value = _read_basic_auth_secret(secret_name, namespace)
    if value is None:
        raise SpecError(
            f"basic-auth Secret {namespace}/{secret_name} is missing, unreadable, "
            "or has no username/password"
        )
    return value


def _resolve_basic_auth(
    managed_spec: ManagedTunnelSpec, spec: Mapping[str, Any], namespace: str
) -> None:
    ac = spec.get("accessControl") or {}
    ref = (ac.get("basicAuth") or {}).get("secretRef")
    if ref:
        managed_spec.access_control.basic_auth = _require_basic_auth(ref["name"], namespace)
    pin_ref = ac.get("pinSecretRef")
    if pin_ref:
        _resolve_pin(managed_spec, pin_ref.get("name", ""), pin_ref.get("key") or "pin", namespace)


def _resolve_pin(managed_spec: ManagedTunnelSpec, name: str, key: str, namespace: str) -> None:
    """Visitor PIN from a Secret in the object's own namespace, failing closed."""
    if managed_spec.access_control.pin:
        raise SpecError("set either an inline pin or a pin Secret, not both")
    pin = _read_secret_key(name, namespace, key) if name else None
    if not pin:
        raise SpecError(
            f"pin Secret {namespace}/{name} is missing, unreadable, or has no key {key!r}"
        )
    managed_spec.access_control.pin = pin


def _read_basic_auth_secret(secret_name: str, namespace: str) -> tuple[str, str] | None:
    """Read username/password from a Kubernetes Secret."""
    try:
        v1 = kubernetes.client.CoreV1Api()
        secret = v1.read_namespaced_secret(secret_name, namespace)
        if not secret.data:
            return None
        username = base64.b64decode(secret.data.get("username", "")).decode()
        password = base64.b64decode(secret.data.get("password", "")).decode()
        if username and password:
            return (username, password)
    except kubernetes.client.ApiException:
        logger.exception("Failed to read basic auth Secret %s/%s", namespace, secret_name)
    return None


def _read_secret_key(secret_name: str, namespace: str, key: str) -> str | None:
    """Read a single key from a Kubernetes Secret."""
    try:
        v1 = kubernetes.client.CoreV1Api()
        secret = v1.read_namespaced_secret(secret_name, namespace)
        if secret.data and key in secret.data:
            return base64.b64decode(secret.data[key]).decode()
    except kubernetes.client.ApiException:
        logger.exception("Failed to read Secret %s/%s key=%s", namespace, secret_name, key)
    return None
