"""Managed tunnel lifecycle — wraps hle-client Tunnel + relay API for access control."""

from __future__ import annotations

import asyncio
import contextlib
import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import urlparse, urlunparse

from hle_client.api import ApiClient, ApiClientConfig
from hle_client.k8s_targets import KubernetesTargetGuard, authority_of
from hle_client.tunnel import Tunnel, TunnelConfig, TunnelFatalError
from hle_common.agent_protocol import K8sServiceTarget


class SpecError(ValueError):
    """A legacy-mode manifest cannot become a tunnel; the message is safe for CR status."""


class TargetRefused(SpecError):  # noqa: N818 — reads better as a noun at call sites
    """The target guard refused the upstream (kube API, metadata, excluded namespace...)."""


def _default_guard() -> KubernetesTargetGuard:
    return KubernetesTargetGuard.from_env()


def build_service_url(namespace: str, name: str, port: int | str, scheme: str) -> str:
    """The in-cluster URL of a Service, validated field by field.

    Every part is checked as a Kubernetes name, port and scheme before it is
    joined, so no field can carry a host, a path or a fragment into the URL.
    """
    try:
        target = K8sServiceTarget(
            namespace=namespace,
            name=name,
            port=port,
            scheme=scheme,  # type: ignore[arg-type]  # validated by K8sServiceTarget
        )
    except ValueError as exc:
        raise SpecError(f"invalid service reference: {exc}") from exc
    return f"{target.scheme}://{target.name}.{target.namespace}.svc:{target.port}"


@dataclass
class AccessRule:
    email: str
    provider: str = "any"


@dataclass
class AccessControlSpec:
    allowed_users: list[AccessRule] = field(default_factory=list)
    pin: str | None = None
    basic_auth: tuple[str, str] | None = None  # (username, password)


@dataclass
class ManagedTunnelSpec:
    """Parsed spec from either HLETunnel CRD or annotated Ingress."""

    service_url: str
    label: str
    auth_mode: str = "sso"
    websocket_enabled: bool = True
    forward_host: bool = False
    verify_ssl: bool = False
    sync_policy: str = "strict"
    access_control: AccessControlSpec = field(default_factory=AccessControlSpec)
    # Custom zone to publish under (None = base domain); apex serves at its root.
    zone: str | None = None
    apex: bool = False


# How often a running tunnel re-runs the target guard (CRD and Ingress alike).
RECHECK_INTERVAL = 60.0

# Consecutive transient refusals a running tunnel tolerates before it is
# stopped. A cluster DNS outage answers every recheck at once; three failures
# (about three intervals) distinguish it from a name that is genuinely gone.
TRANSIENT_REFUSAL_LIMIT = 3

# How long a refused tunnel waits before rechecking its target. Transient
# refusals back off until they settle on the steady delay; permanent ones are
# retried at that steady delay because the Service, namespace exclusion or
# policy may change.
TRANSIENT_RECOVERY_DELAYS: tuple[float, ...] = (30.0, 60.0, 120.0, 300.0)
PERMANENT_RECOVERY_DELAY = 300.0


def is_transient_refusal(reason: str) -> bool:
    """True when the guard refused only because a name could not be resolved.

    A short cluster DNS outage must not permanently stop a tunnel: the same
    name is expected to resolve again shortly. Everything else (excluded
    namespace, kube API / metadata address, cross-namespace, raw URL) is a
    policy decision that does not fix itself.
    """
    return "cannot resolve" in reason


# Called as ``on_refused(tunnel, message)`` after a running tunnel was stopped
# because its target is no longer allowed.
RefusedCallback = Callable[["ManagedTunnel", str], Awaitable[None]]

# Called as ``on_recovered(tunnel)`` after a refused target is allowed again and
# the connection has been restarted.
RecoveredCallback = Callable[["ManagedTunnel"], Awaitable[None]]


