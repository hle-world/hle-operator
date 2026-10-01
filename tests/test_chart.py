"""Tests for the Helm chart's release-tracking contract.

The chart is published as an OCI artifact with its version and appVersion set
from the release tag at package time (see .github/workflows/build.yml). These
tests pin the parts that make an unpinned `helm install oci://.../hle-operator`
resolve to the release's image.
"""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path
from typing import Any

import pytest
import yaml

CHART_DIR = Path(__file__).resolve().parents[1] / "chart" / "hle-operator"

# `helm template` is the real renderer, so these tests exercise the templates
# (conditionals, join, quoting) rather than re-reading values.yaml. The chart
# tests still pass without Helm: only the render-dependent classes skip.
HELM = shutil.which("helm")
requires_helm = pytest.mark.skipif(HELM is None, reason="helm not on PATH")


def _load(name: str) -> dict:
    with (CHART_DIR / name).open() as handle:
        return yaml.safe_load(handle)


def _render(*values: str) -> list[dict[str, Any]]:
    """Render the chart with the given `--set` arguments and return the docs."""
    cmd = [HELM or "helm", "template", "test-release", str(CHART_DIR)]
    for value in values:
        cmd += ["--set", value]
    result = subprocess.run(cmd, capture_output=True, text=True, check=True, timeout=60)
    return [doc for doc in yaml.safe_load_all(result.stdout) if doc]


def _deployment(docs: list[dict[str, Any]], name_suffix: str) -> dict[str, Any]:
    for doc in docs:
        if doc.get("kind") == "Deployment" and doc["metadata"]["name"].endswith(name_suffix):
            return doc
    raise AssertionError(f"no Deployment ending in {name_suffix!r}")


def _container(deployment: dict[str, Any], name: str) -> dict[str, Any]:
    for container in deployment["spec"]["template"]["spec"]["containers"]:
        if container["name"] == name:
            return container
    raise AssertionError(f"no container named {name!r}")


def _env(container: dict[str, Any]) -> dict[str, str | None]:
    return {item["name"]: item.get("value") for item in container.get("env", [])}


class TestChartMetadata:
    def test_chart_identity(self):
        chart = _load("Chart.yaml")
        assert chart["apiVersion"] == "v2"
        assert chart["name"] == "hle-operator"
        assert chart["type"] == "application"

    def test_static_version_matches_app_version(self):
        chart = _load("Chart.yaml")
        # YAML parses the unquoted `version` as a float, so normalise it; the
        # two fields are bumped together and must stay in lockstep.
        assert str(chart["version"]) == chart["appVersion"]


class TestImageTagDefaults:
    def test_operator_image_tag_falls_back_to_app_version(self):
        values = _load("values.yaml")
        assert values["image"]["tag"] == ""
        assert values["image"]["pullPolicy"] == "IfNotPresent"

        template = (CHART_DIR / "templates" / "deployment.yaml").read_text()
        assert ".Values.image.tag | default .Chart.AppVersion" in template


class TestAgentDefaults:
    def test_firepuncher_is_off_by_default(self):
        agent = _load("values.yaml")["agent"]
        assert agent["firepuncher"]["enabled"] is False

    def test_discovery_excludes_system_namespaces(self):
        discovery = _load("values.yaml")["agent"]["discovery"]
        assert set(discovery["excludeNamespaces"]) == {
            "kube-system",
            "kube-public",
            "kube-node-lease",
        }
        assert discovery["excludeLabel"] == "hle.world/expose=denied"


class TestInstallMethod:
    @requires_helm
    def test_agent_sets_install_method_kubernetes(self):
        docs = _render("agent.enabled=true")
        agent = _container(_deployment(docs, "-agent"), "agent")
        assert _env(agent)["HLE_INSTALL_METHOD"] == "kubernetes"

    @requires_helm
    def test_operator_sets_install_method_kubernetes(self):
        docs = _render()
        operator = _container(_deployment(docs, "-operator"), "operator")
        assert _env(operator)["HLE_INSTALL_METHOD"] == "kubernetes"


class TestAgentReadiness:
    @requires_helm
    def test_readiness_probe_runs_agent_status(self):
        deployment = _deployment(_render("agent.enabled=true"), "-agent")
        probe = _container(deployment, "agent")["readinessProbe"]
        assert probe["exec"]["command"] == ["hle", "agent", "status"]

    @requires_helm
    def test_agent_rollout_is_recreate(self):
        deployment = _deployment(_render("agent.enabled=true"), "-agent")
        assert deployment["spec"]["strategy"]["type"] == "Recreate"


class TestFirepuncherToggle:
    @requires_helm
    def test_firepuncher_env_defaults_off(self):
        agent = _container(_deployment(_render("agent.enabled=true"), "-agent"), "agent")
        assert _env(agent)["HLE_FIREPUNCHER_ENABLED"] == "false"

    @requires_helm
    def test_firepuncher_can_be_opted_in(self):
        docs = _render("agent.enabled=true", "agent.firepuncher.enabled=true")
        agent = _container(_deployment(docs, "-agent"), "agent")
        assert _env(agent)["HLE_FIREPUNCHER_ENABLED"] == "true"


class TestDiscoveryScope:
    @requires_helm
    def test_exclusions_are_passed_to_the_agent(self):
        agent = _container(_deployment(_render("agent.enabled=true"), "-agent"), "agent")
        env = _env(agent)
        assert env["HLE_DISCOVERY_EXCLUDE_NAMESPACES"] == (
            "kube-system,kube-public,kube-node-lease"
        )
        assert env["HLE_DISCOVERY_EXCLUDE_LABEL"] == "hle.world/expose=denied"

    @requires_helm
    def test_discovery_off_drops_the_grant_and_the_env(self):
        docs = _render(
            "operator.enabled=false",
            "agent.enabled=true",
            "agent.discovery.enabled=false",
        )
        deployment = _deployment(docs, "-agent")
        env = _env(_container(deployment, "agent"))
        assert "HLE_DISCOVERY_EXCLUDE_NAMESPACES" not in env
        assert deployment["spec"]["template"]["spec"]["automountServiceAccountToken"] is False
        names = {doc["metadata"]["name"] for doc in docs if doc.get("kind") == "ClusterRole"}
        assert names == set()
