"""Managed tunnel lifecycle — wraps hle-client Tunnel + relay API for access control."""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import urlparse

from hle_client.api import ApiClient, ApiClientConfig
from hle_client.tunnel import Tunnel, TunnelConfig, TunnelFatalError


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


class ManagedTunnel:
    """Manages a single HLE tunnel with access control reconciliation."""

    def __init__(
        self,
        spec: ManagedTunnelSpec,
        api_key: str,
        logger: logging.Logger,
    ) -> None:
        self._spec = spec
        self._api_key = api_key
        self._logger = logger
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
        """Start the tunnel connection in a background task."""
        if self._task and not self._task.done():
            return

        self._stopped = False
        self._connected.clear()

        config = TunnelConfig(
            service_url=self._spec.service_url,
            api_key=self._api_key,
            service_label=self._spec.label,
            auth_mode=self._spec.auth_mode,
            websocket_enabled=self._spec.websocket_enabled,
            forward_host=self._spec.forward_host,
            verify_ssl=self._spec.verify_ssl,
            managed_by="hle-operator" if self._spec.sync_policy == "strict" else None,
        )

        self._tunnel = Tunnel(
            config=config,
            on_registered=self._on_registered,
        )
        self._task = asyncio.create_task(self._run())

    async def stop(self) -> None:
        """Stop the tunnel and cancel the background task."""
        self._stopped = True
        if self._tunnel:
            await self._tunnel.disconnect()
        if self._task and not self._task.done():
            self._task.cancel()
            try:
                await self._task
            except (asyncio.CancelledError, Exception):
                pass
        self._tunnel = None
        self._task = None
        self._subdomain = None
        self._connected.clear()

    async def wait_connected(self, timeout: float = 30.0) -> bool:
        """Wait for the tunnel to connect. Returns True if connected."""
        try:
            await asyncio.wait_for(self._connected.wait(), timeout=timeout)
            return True
        except asyncio.TimeoutError:
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
                self._subdomain
            )
        except Exception:
            self._logger.exception("Failed to list access rules")
            return

        # Build desired state
        desired = {
            (u.email, u.provider) for u in self._spec.access_control.allowed_users
        }

        # Build current state
        current = {
            (r["email"], r["provider"]): r["id"]
            for r in current_rules
            if "email" in r and "provider" in r
        }

        # Add missing rules
        for email, provider in desired - set(current.keys()):
            try:
                await client.add_access_rule(self._subdomain, email, provider)
                self._logger.info("Added access rule: %s (%s)", email, provider)
            except Exception:
                self._logger.exception("Failed to add access rule: %s", email)

        # Remove extra rules
        for key, rule_id in current.items():
            if key not in desired:
                try:
                    await client.delete_access_rule(self._subdomain, rule_id)
                    self._logger.info("Removed access rule: %s (%s)", key[0], key[1])
                except Exception:
                    self._logger.exception("Failed to remove access rule: %s", key[0])

    async def _reconcile_pin(self, client: ApiClient) -> None:
        """Sync PIN protection to match the CRD spec."""
        if not self._subdomain:
            return

        try:
            pin_status = await client.get_tunnel_pin_status(self._subdomain)
            has_pin = pin_status.get("enabled", False)
        except Exception:
            self._logger.exception("Failed to get PIN status")
            return

        desired_pin = self._spec.access_control.pin

        if desired_pin and not has_pin:
            try:
                await client.set_tunnel_pin(self._subdomain, desired_pin)
                self._logger.info("PIN set on tunnel")
            except Exception:
                self._logger.exception("Failed to set PIN")
        elif desired_pin and has_pin:
            # Update PIN (always set — we can't check if it matches)
            try:
                await client.set_tunnel_pin(self._subdomain, desired_pin)
            except Exception:
                self._logger.exception("Failed to update PIN")
        elif not desired_pin and has_pin:
            try:
                await client.remove_tunnel_pin(self._subdomain)
                self._logger.info("PIN removed from tunnel")
            except Exception:
                self._logger.exception("Failed to remove PIN")

    async def _reconcile_basic_auth(self, client: ApiClient) -> None:
        """Sync Basic Auth to match the CRD spec."""
        if not self._subdomain:
            return

        try:
            ba_status = await client.get_tunnel_basic_auth_status(self._subdomain)
            has_ba = ba_status.get("enabled", False)
        except Exception:
            self._logger.exception("Failed to get basic auth status")
            return

        desired_ba = self._spec.access_control.basic_auth

        if desired_ba and not has_ba:
            try:
                await client.set_tunnel_basic_auth(
                    self._subdomain, desired_ba[0], desired_ba[1]
                )
                self._logger.info("Basic auth set on tunnel")
            except Exception:
                self._logger.exception("Failed to set basic auth")
        elif desired_ba and has_ba:
            try:
                await client.set_tunnel_basic_auth(
                    self._subdomain, desired_ba[0], desired_ba[1]
                )
            except Exception:
                self._logger.exception("Failed to update basic auth")
        elif not desired_ba and has_ba:
            try:
                await client.remove_tunnel_basic_auth(self._subdomain)
                self._logger.info("Basic auth removed from tunnel")
            except Exception:
                self._logger.exception("Failed to remove basic auth")


def parse_crd_spec(spec: dict[str, Any]) -> ManagedTunnelSpec:
    """Parse an HLETunnel CRD spec into a ManagedTunnelSpec."""
    service_ref = spec.get("serviceRef", {})
    svc_name = service_ref.get("name", "")
    svc_namespace = service_ref.get("namespace", "default")
    svc_port = service_ref.get("port", 80)
    svc_protocol = service_ref.get("protocol", "http")

    service_url = f"{svc_protocol}://{svc_name}.{svc_namespace}.svc:{svc_port}"

    ac_spec = spec.get("accessControl", {})
    allowed_users = [
        AccessRule(email=u["email"], provider=u.get("provider", "any"))
        for u in ac_spec.get("allowedUsers", [])
    ]

    ba_secret_ref = ac_spec.get("basicAuth", {}).get("secretRef")
    # Basic auth is loaded separately via Kubernetes API — store None here,
    # handlers will resolve the Secret and set it.

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
    )


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
    svc_port = service.get("port", {}).get("number", 80)

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
    )