class ManagedTunnel:
    """Manages a single HLE tunnel with access control reconciliation."""

    def __init__(
        self,
        spec: ManagedTunnelSpec,
        api_key: str,
        logger: logging.Logger,
        guard: KubernetesTargetGuard | None = None,
        on_refused: RefusedCallback | None = None,
        on_recovered: RecoveredCallback | None = None,
        recheck_interval: float = RECHECK_INTERVAL,
    ) -> None:
        self._on_refused = on_refused
        self._on_recovered = on_recovered
        self._recheck_interval = recheck_interval
        self._recheck_task: asyncio.Task[None] | None = None
        self._spec = spec
        self._api_key = api_key
        self._logger = logger
        self._guard = guard
        self._pinned_address: str | None = None
        self._transient_failures = 0
        self._tunnel: Tunnel | None = None
        self._task: asyncio.Task[None] | None = None
        self._subdomain: str | None = None
        self._connected = asyncio.Event()
        self._stopped = False

    @property
    def subdomain(self) -> str | None:
        return self._subdomain

    @property
    def public_url(self) -> str | None:
        return self._tunnel.public_url if self._tunnel else None

    @property
    def is_connected(self) -> bool:
        return self._tunnel is not None and self._tunnel.is_connected

    async def start(self) -> None:
        """Start the tunnel and the periodic target recheck.

        Raises :class:`TargetRefused` when the guard refuses the target.
        """
        await self._start_connection()
        if self._recheck_task is None or self._recheck_task.done():
            self._recheck_task = asyncio.create_task(self._recheck_loop())

    async def _start_connection(self) -> None:
        """Start the tunnel connection in a background task."""
        if self._task and not self._task.done():
            return

        # The same refusals agent mode applies: the Kubernetes API, cloud
        # metadata, node addresses, loopback and excluded namespaces.
        #
        # The Tunnel dials the upstream itself and has no per-dial hook, so the
        # address the guard approved is what gets dialled: the URL carries the
        # approved IP and the original authority travels as the Host header and
        # TLS server name. A second DNS answer cannot redirect it (rebinding).
        connect_url, upstream_host = await self._approved_target()

        self._stopped = False
        self._connected.clear()

        config = TunnelConfig(
            service_url=connect_url,
            upstream_host=upstream_host,
            # Never route an in-cluster target through an environment proxy.
            trust_env=False,
            api_key=self._api_key,
            service_label=self._spec.label,
            auth_mode=self._spec.auth_mode,
            websocket_enabled=self._spec.websocket_enabled,
            forward_host=self._spec.forward_host,
            verify_ssl=self._spec.verify_ssl,
            zone=self._spec.zone,
            apex=self._spec.apex,
            managed_by="hle-operator" if self._spec.sync_policy == "strict" else None,
        )

        self._tunnel = Tunnel(
            config=config,
            on_registered=self._on_registered,
        )
        self._task = asyncio.create_task(self._run())

    async def _approved_target(self) -> tuple[str, str | None]:
        """Run the guard; return the URL to dial and the upstream Host/SNI authority."""
        decision = await (self._guard or _default_guard()).check(self._spec.service_url)
        if not decision.allowed:
            raise TargetRefused(f"target refused: {decision.reason}")
        self._pinned_address = decision.address
        if decision.address:
            host = f"[{decision.address}]" if ":" in decision.address else decision.address
            parsed = urlparse(self._spec.service_url)
            port = f":{parsed.port}" if parsed.port else ""
            return (
                urlunparse(parsed._replace(netloc=f"{host}{port}")),
                authority_of(self._spec.service_url),
            )
        if decision.url and decision.url != self._spec.service_url:
            return decision.url, authority_of(self._spec.service_url)
        return self._spec.service_url, None

    async def recheck_target(self) -> None:
        """Re-run the guard; stop the tunnel if refused, redial if the address moved.

        The address is pinned at start, so a Service recreated with a new
        ClusterIP would otherwise stay unreachable until the next spec change.
        A transient refusal (a name that does not resolve) is tolerated up to
        :data:`TRANSIENT_REFUSAL_LIMIT` times in a row — the tunnel keeps its
        pinned address and stays connected — before it is treated like a
        permanent refusal. Raises :class:`TargetRefused` (after stopping the
        connection) once the target is no longer allowed.
        """
        if self._stopped or self._tunnel is None:
            return
        previous = self._pinned_address
        try:
            await self._approved_target()
        except TargetRefused as exc:
            if is_transient_refusal(str(exc)):
                self._transient_failures += 1
                if self._transient_failures < TRANSIENT_REFUSAL_LIMIT:
                    self._logger.warning(
                        "Transient target refusal for %s (%d/%d): %s",
                        self._spec.label,
                        self._transient_failures,
                        TRANSIENT_REFUSAL_LIMIT,
                        exc,
                    )
                    return
            await self._stop_connection()
            raise
        self._transient_failures = 0
        if self._pinned_address != previous:
            self._logger.info("Upstream address changed; reconnecting")
            await self._stop_connection()
            await self._start_connection()

    async def _recheck_loop(self) -> None:
        """One loop per tunnel, whatever created it (HLETunnel or Ingress)."""
        while True:
            await asyncio.sleep(self._recheck_interval)
            try:
                await self.recheck_target()
            except TargetRefused as exc:
                self._logger.error("Tunnel %s stopped: %s", self._spec.label, exc)
                if self._on_refused is not None:
                    try:
                        await self._on_refused(self, str(exc))
                    except Exception:
                        self._logger.exception("Reporting a refused target failed")
                # Stay in this task: a delete or spec change cancels it via stop().
                await self._recover_loop(transient=is_transient_refusal(str(exc)))
            except Exception:
                # A DNS or API blip must not end the loop; try again next round.
                self._logger.exception("Target recheck failed; retrying")

    async def _recover_loop(self, *, transient: bool) -> None:
        """Keep rechecking a refused target and restart the tunnel once allowed.

        Runs inside ``_recheck_task``, so ``stop()`` cancels it along with the
        normal recheck. A transient refusal backs off 30s → 60s → 120s → 300s;
        a permanent one is retried every 300s because the Service, namespace
        exclusion or policy may change.
        """
        delays = TRANSIENT_RECOVERY_DELAYS if transient else (PERMANENT_RECOVERY_DELAY,)
        index = 0
        while True:
            await asyncio.sleep(delays[min(index, len(delays) - 1)])
            try:
                await self._start_connection()
            except TargetRefused as exc:
                self._logger.warning("Target still refused for %s: %s", self._spec.label, exc)
                index += 1
                continue
            except Exception:
                self._logger.exception("Recovery recheck failed; retrying")
                index += 1
                continue
            self._transient_failures = 0
            self._logger.info("Target allowed again; tunnel %s restarted", self._spec.label)
            if self._on_recovered is not None:
                try:
                    await self._on_recovered(self)
                except Exception:
                    self._logger.exception("Reporting a recovered target failed")
            return

    async def stop(self) -> None:
        """Stop the tunnel, its recheck loop and the background task."""
        recheck = self._recheck_task
        self._recheck_task = None
        if recheck is not None and recheck is not asyncio.current_task() and not recheck.done():
            recheck.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await recheck
        await self._stop_connection()

    async def _stop_connection(self) -> None:
        self._stopped = True
        if self._tunnel:
            await self._tunnel.disconnect()
        if self._task and not self._task.done():
            self._task.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await self._task
        self._tunnel = None
        self._task = None
        self._subdomain = None
        self._connected.clear()

    async def wait_connected(self, timeout: float = 30.0) -> bool:
        """Wait for the tunnel to connect. Returns True if connected."""
        try:
            await asyncio.wait_for(self._connected.wait(), timeout=timeout)
            return True
        except TimeoutError:
            return False

    async def reconcile_access_control(self) -> None:
        """Ensure access control on the relay matches the spec (for strict sync policy)."""
        if not self._subdomain or self._spec.sync_policy != "strict":
            return

        client = ApiClient(ApiClientConfig(api_key=self._api_key))

        await self._reconcile_email_rules(client)
        await self._reconcile_pin(client)
        await self._reconcile_basic_auth(client)

    async def _on_registered(self, subdomain: str) -> None:
        """Callback fired after the tunnel registers on the relay."""
        self._subdomain = subdomain
        self._logger.info("Tunnel registered: subdomain=%s", subdomain)
        self._connected.set()

        # Apply access control on first registration
        await self.reconcile_access_control()

    async def _run(self) -> None:
        """Run the tunnel connection (blocks until stopped or fatal error)."""
        if not self._tunnel:
            return
        try:
            await self._tunnel.connect()
        except TunnelFatalError as e:
            self._logger.error("Fatal tunnel error: %s", e)
            raise
        except asyncio.CancelledError:
            pass
        except Exception:
            if not self._stopped:
                self._logger.exception("Tunnel connection error")

    async def _reconcile_email_rules(self, client: ApiClient) -> None:
        """Sync email access rules to match the CRD spec."""
        if not self._subdomain:
            return

        try:
            current_rules: list[dict[str, Any]] = await client.list_access_rules(
                self._subdomain, zone=self._spec.zone
            )
        except Exception:
            self._logger.exception("Failed to list access rules")
            return

        # Build desired state
        desired = {(u.email, u.provider) for u in self._spec.access_control.allowed_users}

        # Build current state
        current = {
            (r["allowed_email"], r["provider"]): r["id"]
            for r in current_rules
            if "allowed_email" in r and "provider" in r
        }

        # Add missing rules
        for email, provider in desired - set(current.keys()):
            try:
                await client.add_access_rule(
                    self._subdomain,
                    email,
                    provider,
                    zone=self._spec.zone,
                )
                self._logger.info("Added access rule: %s (%s)", email, provider)
            except Exception:
                self._logger.exception("Failed to add access rule: %s", email)

        # Remove extra rules
        for key, rule_id in current.items():
            if key not in desired:
                try:
                    await client.delete_access_rule(
                        self._subdomain,
                        rule_id,
                        zone=self._spec.zone,
                    )
                    self._logger.info("Removed access rule: %s (%s)", key[0], key[1])
                except Exception:
                    self._logger.exception("Failed to remove access rule: %s", key[0])

    async def _reconcile_pin(self, client: ApiClient) -> None:
        """Sync PIN protection to match the CRD spec."""
        if not self._subdomain:
            return

        try:
            pin_status = await client.get_tunnel_pin_status(
                self._subdomain,
                zone=self._spec.zone,
            )
            has_pin = pin_status.get("enabled", False)
        except Exception:
            self._logger.exception("Failed to get PIN status")
            return

        desired_pin = self._spec.access_control.pin

        if desired_pin and not has_pin:
            try:
                await client.set_tunnel_pin(self._subdomain, desired_pin, zone=self._spec.zone)
                self._logger.info("PIN set on tunnel")
            except Exception:
                self._logger.exception("Failed to set PIN")
        elif desired_pin and has_pin:
            # Update PIN (always set — we can't check if it matches)
            try:
                await client.set_tunnel_pin(self._subdomain, desired_pin, zone=self._spec.zone)
            except Exception:
                self._logger.exception("Failed to update PIN")
        elif not desired_pin and has_pin:
            try:
                await client.remove_tunnel_pin(self._subdomain, zone=self._spec.zone)
                self._logger.info("PIN removed from tunnel")
            except Exception:
                self._logger.exception("Failed to remove PIN")

    async def _reconcile_basic_auth(self, client: ApiClient) -> None:
        """Sync Basic Auth to match the CRD spec."""
        if not self._subdomain:
            return

        try:
            ba_status = await client.get_tunnel_basic_auth_status(
                self._subdomain, zone=self._spec.zone
            )
            has_ba = ba_status.get("enabled", False)
        except Exception:
            self._logger.exception("Failed to get basic auth status")
            return

        desired_ba = self._spec.access_control.basic_auth

        if desired_ba and not has_ba:
            try:
                await client.set_tunnel_basic_auth(
                    self._subdomain, desired_ba[0], desired_ba[1], zone=self._spec.zone
                )
                self._logger.info("Basic auth set on tunnel")
            except Exception:
                self._logger.exception("Failed to set basic auth")
        elif desired_ba and has_ba:
            try:
                await client.set_tunnel_basic_auth(
                    self._subdomain, desired_ba[0], desired_ba[1], zone=self._spec.zone
                )
            except Exception:
                self._logger.exception("Failed to update basic auth")
        elif not desired_ba and has_ba:
            try:
                await client.remove_tunnel_basic_auth(self._subdomain, zone=self._spec.zone)
                self._logger.info("Basic auth removed from tunnel")
            except Exception:
                self._logger.exception("Failed to remove basic auth")


