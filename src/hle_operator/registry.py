"""The declared-endpoint set, its revision, and the Secret cache.

The registry is the single source of truth for what the cluster says should be
exposed. kopf handlers feed it object bodies on watch events; the initial sync
fills it from a complete list of both kinds before the first frame ever goes
out, so a half-populated set can never retire a still-running tunnel. It also
holds the inputs of the status loop (the last ``declared_ack``) and resolves
the local Secret references the server must never see.

Every endpoint is rebuilt from the stored body plus the Secret caches, so a
rebuild is synchronous and a Secret is only ever read off the event loop.
"""

from __future__ import annotations

import asyncio
import base64
import copy
import dataclasses
import logging
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import Any, Literal, Protocol

import kubernetes.client
from hle_common.agent_protocol import (
    DeclaredAck,
    DeclaredAckEntry,
    DeclaredEndpoint,
    EndpointSpec,
    EndpointStatus,
)

from hle_operator.declared import (
    INGRESS_CLASS,
    PUBLIC_URL_ANNOTATION,
    DeclarationError,
    crd_source_ref,
    crd_to_declared,
    crd_visitor_secret_ref,
    ingress_source_ref,
    ingress_to_declared,
    ingress_visitor_secret_ref,
)

logger = logging.getLogger(__name__)

# The finalizer legacy mode pinned onto every HLETunnel. Agent mode keeps no
# finalizers, so this is removed on sight rather than left to block deletes.
LEGACY_FINALIZER = "hle.world/operator-finalizer"

CUSTOM_GROUP = "hle.world"
CUSTOM_VERSION = "v1alpha1"
CUSTOM_PLURAL = "hletunnels"

# Resend delays after the relay answers a label with `conflict` (a legacy
# tunnel still holds it): one per attempt, then steady. The server rate-limits
# declarations (burst 5, refill 1/10s); this stays far below that.
CONFLICT_RETRY_BACKOFF: tuple[float, ...] = (30.0, 60.0, 120.0)
CONFLICT_RETRY_STEADY: float = 300.0

Kind = Literal["crd", "ingress"]
VisitorRef = tuple[str, str]


class DeclarationClient(Protocol):
    """The slice of ``AgentClient`` the registry drives."""

    async def send_declared_endpoints(
        self, endpoints: list[DeclaredEndpoint], revision: int
    ) -> bool: ...

    def endpoint_statuses(self) -> list[EndpointStatus] | None:
        """None while not connected or not holding the session."""
        ...


class ResourceLister(Protocol):
    """Lists both declaration kinds. Injected so tests need no API server."""

    def list_hletunnels(self) -> list[dict[str, Any]]: ...

    def list_hle_ingresses(self) -> list[dict[str, Any]]: ...


SecretReader = Callable[[str], str]
BasicAuthReader = Callable[[str, str], str | None]
# (namespace, name, finalizers left, resourceVersion the list was read at)
FinalizerStripper = Callable[[str, str, list[str], str | None], None]


@dataclass
class DeclarationTarget:
    """One thing the cluster wants declared, with any reason it could not be."""

    source_ref: str
    endpoint: DeclaredEndpoint | None = None
    error: str | None = None
    # metadata.uid, so a recreated object of the same name gets fresh status.
    uid: str = ""


@dataclass
class _Source:
    kind: Kind
    body: dict[str, Any]


def parse_secret_ref(ref: str) -> tuple[str, str, str]:
    """``namespace/name#key`` -> its three parts."""
    path, _, key = ref.partition("#")
    namespace, _, name = path.partition("/")
    return namespace, name, key


def _b64(data: Mapping[str, str], key: str) -> str:
    value = data.get(key)
    if not value:
        return ""
    return base64.b64decode(value).decode()


def _is_not_found(exc: BaseException) -> bool:
    return getattr(exc, "status", None) == 404


def read_secret_from_kubernetes(ref: str) -> str:
    """Read one key of a Secret through the API (upstream basic auth)."""
    namespace, name, key = parse_secret_ref(ref)
    core = kubernetes.client.CoreV1Api()
    secret = core.read_namespaced_secret(name, namespace)
    if not secret.data or key not in secret.data:
        raise KeyError(f"secret {namespace}/{name} has no key {key!r}")
    return _b64(secret.data, key)


