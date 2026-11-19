"""Map HLETunnel CRs and ``ingressClassName: hle`` Ingresses to declarations.

Pure functions: no Kubernetes API calls and no state. The registry resolves
visitor basic-auth Secrets and passes the ``user:pass`` value in, so these
stay a table of what a manifest field becomes on the control channel.
"""

from __future__ import annotations

import ipaddress
import logging
import re
from collections.abc import Mapping
from typing import Any

from hle_common.agent_protocol import (
    AllowedIp,
    AllowedUser,
    DeclaredAccess,
    DeclaredEndpoint,
    K8sServiceTarget,
)
from hle_common.tunnel_spec import MAX_RESPONSE_TIMEOUT

logger = logging.getLogger(__name__)

# A tunnel label becomes the left-most subdomain label: DNS-1123, lower-case.
_LABEL_RE = re.compile(r"^[a-z0-9]([-a-z0-9]*[a-z0-9])?$")
_LABEL_MAX = 63

# Aliases are FQDNs, the same shape the relay accepts for a zone.
_HOSTNAME_RE = re.compile(r"^([a-z0-9]([a-z0-9-]*[a-z0-9])?\.)+[a-z]{2,63}$")
_HOSTNAME_MAX = 253

# At most this many visitor IP rules, the live origin address included.
_MAX_ALLOWED_IPS = 20

# Ranges no visitor can ever arrive from: RFC 1918, CGNAT, loopback and
# link-local. Documentation ranges (203.0.113.0/24, ...) are deliberately not
# here: they are public space, so a declared one is not inert.
_V4_PRIVATE: tuple[ipaddress.IPv4Network, ...] = (
    ipaddress.IPv4Network("10.0.0.0/8"),
    ipaddress.IPv4Network("172.16.0.0/12"),
    ipaddress.IPv4Network("192.168.0.0/16"),
    ipaddress.IPv4Network("100.64.0.0/10"),
    ipaddress.IPv4Network("127.0.0.0/8"),
    ipaddress.IPv4Network("169.254.0.0/16"),
)
_V6_PRIVATE: tuple[ipaddress.IPv6Network, ...] = (
    ipaddress.IPv6Network("::1/128"),
    ipaddress.IPv6Network("fc00::/7"),
    ipaddress.IPv6Network("fe80::/10"),
)

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


def _response_timeout(raw: Any, field_name: str) -> int | None:
    """Parse an optional response timeout (seconds) from a CRD field or annotation.

    Missing/empty means unset (``None``) so the relay default applies. An int or
    a numeric string is accepted; booleans, other types and values outside
    ``1..MAX_RESPONSE_TIMEOUT`` raise :class:`DeclarationError` naming the field.
    """
    if raw is None:
        return None
    if isinstance(raw, bool):
        raise DeclarationError(
            f"invalid {field_name} {raw!r}: must be an integer 1..{MAX_RESPONSE_TIMEOUT}"
        )
    if isinstance(raw, str):
        raw = raw.strip()
        if not raw:
            return None
        try:
            value = int(raw)
        except ValueError as exc:
            raise DeclarationError(
                f"invalid {field_name} {raw!r}: must be an integer 1..{MAX_RESPONSE_TIMEOUT}"
            ) from exc
    elif isinstance(raw, int):
        value = raw
    else:
        raise DeclarationError(
            f"invalid {field_name} {raw!r}: must be an integer 1..{MAX_RESPONSE_TIMEOUT}"
        )
    if not 1 <= value <= MAX_RESPONSE_TIMEOUT:
        raise DeclarationError(
            f"invalid {field_name} {raw!r}: must be an integer 1..{MAX_RESPONSE_TIMEOUT}"
        )
    return value


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


