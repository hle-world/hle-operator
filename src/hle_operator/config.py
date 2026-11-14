"""Operator configuration loaded from environment and Kubernetes secrets."""

from __future__ import annotations

import fnmatch
import logging
import os
from collections.abc import Mapping
from dataclasses import dataclass, field
from enum import StrEnum

import kubernetes.client
import kubernetes.config

logger = logging.getLogger(__name__)

# Global API key loaded at startup from the hle-api-key Secret.
_api_key: str = ""

# -- mode and agent-mode settings -------------------------------------------
# Agent mode is the default (hle-world/hle-operator #6): one pod, one
# enrollment, the CRD/Ingress reconciler layered on top. Legacy mode keeps the
# full-account apiKey operator exactly as it was.
MODE_ENV = "HLE_OPERATOR_MODE"
AGENT_TOKEN_ENV = "HLE_AGENT_TOKEN"  # noqa: S105 — an env var name, not a secret
CREDENTIAL_ENV = "HLE_CREDENTIAL"
LEGACY_API_KEY_ENV = "HLE_API_KEY"
GITOPS_ENV = "HLE_GITOPS"
RELAY_HOST_ENV = "HLE_RELAY_HOST"
RELAY_PORT_ENV = "HLE_RELAY_PORT"
NAMESPACES_ENV = "HLE_NAMESPACES"
# Glob scope: which namespaces are reconciled. `HLE_NAMESPACE_PATTERNS` is the
# allow-list, `HLE_EXCLUDE_NAMESPACES` the deny-list (it wins). Literal
# `HLE_NAMESPACES` and globs are mutually exclusive: RBAC differs, and mixing
# them would be ambiguous.
NAMESPACE_PATTERNS_ENV = "HLE_NAMESPACE_PATTERNS"
EXCLUDE_NAMESPACES_ENV = "HLE_EXCLUDE_NAMESPACES"
DEFAULT_RELAY_HOST = "hle.world"
DEFAULT_RELAY_PORT = 443


class OperatorMode(StrEnum):
    AGENT = "agent"
    LEGACY = "legacy"


@dataclass
class OperatorConfig:
    """Resolved runtime configuration for one process."""

    mode: OperatorMode
    # Agent mode: the tunnel-scoped ``hle_`` key (or a legacy ``hlea_`` token).
    # Legacy mode: the full-account API key.
    credential: str = ""
    # Whether to run the CRD/Ingress reconciler and declare its endpoints.
    # Legacy mode always does; in agent mode it is opt-in and off by default.
    gitops: bool = False
    relay_host: str = DEFAULT_RELAY_HOST
    relay_port: int = DEFAULT_RELAY_PORT
    # Empty means cluster-wide. kopf takes either this or clusterwide=True.
    # Literal namespace names; RBAC is granted per namespace.
    namespaces: list[str] = field(default_factory=list)
    # Namespace globs to reconcile exclusively. Empty means every namespace
    # (subject to excludes). RBAC stays cluster-wide: a per-run namespace
    # cannot be granted ahead of time.
    namespace_patterns: list[str] = field(default_factory=list)
    # Namespace globs never reconciled. Wins over the patterns.
    exclude_namespaces: list[str] = field(default_factory=list)


def _env_bool(value: str | None, *, default: bool) -> bool:
    if value is None:
        return default
    return value.strip().lower() in ("1", "true", "yes", "on")


def _env_list(value: str | None) -> list[str]:
    """Split a comma-separated env var, dropping empty entries and blanks."""
    return [item.strip() for item in (value or "").split(",") if item.strip()]


def resolve_mode(env: Mapping[str, str] | None = None) -> OperatorMode:
    """Pick agent or legacy mode.

    ``HLE_OPERATOR_MODE`` wins when set. Otherwise auto-detect: an older
    install that has only ``HLE_API_KEY`` keeps working as legacy, because a
    full-account key cannot be turned into an agent enrollment in place. Any
    agent credential present means agent mode, the default.
    """
    env = os.environ if env is None else env
    explicit = (env.get(MODE_ENV) or "").strip().lower()
    if explicit == OperatorMode.AGENT:
        return OperatorMode.AGENT
    if explicit == OperatorMode.LEGACY:
        return OperatorMode.LEGACY
    if explicit:
        logger.warning("Ignoring %s=%r; expected 'agent' or 'legacy'", MODE_ENV, explicit)

    has_agent_cred = bool(env.get(AGENT_TOKEN_ENV) or env.get(CREDENTIAL_ENV))
    has_api_key = bool(env.get(LEGACY_API_KEY_ENV))
    if has_api_key and not has_agent_cred:
        return OperatorMode.LEGACY
    return OperatorMode.AGENT