def read_basic_auth_from_kubernetes(namespace: str, name: str) -> str | None:
    """Read username/password of a visitor basic-auth Secret as ``user:pass``."""
    core = kubernetes.client.CoreV1Api()
    secret = core.read_namespaced_secret(name, namespace)
    if not secret.data:
        return None
    username = _b64(secret.data, "username")
    password = _b64(secret.data, "password")
    return f"{username}:{password}" if username and password else None


def strip_finalizer_from_kubernetes(
    namespace: str, name: str, remaining: list[str], resource_version: str | None
) -> None:
    """Drop the legacy operator finalizer from a CR.

    Custom objects only take a merge patch, which replaces the whole list, so
    the patch carries the resourceVersion the list was read at: if anything
    else changed the finalizers meanwhile, the API answers 409 and the next
    watch event retries with the fresh list instead of dropping theirs.
    """
    metadata: dict[str, Any] = {"finalizers": remaining}
    if resource_version:
        metadata["resourceVersion"] = resource_version
    api = kubernetes.client.CustomObjectsApi()
    api.patch_namespaced_custom_object(
        CUSTOM_GROUP,
        CUSTOM_VERSION,
        namespace,
        CUSTOM_PLURAL,
        name,
        {"metadata": metadata},
    )


class KubernetesLister:
    """The default lister: the API server, scoped exactly like kopf's watch."""

    def __init__(self, namespaces: list[str] | None = None) -> None:
        self._namespaces = namespaces or []

    def list_hletunnels(self) -> list[dict[str, Any]]:
        api = kubernetes.client.CustomObjectsApi()
        if self._namespaces:
            items = [
                item
                for ns in self._namespaces
                for item in api.list_namespaced_custom_object(
                    CUSTOM_GROUP, CUSTOM_VERSION, ns, CUSTOM_PLURAL
                ).get("items", [])
            ]
        else:
            items = api.list_cluster_custom_object(CUSTOM_GROUP, CUSTOM_VERSION, CUSTOM_PLURAL).get(
                "items", []
            )
        return [_to_dict(item) for item in items]

    def list_hle_ingresses(self) -> list[dict[str, Any]]:
        api = kubernetes.client.NetworkingV1Api()
        if self._namespaces:
            listed = [
                item for ns in self._namespaces for item in api.list_namespaced_ingress(ns).items
            ]
        else:
            listed = api.list_ingress_for_all_namespaces().items
        return [
            body
            for body in (_to_dict(item) for item in listed)
            if (body.get("spec") or {}).get("ingressClassName") == INGRESS_CLASS
        ]


def _to_dict(obj: Any) -> dict[str, Any]:
    if isinstance(obj, dict):
        return obj
    serialized = kubernetes.client.ApiClient().sanitize_for_serialization(obj)
    return dict(serialized) if isinstance(serialized, dict) else {}


def _source_ref(kind: Kind, body: Mapping[str, Any]) -> str:
    return crd_source_ref(body) if kind == "crd" else ingress_source_ref(body)


def _visitor_ref(kind: Kind, body: Mapping[str, Any]) -> VisitorRef | None:
    return crd_visitor_secret_ref(body) if kind == "crd" else ingress_visitor_secret_ref(body)


def _upstream_ref(kind: Kind, body: Mapping[str, Any]) -> str | None:
    if kind != "crd":
        return None
    ref = (body.get("spec") or {}).get("upstreamBasicAuthSecret")
    return str(ref) if ref else None


def _relevant(source: _Source) -> tuple[Any, ...]:
    """The parts of a body a declaration is built from (not status, not RV)."""
    meta = source.body.get("metadata") or {}
    annotations = {
        k: v for k, v in (meta.get("annotations") or {}).items() if k != PUBLIC_URL_ANNOTATION
    }
    return (source.kind, source.body.get("spec"), annotations, meta.get("uid"))