def _allowed_ip(entry: str, label: str = "") -> AllowedIp:
    """One ``{cidr, label}`` visitor rule, validated and canonicalised.

    A bare address becomes its host network (``/32`` or ``/128``); a bracketed
    IPv6 address is accepted. ``/0`` is refused because it would match every
    visitor — that is what ``authMode: none`` is for.
    """
    candidate = entry.strip()
    if candidate.startswith("[") and candidate.endswith("]"):
        candidate = candidate[1:-1]
    try:
        network = ipaddress.ip_network(candidate, strict=False)
    except ValueError as exc:
        raise DeclarationError(f"invalid allowed IP {entry!r}") from exc
    if network.prefixlen == 0:
        raise DeclarationError(
            f"invalid allowed IP {entry!r}: /0 would match every visitor; "
            "use authMode: none to make the tunnel public"
        )
    return AllowedIp(cidr=str(network), label=label)


def _parse_annotation_ips(raw: str) -> list[AllowedIp]:
    """``cidr,cidr`` annotation -> allowed IP rules, deduplicated in order."""
    ips: list[AllowedIp] = []
    seen: set[str] = set()
    for entry in raw.split(","):
        entry = entry.strip()
        if not entry:
            continue
        allowed_ip = _allowed_ip(entry)
        if allowed_ip.cidr in seen:
            continue
        seen.add(allowed_ip.cidr)
        ips.append(allowed_ip)
    return ips


def _crd_allowed_ips(ac: Mapping[str, Any]) -> list[AllowedIp]:
    """``accessControl.allowedIps`` -> allowed IP rules, deduplicated in order."""
    ips: list[AllowedIp] = []
    seen: set[str] = set()
    for item in ac.get("allowedIps") or []:
        if not isinstance(item, Mapping) or not item.get("cidr"):
            continue
        allowed_ip = _allowed_ip(str(item["cidr"]), str(item.get("label") or ""))
        if allowed_ip.cidr in seen:
            continue
        seen.add(allowed_ip.cidr)
        ips.append(allowed_ip)
    return ips


def _is_private_cidr(cidr: str) -> bool:
    """True when no visitor can ever arrive from *cidr* (entirely private space)."""
    network = ipaddress.ip_network(cidr, strict=False)
    if isinstance(network, ipaddress.IPv4Network):
        return any(network.subnet_of(private) for private in _V4_PRIVATE)
    return any(network.subnet_of(private) for private in _V6_PRIVATE)


def _build_access(
    allowed_users: list[AllowedUser],
    pin: str | None,
    basic_auth: str | None,
    allowed_ips: list[AllowedIp] | None = None,
    allow_origin_ip: bool = False,
    *,
    auth_mode: str = "sso",
) -> DeclaredAccess | None:
    """An access policy with something in it, or None (leave/clear, see strict)."""
    ips = list(allowed_ips or [])
    if len(ips) + (1 if allow_origin_ip else 0) > _MAX_ALLOWED_IPS:
        raise DeclarationError(
            f"too many allowed IPs: at most {_MAX_ALLOWED_IPS} entries, "
            "including the live origin address"
        )
    for allowed_ip in ips:
        if _is_private_cidr(allowed_ip.cidr):
            logger.warning(
                "allowed IP %s is a private range; ignored by relay: "
                "private range cannot match a visitor",
                allowed_ip.cidr,
            )
    if auth_mode == "none" and (ips or allow_origin_ip):
        logger.warning(
            "authMode: none makes the tunnel public; the allowedIps list is "
            "not consulted while the tunnel is public"
        )
    if not allowed_users and not pin and not basic_auth and not ips and not allow_origin_ip:
        return None
    try:
        return DeclaredAccess(
            allowed_users=allowed_users,
            pin=pin,
            basic_auth=basic_auth,
            allowed_ips=ips,
            allow_origin_ip=allow_origin_ip,
        )
    except ValueError as exc:
        raise DeclarationError(f"invalid access control: {exc}") from exc


def _zone_fields(zone: Any, apex: Any) -> dict[str, Any]:
    """``zone``/``apex`` for a declaration: lower-case zone, '' -> base domain."""
    zone_s = str(zone or "").strip().lower().rstrip(".") or None
    apex_b = bool(apex)
    if apex_b and not zone_s:
        raise DeclarationError("apex requires zone")
    return {"zone": zone_s, "apex": apex_b}


