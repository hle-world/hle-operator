"""Map HLETunnel CRs and ``ingressClassName: hle`` Ingresses to declarations.

Pure functions: no Kubernetes API calls and no state. The registry resolves
visitor basic-auth Secrets and passes the ``user:pass`` value in, so these
stay a table of what a manifest field becomes on the control channel.
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from typing import Any

from hle_common.agent_protocol import (
    AllowedUser,
    DeclaredAccess,
    DeclaredEndpoint,
    K8sServiceTarget,
)

# A tunnel label becomes the left-most subdomain label: DNS-1123, lower-case.
_LABEL_RE = re.compile(r"^[a-z0-9]([-a-z0-9]*[a-z0-9])?$")
_LABEL_MAX = 63

CRD_KIND = "hletunnel"
INGRESS_KIND = "ingress"
INGRESS_CLASS = "hle"
# Written back by the status loop; never an input to a declaration.
PUBLIC_URL_ANNOTATION = "hle.world/public-url"


class DeclarationError(ValueError):
    """A manifest cannot become a declaration.

    The message is meant for CR ``.status.message``: it names the field at
    fault, never a Secret value.
    """


def normalise_label(raw: str) -> str:
    """Lower-case and validate a label, raising :class:`DeclarationError`."""
    label = (raw or "").strip().lower()
    if not label or len(label) > _LABEL_MAX or not _LABEL_RE.match(label):
        raise DeclarationError(f"invalid label {raw!r}: must be a lower-case DNS label")
    return label


def crd_source_ref(body: Mapping[str, Any]) -> str:
    meta = body.get("metadata") or {}
    return f"{CRD_KIND}:{meta.get('namespace') or 'default'}/{meta.get('name') or ''}"


def ingress_source_ref(body: Mapping[str, Any]) -> str:
    meta = body.get("metadata") or {}
    return f"{INGRESS_KIND}:{meta.get('namespace') or 'default'}/{meta.get('name') or ''}"


def crd_visitor_secret_ref(body: Mapping[str, Any]) -> tuple[str, str] | None:
    """``(namespace, name)`` of the visitor basic-auth Secret, if any.

    The CRD's ``accessControl.basicAuth.secretRef`` has no namespace of its
    own; it is always the CR's namespace.
    """
    meta = body.get("metadata") or {}
    namespace = meta.get("namespace") or "default"
    ac = (body.get("spec") or {}).get("accessControl") or {}
    name = ((ac.get("basicAuth") or {}).get("secretRef") or {}).get("name") or ""
    return (namespace, name) if name else None


def ingress_visitor_secret_ref(body: Mapping[str, Any]) -> tuple[str, str] | None:
    meta = body.get("metadata") or {}
    namespace = meta.get("namespace") or "default"
    annotations = meta.get("annotations") or {}
    name = (annotations.get("hle.world/basic-auth-secret") or "").strip()
    return (namespace, name) if name else None


def _parse_annotation_users(raw: str) -> list[AllowedUser]:
    """``email:provider,email`` annotation -> allowed users."""
    users: list[AllowedUser] = []
    for entry in raw.split(","):
        entry = entry.strip()
        if not entry:
            continue
        if ":" in entry:
            email, provider = entry.rsplit(":", 1)
            users.append(_allowed_user(email.strip(), provider.strip()))
        else:
            users.append(_allowed_user(entry, "any"))
    return users


def _allowed_user(email: str, provider: str) -> AllowedUser:
    try:
        return AllowedUser(email=email, provider=provider or "any")
    except ValueError as exc:
        raise DeclarationError(f"invalid allowed user {email!r}: {exc}") from exc


def _crd_allowed_users(ac: Mapping[str, Any]) -> list[AllowedUser]:
    users: list[AllowedUser] = []
    for item in ac.get("allowedUsers") or []:
        if not isinstance(item, Mapping) or not item.get("email"):
            continue
        users.append(_allowed_user(str(item["email"]), str(item.get("provider") or "any")))
    return users


def _build_access(
    allowed_users: list[AllowedUser],
    pin: str | None,
    basic_auth: str | None,
) -> DeclaredAccess | None:
    """An access policy with something in it, or None (leave/clear, see strict)."""
    if not allowed_users and not pin and not basic_auth:
        return None
    try:
        return DeclaredAccess(allowed_users=allowed_users, pin=pin, basic_auth=basic_auth)
    except ValueError as exc:
        raise DeclarationError(f"invalid access control: {exc}") from exc


def _zone_fields(zone: Any, apex: Any) -> dict[str, Any]:
    """``zone``/``apex`` for a declaration: lower-case zone, '' -> base domain."""
    zone_s = str(zone or "").strip().lower().rstrip(".") or None
    apex_b = bool(apex)
    if apex_b and not zone_s:
        raise DeclarationError("apex requires zone")
    return {"zone": zone_s, "apex": apex_b}


def _crd_target(spec: Mapping[str, Any], namespace: str) -> K8sServiceTarget:
    ref = spec.get("serviceRef") or {}
    name = ref.get("name") or ""
    if not name:
        raise DeclarationError("serviceRef.name is required")
    try:
        return K8sServiceTarget(
            namespace=ref.get("namespace") or namespace,
            name=name,
            port=ref.get("port", 80),
            scheme=ref.get("protocol", "http"),
        )
    except ValueError as exc:
        raise DeclarationError(f"invalid serviceRef: {exc}") from exc


def crd_to_declared(body: Mapping[str, Any], *, basic_auth: str | None = None) -> DeclaredEndpoint:
    """Build the declaration for one HLETunnel body.

    ``basic_auth`` is the resolved visitor Secret as ``user:pass``; the caller
    reads the Secret so this stays pure.
    """
    meta = body.get("metadata") or {}
    namespace = meta.get("namespace") or "default"
    spec = body.get("spec") or {}
    ac = spec.get("accessControl") or {}

    label = normalise_label(spec.get("label") or (spec.get("serviceRef") or {}).get("name", ""))
    try:
        return DeclaredEndpoint(
            label=label,
            target=_crd_target(spec, namespace),
            auth_mode=spec.get("authMode", "sso"),
            websocket_enabled=spec.get("websocketEnabled", True),
            forward_host=spec.get("forwardHost", False),
            verify_ssl=spec.get("verifySSL", False),
            sync_policy=spec.get("syncPolicy", "strict"),
            source_ref=crd_source_ref(body),
            upstream_basic_auth_secret=spec.get("upstreamBasicAuthSecret") or None,
            access=_build_access(_crd_allowed_users(ac), ac.get("pin"), basic_auth),
            **_zone_fields(spec.get("zone"), spec.get("apex", False)),
        )
    except ValueError as exc:
        # A malformed future CRD field must go to CR status, not kill the loop.
        raise DeclarationError(str(exc)) from exc


def _ingress_backend(spec: Mapping[str, Any]) -> tuple[str, int | str]:
    rules = spec.get("rules") or []
    if not rules:
        raise DeclarationError("ingress has no rules")
    paths = (rules[0].get("http") or {}).get("paths") or []
    if not paths:
        raise DeclarationError("ingress first rule has no paths")
    service = (paths[0].get("backend") or {}).get("service") or {}
    name = service.get("name") or ""
    if not name:
        raise DeclarationError("ingress backend service name is required")
    port = (service.get("port") or {}).get("number")
    if port is None:
        port = (service.get("port") or {}).get("name")
    if port is None:
        raise DeclarationError("ingress backend service port is required")
    return name, port


def ingress_to_declared(
    body: Mapping[str, Any], *, basic_auth: str | None = None
) -> DeclaredEndpoint:
    """Build the declaration for one ``ingressClassName: hle`` Ingress body."""
    meta = body.get("metadata") or {}
    namespace = meta.get("namespace") or "default"
    annotations = meta.get("annotations") or {}
    spec = body.get("spec") or {}

    service_name, port = _ingress_backend(spec)
    label = normalise_label(annotations.get("hle.world/label") or service_name)
    try:
        target = K8sServiceTarget(
            namespace=namespace,
            name=service_name,
            port=port,
            scheme=annotations.get("hle.world/protocol", "http"),
        )
    except ValueError as exc:
        raise DeclarationError(f"invalid ingress backend: {exc}") from exc

    access = _build_access(
        _parse_annotation_users(annotations.get("hle.world/allowed-users", "")),
        annotations.get("hle.world/pin"),
        basic_auth,
    )
    try:
        return DeclaredEndpoint(
            label=label,
            target=target,
            auth_mode=annotations.get("hle.world/auth-mode", "sso"),
            websocket_enabled=annotations.get("hle.world/websocket-enabled", "true").lower()
            == "true",
            forward_host=annotations.get("hle.world/forward-host", "false").lower() == "true",
            verify_ssl=annotations.get("hle.world/verify-ssl", "false").lower() == "true",
            sync_policy=annotations.get("hle.world/sync-policy", "strict"),
            source_ref=ingress_source_ref(body),
            access=access,
            **_zone_fields(
                annotations.get("hle.world/zone"),
                annotations.get("hle.world/apex", "false").lower() == "true",
            ),
        )
    except ValueError as exc:
        raise DeclarationError(str(exc)) from exc