class _Mutated(Exception):
    """A watch event landed while a list was in flight; the list may be stale."""


class DeclarationRegistry:
    """The desired set, its revision, and everything the status loop needs."""

    def __init__(
        self,
        client: DeclarationClient,
        *,
        lister: ResourceLister | None = None,
        read_secret: SecretReader | None = None,
        read_basic_auth: BasicAuthReader | None = None,
        strip_finalizer: FinalizerStripper | None = None,
        debounce: float = 1.0,
        relist_interval: float = 300.0,
        secret_poll_interval: float = 60.0,
    ) -> None:
        self._client = client
        self._lister = lister or KubernetesLister()
        self._read_secret = read_secret or read_secret_from_kubernetes
        self._read_basic_auth = read_basic_auth or read_basic_auth_from_kubernetes
        self._strip_finalizer = strip_finalizer or strip_finalizer_from_kubernetes
        self._debounce = debounce
        self._relist_interval = relist_interval
        self._secret_poll_interval = secret_poll_interval

        # What the cluster says, by source_ref. Endpoints are derived from it.
        self._sources: dict[str, _Source] = {}
        self._endpoints: dict[str, DeclaredEndpoint] = {}
        self._errors: dict[str, str] = {}
        # Secret caches. A visitor value of None means "read, and unusable".
        self._visitor_cache: dict[VisitorRef, str | None] = {}
        self._secret_cache: dict[str, str] = {}

        self._acks: dict[str, DeclaredAckEntry] = {}
        self._ack_revision = -1
        self._ack_seen = False

        self._revision = 0
        self._initial_done = False
        # Bumped by every watch event, so a list that raced one is discarded.
        self._mutations = 0
        self._listing = False
        self._last_sent: list[DeclaredEndpoint] | None = None
        self._dirty = False
        self._send_task: asyncio.Task[None] | None = None
        self._closed = False
        # Conflict retry: one timer, the attempts made since the last clean ack.
        self._retry_task: asyncio.Task[None] | None = None
        self._retry_step = 0
        self._conflicted: list[str] = []

    # -- watch events --------------------------------------------------------

    async def apply_crd(self, body: Mapping[str, Any], *, schedule: bool = True) -> None:
        await self._apply("crd", body, schedule=schedule)

    async def apply_ingress(self, body: Mapping[str, Any], *, schedule: bool = True) -> None:
        await self._apply("ingress", body, schedule=schedule)

    async def _apply(self, kind: Kind, body: Mapping[str, Any], *, schedule: bool) -> None:
        source_ref = _source_ref(kind, body)
        stored = self._sources.get(source_ref)
        source = _Source(kind, copy.deepcopy(dict(body)))
        if stored is not None and _relevant(stored) == _relevant(source):
            # Our own status patches come back as MODIFIED events. They change
            # nothing that is declared, so they neither count as a mutation
            # (which would void an in-flight list) nor trigger a send.
            self._sources[source_ref] = source
            return
        self._mutations += 1
        self._sources[source_ref] = source
        await self._prefetch_secrets([self._sources[source_ref]])
        # Rebuild from whatever is stored now: a relist that landed during the
        # prefetch holds the newer body, and a delete leaves nothing to build.
        changed = self._rebuild(source_ref)
        if schedule and changed:
            self.schedule_send()

    def remove(self, source_ref: str, *, schedule: bool = True) -> None:
        # Every non-hle Ingress lands here on each of its events, so outside a
        # list only a real removal counts as a mutation. During one, any
        # removal does: the in-flight list may still contain the object.
        existed = self._sources.pop(source_ref, None) is not None
        if existed or self._listing:
            self._mutations += 1
        if not existed:
            return
        changed = self._rebuild(source_ref)
        if schedule and changed:
            self.schedule_send()

    # -- building ------------------------------------------------------------

    def _build(self, source: _Source) -> DeclaredEndpoint:
        visitor_ref = _visitor_ref(source.kind, source.body)
        basic_auth: str | None = None
        if visitor_ref is not None:
            # Fail closed: a policy that names a Secret is never declared
            # without it, which on a strict endpoint would clear basic auth.
            basic_auth = self._visitor_cache.get(visitor_ref)
            if basic_auth is None:
                raise DeclarationError(
                    f"basic-auth Secret {visitor_ref[0]}/{visitor_ref[1]} is missing, "
                    "unreadable, or has no username/password"
                )
        if source.kind == "crd":
            return crd_to_declared(source.body, basic_auth=basic_auth)
        return ingress_to_declared(source.body, basic_auth=basic_auth)

    def _rebuild(self, source_ref: str) -> bool:
        """Re-derive one entry from its stored body. True when it changed."""
        before = (self._endpoints.get(source_ref), self._errors.get(source_ref))
        self._endpoints.pop(source_ref, None)
        self._errors.pop(source_ref, None)
        source = self._sources.get(source_ref)
        if source is not None:
            try:
                self._endpoints[source_ref] = self._build(source)
            except DeclarationError as exc:
                self._errors[source_ref] = str(exc)
        return before != (self._endpoints.get(source_ref), self._errors.get(source_ref))

    def _rebuild_all(self) -> None:
        for source_ref in list(self._endpoints.keys() | self._errors.keys() | self._sources.keys()):
            self._rebuild(source_ref)

    def _effective(self) -> tuple[dict[str, DeclaredEndpoint], dict[str, str]]:
        """Endpoints to declare and errors to show, with duplicate labels resolved.

        Two objects asking for one label would otherwise both be declared and
        the server would keep the last one, while both mirrored its status.
        The first by source_ref keeps it; the others fail visibly.
        """
        endpoints: dict[str, DeclaredEndpoint] = {}
        errors = dict(self._errors)
        owner: dict[str, str] = {}
        for source_ref in sorted(self._endpoints):
            endpoint = self._endpoints[source_ref]
            holder = owner.get(endpoint.label)
            if holder is not None:
                errors[source_ref] = f"label {endpoint.label!r} is already declared by {holder}"
                continue
            owner[endpoint.label] = source_ref
            endpoints[source_ref] = endpoint
        return endpoints, errors

    # -- finalizer hygiene ---------------------------------------------------

    async def strip_legacy_finalizer(self, body: Mapping[str, Any]) -> bool:
        """Remove the legacy operator finalizer on sight. Returns True if it did.

        Only that one entry is dropped; every other finalizer is kept, and the
        patch is pinned to the resourceVersion the list was read at.
        """
        meta = body.get("metadata") or {}
        finalizers = list(meta.get("finalizers") or [])
        if LEGACY_FINALIZER not in finalizers:
            return False
        remaining = [f for f in finalizers if f != LEGACY_FINALIZER]
        try:
            await asyncio.to_thread(
                self._strip_finalizer,
                meta.get("namespace") or "default",
                meta.get("name") or "",
                remaining,
                meta.get("resourceVersion"),
            )
        except Exception:  # noqa: BLE001 — best-effort; the next event retries
            logger.warning(
                "Could not strip %s from %s/%s; will retry on the next event",
                LEGACY_FINALIZER,
                meta.get("namespace"),
                meta.get("name"),
            )
            return False
        return True

    # -- declarations --------------------------------------------------------

    def declared(self) -> list[DeclaredEndpoint]:
        endpoints, _ = self._effective()
        return [endpoints[ref] for ref in sorted(endpoints)]

    def targets(self) -> list[DeclarationTarget]:
        """Declared endpoints plus the ones that could not be, for status."""
        endpoints, errors = self._effective()
        out = [
            DeclarationTarget(source_ref=ref, endpoint=endpoint, uid=self._uid(ref))
            for ref, endpoint in endpoints.items()
        ]
        out += [
            DeclarationTarget(source_ref=ref, error=message, uid=self._uid(ref))
            for ref, message in errors.items()
        ]
        return sorted(out, key=lambda t: t.source_ref)

    def _uid(self, source_ref: str) -> str:
        source = self._sources.get(source_ref)
        if source is None:
            return ""
        return str((source.body.get("metadata") or {}).get("uid") or "")

    def next_revision(self) -> int:
        """Monotonic within the process, wall-clock ms so a new pod exceeds it."""
        self._revision = max(self._revision + 1, time.time_ns() // 1_000_000)
        return self._revision

    async def send_now(self, *, force: bool = False) -> bool:
        """Hand the full set to the client.

        An unchanged set is not re-sent (the client already re-sends its last
        frame on every welcome) unless *force*: an upstream Secret rotation
        leaves the set identical but needs the server's state_sync so the
        resolver runs again.
        """
        endpoints = self.declared()
        if not force and endpoints == self._last_sent:
            return True
        if endpoints != self._last_sent:
            # A changed desired set restarts the conflict backoff from scratch.
            self._reset_retry()
        sent = await self._client.send_declared_endpoints(endpoints, self.next_revision())
        self._last_sent = endpoints
        return sent

    async def send_if_ready(self, *, force: bool = False) -> bool:
        """Send, but never before the initial sync has seen both full lists."""
        if not self._initial_done:
            return False
        return await self.send_now(force=force)

    def schedule_send(self) -> None:
        """One frame per debounce window, carrying the latest state."""
        if self._closed:
            return
        self._dirty = True
        if self._send_task is not None and not self._send_task.done():
            return
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            return
        self._send_task = loop.create_task(self._debounced_send())

    async def _debounced_send(self) -> None:
        # Loops while events keep arriving, so a change that lands while a
        # frame is being sent goes out in the next one rather than waiting for
        # some later event.
        while self._dirty and not self._closed:
            await asyncio.sleep(self._debounce)
            self._dirty = False
            try:
                await self.send_if_ready()
            except Exception:  # noqa: BLE001 — the next change or relist sends again
                logger.exception("Sending declared endpoints failed")

    # -- acks ----------------------------------------------------------------

    def record_ack(self, ack: DeclaredAck) -> None:
        if ack.revision < self._ack_revision:
            return  # a late ack for an older frame
        self._ack_revision = ack.revision
        self._ack_seen = True
        self._acks = {entry.label: entry for entry in ack.endpoints}
        if ack.revision < self._revision:
            return  # a newer frame is out; its ack decides whether to retry
        self._conflicted = sorted(e.label for e in ack.endpoints if e.status == "conflict")
        if not self._conflicted:
            self._reset_retry()
        else:
            self._ensure_retry()

    # -- conflict retry ------------------------------------------------------

    def _reset_retry(self) -> None:
        self._retry_step = 0
        self._conflicted = []
        task = self._retry_task
        self._retry_task = None
        if task is not None and not task.done() and task is not asyncio.current_task():
            task.cancel()

    def _ensure_retry(self) -> None:
        if self._closed:
            return
        if self._retry_task is not None and not self._retry_task.done():
            return
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            return
        self._retry_task = loop.create_task(self._retry_conflicts())

    def _retry_delay(self) -> float:
        if self._retry_step < len(CONFLICT_RETRY_BACKOFF):
            return CONFLICT_RETRY_BACKOFF[self._retry_step]
        return CONFLICT_RETRY_STEADY

    async def _retry_conflicts(self) -> None:
        """Re-declare the current set until no label is answered with conflict.

        The relay only answers a declaration, so a label freed later (the old
        pod exits) would otherwise stay Failed until something else changed.
        """
        while not self._closed and self._conflicted:
            delay = self._retry_delay()
            await asyncio.sleep(delay)
            if self._closed or not self._conflicted:
                return
            if (
                not self._initial_done
                or self._client.endpoint_statuses() is None
                or self._dirty
                or (self._send_task is not None and not self._send_task.done())
            ):
                continue  # not connected / not ready / a debounced send is due
            logger.info(
                "Relay reported a conflict for %s; re-declaring (retry %d, waited %gs)",
                ", ".join(self._conflicted),
                self._retry_step + 1,
                delay,
            )
            self._retry_step += 1
            try:
                await self.send_now(force=True)
            except Exception:  # noqa: BLE001 — the next round tries again
                logger.exception("Retrying conflicted declarations failed")

    def ack_seen(self) -> bool:
        return self._ack_seen

    def ack_for_label(self, label: str) -> DeclaredAckEntry | None:
        return self._acks.get(label)

    # -- initial sync / relist ----------------------------------------------

    async def _list_all(self) -> dict[str, _Source]:
        """Both full lists plus their Secrets, or raise.

        Raises :class:`_Mutated` when a watch event arrived meanwhile: the list
        could then be older than that event, and replacing the map with it
        could resurrect a deleted object.
        """
        mark = self._mutations
        self._listing = True
        try:
            crds = await asyncio.to_thread(self._lister.list_hletunnels)
            ingresses = await asyncio.to_thread(self._lister.list_hle_ingresses)
            sources: dict[str, _Source] = {}
            for body in crds:
                sources[crd_source_ref(body)] = _Source("crd", body)
            for body in ingresses:
                sources[ingress_source_ref(body)] = _Source("ingress", body)
            await self._prefetch_secrets(list(sources.values()), strict=True)
        finally:
            self._listing = False
        if self._mutations != mark:
            raise _Mutated
        return sources

    def _replace(self, sources: dict[str, _Source]) -> None:
        self._sources = sources
        self._rebuild_all()
        self._prune_secret_caches()

    async def try_initial_sync(self) -> bool:
        """List both kinds, fill the map, then send the first frame.

        Returns False without touching the map when either list fails, so an
        empty frame can never go out ahead of a complete picture.
        """
        try:
            sources = await self._list_all()
        except _Mutated:
            logger.debug("Watch events arrived during the initial list; listing again")
            return False
        except Exception:  # noqa: BLE001 — retried by run_initial_sync
            logger.exception("Initial cluster listing failed; keeping last-known state")
            return False
        self._replace(sources)
        self._initial_done = True
        logger.info("Initial cluster sync: %d endpoint(s)", len(self.declared()))
        await self.send_now(force=True)
        return True

    async def run_initial_sync(self, retry_interval: float = 5.0) -> None:
        while not self._initial_done and not self._closed:
            try:
                if await self.try_initial_sync():
                    return
            except Exception:  # noqa: BLE001 — never let the gate task die silently
                logger.exception("Initial sync failed")
                if self._initial_done:
                    return
            await asyncio.sleep(retry_interval)

    async def relist(self) -> bool:
        """Correct drift every few minutes. Returns True when the set changed."""
        if not self._initial_done:
            return False
        try:
            sources = await self._list_all()
        except _Mutated:
            logger.debug("Watch events arrived during the relist; skipping this round")
            return False
        except Exception:  # noqa: BLE001 — watch events remain the primary input
            logger.exception("Periodic relist failed")
            return False
        before = [(t.source_ref, t.endpoint, t.error) for t in self.targets()]
        self._replace(sources)
        after = [(t.source_ref, t.endpoint, t.error) for t in self.targets()]
        if before == after:
            return False
        await self.send_if_ready()
        return True

    async def run_maintenance(self) -> None:
        while not self._closed:
            await asyncio.sleep(self._relist_interval)
            try:
                await self.relist()
            except Exception:  # noqa: BLE001
                logger.exception("Relist failed")

    # -- Secrets -------------------------------------------------------------

    async def _prefetch_secrets(self, sources: list[_Source], *, strict: bool = False) -> None:
        """Read Secrets not cached yet, off the event loop.

        With *strict* (the list paths) a transient read error raises, so the
        initial sync retries instead of declaring a set that is missing an
        endpoint only because the API blipped.
        """
        for source in sources:
            visitor = _visitor_ref(source.kind, source.body)
            if visitor is not None and visitor not in self._visitor_cache:
                value = await self._fetch_visitor(visitor, None, strict=strict)
                self._visitor_cache[visitor] = value
            upstream = _upstream_ref(source.kind, source.body)
            if upstream is not None and upstream not in self._secret_cache:
                fetched = await self._fetch_upstream(upstream, strict=strict)
                if fetched is not None:
                    self._secret_cache[upstream] = fetched

    async def _fetch_visitor(
        self, ref: VisitorRef, last: str | None, *, strict: bool = False
    ) -> str | None:
        try:
            return await asyncio.to_thread(self._read_basic_auth, ref[0], ref[1])
        except Exception as exc:  # noqa: BLE001 — the value is never logged
            if _is_not_found(exc):
                return None
            if strict:
                raise
            logger.warning("Could not read basic-auth Secret %s/%s", ref[0], ref[1])
            return last  # a transient error keeps the last good value

    async def _fetch_upstream(self, ref: str, *, strict: bool = False) -> str | None:
        try:
            return await asyncio.to_thread(self._read_secret, ref)
        except Exception as exc:  # noqa: BLE001 — the value is never logged
            if strict and not _is_not_found(exc) and not isinstance(exc, KeyError):
                raise
            logger.warning("Could not read upstream Secret %s", ref)
            return None

    def _referenced(self) -> tuple[set[VisitorRef], set[str]]:
        visitors: set[VisitorRef] = set()
        upstreams: set[str] = set()
        for source in self._sources.values():
            visitor = _visitor_ref(source.kind, source.body)
            if visitor is not None:
                visitors.add(visitor)
            upstream = _upstream_ref(source.kind, source.body)
            if upstream is not None:
                upstreams.add(upstream)
        return visitors, upstreams

    def _prune_secret_caches(self) -> None:
        visitors, upstreams = self._referenced()
        for ref in self._visitor_cache.keys() - visitors:
            del self._visitor_cache[ref]
        for key in self._secret_cache.keys() - upstreams:
            del self._secret_cache[key]

    def resolve_spec(self, spec: EndpointSpec) -> EndpointSpec:
        """``spec_resolver``: fill upstream basic auth from a local Secret.

        A declaration only carries ``upstream_basic_auth_secret``; the value
        never travelled, so it is looked up here, by label, before the
        reconciler compares and starts the tunnel. Only the cache is read: this
        runs on the event loop, and the Secret poll keeps the cache fresh.
        """
        endpoints, _ = self._effective()
        for endpoint in endpoints.values():
            if endpoint.label != spec.label:
                continue
            ref = endpoint.upstream_basic_auth_secret
            if not ref:
                return spec
            value = self._secret_cache.get(ref)
            if value is None:
                raise ValueError(f"upstream secret {ref} is unavailable")
            return dataclasses.replace(spec, upstream_basic_auth=value)
        return spec

    async def poll_secrets_once(self) -> bool:
        """Re-read every referenced Secret; True when anything changed.

        A changed visitor Secret re-declares (the value travels); a changed
        upstream Secret forces a frame so the server's state_sync runs the
        resolver again and the tunnel restarts with the new credential.
        """
        visitors, upstreams = self._referenced()
        visitor_changed = False
        for ref in visitors:
            last = self._visitor_cache.get(ref)
            value = await self._fetch_visitor(ref, last)
            if value != last:
                visitor_changed = True
            self._visitor_cache[ref] = value
        upstream_changed = False
        for key in upstreams:
            value = await self._fetch_upstream(key)
            if value is not None and self._secret_cache.get(key) != value:
                self._secret_cache[key] = value
                upstream_changed = True
        self._prune_secret_caches()

        set_changed = False
        if visitor_changed:
            before = self.declared()
            self._rebuild_all()
            set_changed = self.declared() != before
        if set_changed or upstream_changed:
            await self.send_if_ready(force=upstream_changed)
        return visitor_changed or upstream_changed

    async def run_secret_poll(self) -> None:
        while not self._closed:
            await asyncio.sleep(self._secret_poll_interval)
            try:
                await self.poll_secrets_once()
            except Exception:  # noqa: BLE001
                logger.exception("Secret poll failed")

    # -- shutdown ------------------------------------------------------------

    def close(self) -> None:
        self._closed = True
        if self._send_task is not None and not self._send_task.done():
            self._send_task.cancel()
        if self._retry_task is not None and not self._retry_task.done():
            self._retry_task.cancel()