def _aliases_option(raw: Any) -> dict[str, str]:
    """The ``options["aliases"]`` entry for a declaration, or {} when there are none.

    Accepts a CRD list or a comma-separated annotation: entries are stripped,
    lower-cased, have a trailing dot dropped and duplicates removed, keeping
    order. Each must be an FQDN, the same shape the relay accepts for a zone.
    """
    if raw is None:
        entries: list[Any] = []
    elif isinstance(raw, str):
        entries = raw.split(",")
    else:
        entries = list(raw)

    aliases: list[str] = []
    for entry in entries:
        alias = str(entry).strip().lower().rstrip(".")
        if not alias:
            continue
        if len(alias) > _HOSTNAME_MAX or not _HOSTNAME_RE.match(alias):
            raise DeclarationError(
                f"invalid alias {entry!r}: must be a hostname like app.example.com"
            )
        if alias not in aliases:
            aliases.append(alias)
    return {"aliases": ",".join(aliases)} if aliases else {}


def _crd_target(
    spec: Mapping[str, Any], namespace: str, *, allow_cross_namespace: bool = False
) -> K8sServiceTarget:
    ref = spec.get("serviceRef") or {}
    name = ref.get("name") or ""
    if not name:
        raise DeclarationError("serviceRef.name is required")
    target_namespace = ref.get("namespace") or namespace
    if target_namespace != namespace and not allow_cross_namespace:
        raise DeclarationError(
            f"serviceRef.namespace {target_namespace!r} differs from the HLETunnel namespace "
            f"{namespace!r}; cross-namespace references are disabled"
        )
    try:
        return K8sServiceTarget(
            namespace=target_namespace,
            name=name,
            port=ref.get("port", 80),
            scheme=ref.get("protocol", "http"),
        )
    except ValueError as exc:
        raise DeclarationError(f"invalid serviceRef: {exc}") from exc


def normalise_secret_ref(ref: str, namespace: str, field: str = "upstreamBasicAuthSecret") -> str:
    """``name#key`` or ``namespace/name#key`` -> ``namespace/name#key``.

    A Secret is only ever read from the declaring object's own namespace: a
    reference to another namespace would let whoever writes the manifest make
    the operator read, and forward, a Secret they cannot read themselves.
    """
    path, sep, key = ref.partition("#")
    if not sep or not key:
        raise DeclarationError(f"{field} must be 'name#key'")
    ref_namespace, slash, name = path.rpartition("/")
    if not slash:
        ref_namespace = namespace
    if ref_namespace != namespace:
        raise DeclarationError(
            f"{field} namespace {ref_namespace!r} differs from the "
            f"HLETunnel namespace {namespace!r}; Secrets are read from its own namespace"
        )
    if not name:
        raise DeclarationError(f"{field} must be 'name#key'")
    return f"{namespace}/{name}#{key}"


def crd_pin_secret_ref(body: Mapping[str, Any]) -> str | None:
    """``namespace/name#key`` of the Secret holding the visitor PIN, if any.

    ``accessControl.pinSecretRef`` is ``{name, key}``; ``key`` defaults to
    ``pin``. The Secret is always in the HLETunnel's own namespace.
    """
    meta = body.get("metadata") or {}
    namespace = meta.get("namespace") or "default"
    ac = (body.get("spec") or {}).get("accessControl") or {}
    ref = ac.get("pinSecretRef")
    if not ref:
        return None
    name = str(ref.get("name") or "") if isinstance(ref, Mapping) else ""
    if not name:
        raise DeclarationError("accessControl.pinSecretRef.name is required")
    key = str(ref.get("key") or "pin")
    return f"{namespace}/{name}#{key}"


def ingress_pin_secret_ref(body: Mapping[str, Any]) -> str | None:
    """``namespace/name#key`` from the ``hle.world/pin-secret`` annotation (``name[#key]``)."""
    meta = body.get("metadata") or {}
    namespace = meta.get("namespace") or "default"
    raw = ((meta.get("annotations") or {}).get("hle.world/pin-secret") or "").strip()
    if not raw:
        return None
    if "#" not in raw:
        raw = f"{raw}#pin"
    return normalise_secret_ref(raw, namespace, "hle.world/pin-secret")