def parse_crd_spec(
    spec: dict[str, Any],
    namespace: str = "default",
    *,
    allow_cross_namespace: bool = False,
) -> ManagedTunnelSpec:
    """Parse an HLETunnel CRD spec into a ManagedTunnelSpec.

    ``namespace`` is the HLETunnel's own. A ``serviceRef.namespace`` naming
    another namespace is refused unless ``allow_cross_namespace`` is set.
    Raises :class:`SpecError`.
    """
    service_ref = spec.get("serviceRef") or {}
    svc_name = service_ref.get("name", "")
    svc_namespace = service_ref.get("namespace") or namespace
    if svc_namespace != namespace and not allow_cross_namespace:
        raise SpecError(
            f"serviceRef.namespace {svc_namespace!r} differs from the HLETunnel namespace "
            f"{namespace!r}; cross-namespace references are disabled"
        )
    svc_port = service_ref.get("port", 80)
    svc_protocol = service_ref.get("protocol", "http")

    service_url = build_service_url(svc_namespace, svc_name, svc_port, svc_protocol)

    ac_spec = spec.get("accessControl", {})
    allowed_users = [
        AccessRule(email=u["email"], provider=u.get("provider", "any"))
        for u in ac_spec.get("allowedUsers", [])
    ]

    # Basic auth is loaded separately via Kubernetes API —
    # handlers will resolve the Secret and set it on the spec.

    return ManagedTunnelSpec(
        service_url=service_url,
        label=spec.get("label", svc_name),
        auth_mode=spec.get("authMode", "sso"),
        websocket_enabled=spec.get("websocketEnabled", True),
        forward_host=spec.get("forwardHost", False),
        verify_ssl=spec.get("verifySSL", False),
        sync_policy=spec.get("syncPolicy", "strict"),
        access_control=AccessControlSpec(
            allowed_users=allowed_users,
            pin=ac_spec.get("pin"),
        ),
        zone=_zone(spec.get("zone")),
        apex=bool(spec.get("apex", False)),
    )


