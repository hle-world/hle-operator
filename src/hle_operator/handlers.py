"""Kopf handlers for HLETunnel CRDs and annotated Ingress resources."""

from __future__ import annotations

import asyncio
import base64
import logging

import kopf
import kubernetes.client

from hle_operator.config import get_api_key, load_api_key, load_config
from hle_operator.tunnel_manager import (
    ManagedTunnel,
    ManagedTunnelSpec,
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
async def startup(settings: kopf.OperatorSettings, **kwargs) -> None:  # type: ignore[no-untyped-def]
    settings.persistence.finalizer = "hle.world/operator-finalizer"
    settings.persistence.progress_storage = kopf.AnnotationsProgressStorage(
        prefix="hle.world"
    )
    settings.persistence.diffbase_storage = kopf.AnnotationsDiffBaseStorage(
        prefix="hle.world"
    )

    load_config()
    load_api_key()

    api_key = get_api_key()
    if not api_key:
        logger.error("No HLE API key configured — tunnels will fail to connect")


@kopf.on.cleanup()
async def cleanup(**kwargs) -> None:  # type: ignore[no-untyped-def]
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

    managed_spec = parse_crd_spec(dict(spec))

    # Resolve basicAuth Secret if specified
    ba_secret = (spec.get("accessControl") or {}).get("basicAuth", {}).get("secretRef")
    if ba_secret:
        managed_spec.access_control.basic_auth = _read_basic_auth_secret(
            ba_secret["name"], ns
        )

    # Per-tunnel API key override
    api_key_ref = spec.get("apiKeyRef")
    if api_key_ref:
        api_key = _read_secret_key(
            api_key_ref["name"], ns, api_key_ref.get("key", "api-key")
        ) or get_api_key()
    else:
        api_key = get_api_key()

    if not api_key:
        patch.status["phase"] = "Failed"
        patch.status["message"] = "No HLE API key configured"
        return {"phase": "Failed"}

    tunnel = ManagedTunnel(spec=managed_spec, api_key=api_key, logger=logger)
    _tunnels[key] = tunnel

    patch.status["phase"] = "Pending"
    patch.status["message"] = "Starting tunnel"
    await tunnel.start()

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
        managed_spec = parse_crd_spec(dict(spec))
        ba_secret = (
            (spec.get("accessControl") or {}).get("basicAuth", {}).get("secretRef")
        )
        if ba_secret:
            managed_spec.access_control.basic_auth = _read_basic_auth_secret(
                ba_secret["name"], ns
            )
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
    managed_spec.service_url = f"{protocol}://{svc_name}.{ns}.svc:{svc_port}"

    # Resolve basicAuth Secret if specified
    ba_secret_name = annotations.get("hle.world/basic-auth-secret")
    if ba_secret_name:
        managed_spec.access_control.basic_auth = _read_basic_auth_secret(
            ba_secret_name, ns
        )

    # Stop existing if resuming
    existing = _ingress_tunnels.pop(key, None)
    if existing:
        await existing.stop()

    api_key = get_api_key()
    if not api_key:
        logger.error("No HLE API key — cannot create tunnel for Ingress %s", name)
        return {"phase": "Failed"}

    tunnel = ManagedTunnel(spec=managed_spec, api_key=api_key, logger=logger)
    _ingress_tunnels[key] = tunnel
    await tunnel.start()

    connected = await tunnel.wait_connected(timeout=30.0)
    if connected:
        logger.info(
            "Ingress tunnel connected: %s -> %s", name, tunnel.public_url
        )
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


def _read_basic_auth_secret(
    secret_name: str, namespace: str
) -> tuple[str, str] | None:
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


def _read_secret_key(
    secret_name: str, namespace: str, key: str
) -> str | None:
    """Read a single key from a Kubernetes Secret."""
    try:
        v1 = kubernetes.client.CoreV1Api()
        secret = v1.read_namespaced_secret(secret_name, namespace)
        if secret.data and key in secret.data:
            return base64.b64decode(secret.data[key]).decode()
    except kubernetes.client.ApiException:
        logger.exception("Failed to read Secret %s/%s key=%s", namespace, secret_name, key)
    return None