def load_operator_config(env: Mapping[str, str] | None = None) -> OperatorConfig:
    """Read mode, credential, gitops, relay and namespace scope from the env."""
    env = os.environ if env is None else env
    mode = resolve_mode(env)

    if mode is OperatorMode.AGENT:
        credential = env.get(AGENT_TOKEN_ENV) or env.get(CREDENTIAL_ENV) or ""
        gitops = _env_bool(env.get(GITOPS_ENV), default=False)
        if not credential:
            logger.error(
                "Agent mode has no credential — set %s (or %s) to the tunnel-scoped key",
                AGENT_TOKEN_ENV,
                CREDENTIAL_ENV,
            )
        if env.get(LEGACY_API_KEY_ENV):
            logger.warning(
                "Agent mode is using a full-account %s; prefer a tunnel-scoped credential",
                LEGACY_API_KEY_ENV,
            )
    else:
        credential = env.get(LEGACY_API_KEY_ENV) or ""
        gitops = True
        logger.warning(
            "Running in legacy operator mode (%s=legacy); the full-account %s path is "
            "deprecated in favour of the agent default",
            MODE_ENV,
            LEGACY_API_KEY_ENV,
        )

    namespaces = _env_list(env.get(NAMESPACES_ENV))
    namespace_patterns = _env_list(env.get(NAMESPACE_PATTERNS_ENV))
    exclude_namespaces = _env_list(env.get(EXCLUDE_NAMESPACES_ENV))
    if namespaces and (namespace_patterns or exclude_namespaces):
        logger.error(
            "%s cannot be combined with %s or %s: pick literal names or globs, not both",
            NAMESPACES_ENV,
            NAMESPACE_PATTERNS_ENV,
            EXCLUDE_NAMESPACES_ENV,
        )
        raise SystemExit(2)
    try:
        relay_port = int(env.get(RELAY_PORT_ENV) or DEFAULT_RELAY_PORT)
    except ValueError:
        logger.warning("Ignoring invalid %s; using %d", RELAY_PORT_ENV, DEFAULT_RELAY_PORT)
        relay_port = DEFAULT_RELAY_PORT

    return OperatorConfig(
        mode=mode,
        credential=credential,
        gitops=gitops,
        relay_host=env.get(RELAY_HOST_ENV) or DEFAULT_RELAY_HOST,
        relay_port=relay_port,
        namespaces=namespaces,
        namespace_patterns=namespace_patterns,
        exclude_namespaces=exclude_namespaces,
    )


def kopf_scope(cfg: OperatorConfig) -> tuple[bool, list[str]]:
    """``(clusterwide, namespaces)`` for ``kopf.operator``.

    Literal names keep kopf's per-namespace watch. Globs and excludes let kopf
    filter. kopf matches a namespace if ANY list entry matches it, and a ``!``
    negation only applies inside the same comma-separated entry, so every
    include glob carries all the negations: ``["e2e-*, !x"]`` or, with only
    excludes, ``["*, !e2e-*"]``. A bare ``["*", "!e2e-*"]`` would still
    watch ``e2e-*`` through the ``*`` entry.
    """
    if cfg.namespaces:
        return False, list(cfg.namespaces)
    if cfg.namespace_patterns or cfg.exclude_namespaces:
        negations = "".join(f", !{pattern}" for pattern in cfg.exclude_namespaces)
        return False, [f"{pattern}{negations}" for pattern in cfg.namespace_patterns or ["*"]]
    return True, []


def namespace_in_scope(cfg: OperatorConfig, name: str) -> bool:
    """Whether *name* may be reconciled under the configured namespace scope.

    Excludes always win. Structurally the same rule as kopf's, applied to the
    list results the agent reads directly from the API server.
    """
    if cfg.namespaces and name not in cfg.namespaces:
        return False
    if cfg.namespace_patterns and not any(
        fnmatch.fnmatchcase(name, pattern) for pattern in cfg.namespace_patterns
    ):
        return False
    return not any(fnmatch.fnmatchcase(name, pattern) for pattern in cfg.exclude_namespaces)


def load_config() -> None:
    """Load Kubernetes config (in-cluster or kubeconfig for local dev)."""
    try:
        kubernetes.config.load_incluster_config()
        logger.info("Loaded in-cluster Kubernetes config")
    except kubernetes.config.ConfigException:
        kubernetes.config.load_kube_config()
        logger.info("Loaded kubeconfig for local development")


def load_api_key() -> str:
    """Load the global HLE API key from the environment or Kubernetes Secret.

    Order of precedence:
    1. HLE_API_KEY environment variable (set by Secret envFrom in deployment)
    2. Direct read from the Secret (fallback)
    """
    global _api_key

    env_key = os.environ.get("HLE_API_KEY", "")
    if env_key:
        _api_key = env_key
        logger.info("Loaded HLE API key from environment")
        return _api_key

    # Fallback: read from Secret directly
    namespace = os.environ.get("POD_NAMESPACE", "default")
    secret_name = os.environ.get("HLE_API_KEY_SECRET", "hle-api-key")
    secret_key = os.environ.get("HLE_API_KEY_SECRET_KEY", "api-key")

    try:
        v1 = kubernetes.client.CoreV1Api()
        secret = v1.read_namespaced_secret(secret_name, namespace)
        if secret.data and secret_key in secret.data:
            import base64

            _api_key = base64.b64decode(secret.data[secret_key]).decode()
            logger.info("Loaded HLE API key from Secret %s/%s", namespace, secret_name)
        else:
            logger.error("Secret %s/%s missing key '%s'", namespace, secret_name, secret_key)
    except kubernetes.client.ApiException as e:
        logger.error("Failed to read Secret %s/%s: %s", namespace, secret_name, e.reason)

    return _api_key


def get_api_key() -> str:
    return _api_key


# An HLETunnel may only expose a Service in its own namespace. Naming another
# one lets anyone who can create HLETunnels in one namespace publish a Service
# from a namespace they have no rights in, so it stays off until the cluster
# owner turns it on (chart value `gitops.allowCrossNamespace`).
ALLOW_CROSS_NAMESPACE_ENV = "HLE_ALLOW_CROSS_NAMESPACE"


def allow_cross_namespace(env: Mapping[str, str] | None = None) -> bool:
    env = os.environ if env is None else env
    return _env_bool(env.get(ALLOW_CROSS_NAMESPACE_ENV), default=False)