def _pin(inline: Any, secret_ref: str | None, secret_value: str | None) -> str | None:
    """The PIN for a declaration: inline or from a Secret, never both, never missing."""
    if secret_ref is None:
        return inline or None
    if inline:
        raise DeclarationError("set either an inline pin or a pin Secret, not both")
    if not secret_value:
        raise DeclarationError(
            f"pin Secret {secret_ref.split('#', 1)[0]} is missing, unreadable, or has no such key"
        )
    return secret_value


def crd_to_declared(
    body: Mapping[str, Any],
    *,
    basic_auth: str | None = None,
    pin_secret: str | None = None,
    allow_cross_namespace: bool = False,
) -> DeclaredEndpoint:
    """Build the declaration for one HLETunnel body.

    ``basic_auth`` is the resolved visitor Secret as ``user:pass``; the caller
    reads the Secret so this stays pure.
    """
    meta = body.get("metadata") or {}
    namespace = meta.get("namespace") or "default"
    spec = body.get("spec") or {}
    ac = spec.get("accessControl") or {}

    if spec.get("apiKeyRef"):
        raise DeclarationError(
            "apiKeyRef is not supported in agent mode: every tunnel is declared under "
            "the operator's own credential"
        )
    upstream_secret = spec.get("upstreamBasicAuthSecret") or None
    if upstream_secret:
        upstream_secret = normalise_secret_ref(str(upstream_secret), namespace)

    label = normalise_label(spec.get("label") or (spec.get("serviceRef") or {}).get("name", ""))
    auth_mode = spec.get("authMode", "sso")
    try:
        return DeclaredEndpoint(
            label=label,
            target=_crd_target(spec, namespace, allow_cross_namespace=allow_cross_namespace),
            auth_mode=auth_mode,
            websocket_enabled=spec.get("websocketEnabled", True),
            forward_host=spec.get("forwardHost", False),
            verify_ssl=spec.get("verifySSL", False),
            response_timeout=_response_timeout(spec.get("responseTimeout"), "responseTimeout"),
            sync_policy=spec.get("syncPolicy", "strict"),
            source_ref=crd_source_ref(body),
            upstream_basic_auth_secret=upstream_secret,
            access=_build_access(
                _crd_allowed_users(ac),
                _pin(ac.get("pin"), crd_pin_secret_ref(body), pin_secret),
                basic_auth,
                _crd_allowed_ips(ac),
                bool(ac.get("allowOriginIp", False)),
                auth_mode=auth_mode,
            ),
            options=_aliases_option(spec.get("aliases")),
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
    body: Mapping[str, Any], *, basic_auth: str | None = None, pin_secret: str | None = None
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

    auth_mode = annotations.get("hle.world/auth-mode", "sso")
    access = _build_access(
        _parse_annotation_users(annotations.get("hle.world/allowed-users", "")),
        _pin(annotations.get("hle.world/pin"), ingress_pin_secret_ref(body), pin_secret),
        basic_auth,
        _parse_annotation_ips(annotations.get("hle.world/allowed-ips", "")),
        annotations.get("hle.world/allow-origin-ip", "false").lower() == "true",
        auth_mode=auth_mode,
    )
    try:
        return DeclaredEndpoint(
            label=label,
            target=target,
            auth_mode=auth_mode,
            websocket_enabled=annotations.get("hle.world/websocket-enabled", "true").lower()
            == "true",
            forward_host=annotations.get("hle.world/forward-host", "false").lower() == "true",
            verify_ssl=annotations.get("hle.world/verify-ssl", "false").lower() == "true",
            response_timeout=_response_timeout(
                annotations.get("hle.world/response-timeout"), "hle.world/response-timeout"
            ),
            sync_policy=annotations.get("hle.world/sync-policy", "strict"),
            source_ref=ingress_source_ref(body),
            access=access,
            options=_aliases_option(annotations.get("hle.world/aliases")),
            **_zone_fields(
                annotations.get("hle.world/zone"),
                annotations.get("hle.world/apex", "false").lower() == "true",
            ),
        )
    except ValueError as exc:
        raise DeclarationError(str(exc)) from exc
