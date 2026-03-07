"""Operator configuration loaded from environment and Kubernetes secrets."""

from __future__ import annotations

import logging
import os

import kubernetes.client
import kubernetes.config

logger = logging.getLogger(__name__)

# Global API key loaded at startup from the hle-api-key Secret.
_api_key: str = ""


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