def _zone(raw: Any) -> str | None:
    """Normalise a zone field/annotation: lower-case, no trailing dot, '' -> None."""
    zone = str(raw or "").strip().lower().rstrip(".")
    return zone or None


def parse_ingress_annotations(
    annotations: dict[str, str],
    rules: list[dict[str, Any]],
) -> ManagedTunnelSpec | None:
    """Parse an Ingress resource into a ManagedTunnelSpec using annotations."""
    if not rules:
        return None

    # Extract the first rule's backend service
    rule = rules[0]
    paths = rule.get("http", {}).get("paths", [])
    if not paths:
        return None

    backend = paths[0].get("backend", {})
    service = backend.get("service", {})
    svc_name = service.get("name", "")

    if not svc_name:
        return None

    # Namespace comes from the Ingress metadata, not annotations
    # It will be set by the handler

    label = annotations.get("hle.world/label", svc_name)
    auth_mode = annotations.get("hle.world/auth-mode", "sso")
    sync_policy = annotations.get("hle.world/sync-policy", "strict")
    ws_enabled = annotations.get("hle.world/websocket-enabled", "true").lower() == "true"
    forward_host = annotations.get("hle.world/forward-host", "false").lower() == "true"
    verify_ssl = annotations.get("hle.world/verify-ssl", "false").lower() == "true"
    pin = annotations.get("hle.world/pin")

    # Parse allowed users: "email1:provider1,email2:provider2"
    allowed_users: list[AccessRule] = []
    users_str = annotations.get("hle.world/allowed-users", "")
    if users_str:
        for entry in users_str.split(","):
            entry = entry.strip()
            if ":" in entry:
                email, provider = entry.rsplit(":", 1)
                allowed_users.append(AccessRule(email=email.strip(), provider=provider.strip()))
            else:
                allowed_users.append(AccessRule(email=entry))

    return ManagedTunnelSpec(
        service_url="",  # Will be set by handler with namespace
        label=label,
        auth_mode=auth_mode,
        websocket_enabled=ws_enabled,
        forward_host=forward_host,
        verify_ssl=verify_ssl,
        sync_policy=sync_policy,
        access_control=AccessControlSpec(
            allowed_users=allowed_users,
            pin=pin,
        ),
        zone=_zone(annotations.get("hle.world/zone")),
        apex=annotations.get("hle.world/apex", "false").lower() == "true",
    )
