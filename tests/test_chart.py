"""Tests for the Helm chart's release-tracking contract and agent-mode pivot.

The chart is published as an OCI artifact with its version and appVersion set
from the release tag at package time (see .github/workflows/build.yml). These
tests pin the parts that make an unpinned `helm install oci://.../hle-operator`
resolve to the release's image, plus the agent/legacy modes, the credential
migration off `apiKey`/`agent.token`, the handover strategy and scoped RBAC.
"""

from __future__ import annotations

import re
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


def _secret_ref(container: dict[str, Any], name: str) -> dict[str, str]:
    for item in container.get("env", []):
        if item["name"] == name:
            return item["valueFrom"]["secretKeyRef"]
    raise AssertionError(f"no env {name!r}")


def _kind_names(docs: list[dict[str, Any]], kind: str) -> list[str]:
    return [doc["metadata"]["name"] for doc in docs if doc.get("kind") == kind]


def _rules(doc: dict[str, Any]) -> list[dict[str, Any]]:
    return doc.get("rules") or []


def _rule(role: dict[str, Any], resource: str) -> dict[str, Any]:
    for rule in _rules(role):
        if resource in rule.get("resources", []):
            return rule
    raise AssertionError(f"no rule for {resource!r} in {role['metadata']['name']}")


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


class TestCrdLabelPattern:
    """The CRD's label pattern agrees with the relay and declared.py."""

    def _pattern(self) -> str:
        # The CRD is a Helm template (guarded by `{{- if }}`), so read the label's
        # pattern line rather than parsing the file as YAML.
        text = (CHART_DIR / "templates" / "crds" / "hletunnel.yaml").read_text()
        match = re.search(r"label:\n(?:.*\n)*?\s+pattern: \"([^\"]+)\"", text)
        assert match, "label pattern not found in the CRD"
        return match.group(1)

    @pytest.mark.parametrize("label", ["a", "ab", "a-b", "jellyfin", "x9"])
    def test_accepts_valid_labels(self, label: str):
        assert re.fullmatch(self._pattern(), label)

    @pytest.mark.parametrize("label", ["", "-a", "a-", "A", "a.b", "a_b"])
    def test_rejects_invalid_labels(self, label: str):
        assert not re.fullmatch(self._pattern(), label)


class TestImageTagDefaults:
    def test_operator_image_tag_falls_back_to_app_version(self):
        values = _load("values.yaml")
        assert values["image"]["tag"] == ""
        assert values["image"]["pullPolicy"] == "IfNotPresent"

        template = (CHART_DIR / "templates" / "deployment.yaml").read_text()
        assert ".Values.image.tag | default .Chart.AppVersion" in template


class TestModeResolution:
    @requires_helm
    def test_default_is_agent(self):
        deployment = _deployment(_render(), "-operator")
        assert _env(_container(deployment, "operator"))["HLE_OPERATOR_MODE"] == "agent"

    @requires_helm
    def test_api_key_only_is_legacy(self):
        deployment = _deployment(_render("apiKey.value=hle_old"), "-operator")
        assert _env(_container(deployment, "operator"))["HLE_OPERATOR_MODE"] == "legacy"

    @requires_helm
    def test_api_key_existing_secret_is_legacy(self):
        deployment = _deployment(_render("apiKey.existingSecret=legacy-key"), "-operator")
        assert _env(_container(deployment, "operator"))["HLE_OPERATOR_MODE"] == "legacy"

    @requires_helm
    def test_credential_is_agent(self):
        deployment = _deployment(_render("credential.value=hle_new"), "-operator")
        assert _env(_container(deployment, "operator"))["HLE_OPERATOR_MODE"] == "agent"

    @requires_helm
    def test_explicit_mode_wins_over_auto(self):
        deployment = _deployment(_render("mode=legacy", "credential.value=hle_new"), "-operator")
        assert _env(_container(deployment, "operator"))["HLE_OPERATOR_MODE"] == "legacy"


class TestInstallMethod:
    @requires_helm
    def test_agent_sets_install_method_kubernetes(self):
        deployment = _deployment(_render("credential.value=hle_x"), "-operator")
        assert _env(_container(deployment, "operator"))["HLE_INSTALL_METHOD"] == "kubernetes"

    @requires_helm
    def test_legacy_sets_install_method_kubernetes(self):
        deployment = _deployment(_render("apiKey.value=hle_old"), "-operator")
        assert _env(_container(deployment, "operator"))["HLE_INSTALL_METHOD"] == "kubernetes"


class TestAgentDeployment:
    @requires_helm
    def test_agent_mode_is_one_deployment(self):
        docs = _render("credential.value=hle_x")
        deployments = [doc for doc in docs if doc.get("kind") == "Deployment"]
        assert [doc["metadata"]["name"] for doc in deployments] == ["test-release-hle-operator"]

    @requires_helm
    def test_replicas_pinned_to_one_in_agent_mode(self):
        deployment = _deployment(_render("credential.value=hle_x", "replicaCount=5"), "-operator")
        assert deployment["spec"]["replicas"] == 1

    @requires_helm
    def test_handover_group_is_lowercased_namespace_release(self):
        deployment = _deployment(_render("credential.value=hle_x"), "-operator")
        env = _env(_container(deployment, "operator"))
        assert env["HLE_HANDOVER_GROUP"] == "default/test-release"

    @requires_helm
    def test_agent_writes_state_to_the_data_volume(self):
        deployment = _deployment(_render("credential.value=hle_x"), "-operator")
        container = _container(deployment, "operator")
        assert _env(container)["HLE_HOME"] == "/data"
        mounts = {m["name"]: m["mountPath"] for m in container["volumeMounts"]}
        assert mounts == {"data": "/data", "tmp": "/tmp"}  # noqa: S108
        assert deployment["spec"]["template"]["spec"]["volumes"] == [
            {"name": "data", "emptyDir": {}},
            {"name": "tmp", "emptyDir": {}},
        ]

    @requires_helm
    def test_gitops_is_passed_to_the_agent(self):
        deployment = _deployment(_render("credential.value=hle_x"), "-operator")
        env = _env(_container(deployment, "operator"))
        assert env["HLE_GITOPS"] == "true"

        env = _env(
            _container(
                _deployment(_render("credential.value=hle_x", "gitops.enabled=false"), "-operator"),
                "operator",
            )
        )
        assert env["HLE_GITOPS"] == "false"


class TestReadinessProbe:
    @requires_helm
    def test_readiness_probe_waits_for_the_agent(self):
        deployment = _deployment(_render("credential.value=hle_x"), "-operator")
        probe = _container(deployment, "operator")["readinessProbe"]
        assert probe["exec"]["command"] == ["hle", "agent", "status", "--ready"]

    @requires_helm
    def test_legacy_has_no_agent_readiness_probe(self):
        deployment = _deployment(_render("apiKey.value=hle_old"), "-operator")
        assert "readinessProbe" not in _container(deployment, "operator")


class TestHandoverStrategy:
    @requires_helm
    def test_agent_defaults_to_zero_drop_rolling_update(self):
        deployment = _deployment(_render("credential.value=hle_x"), "-operator")
        assert deployment["spec"]["strategy"] == {
            "type": "RollingUpdate",
            "rollingUpdate": {"maxSurge": 1, "maxUnavailable": 0},
        }

    @requires_helm
    def test_handover_disabled_renders_recreate(self):
        deployment = _deployment(
            _render("credential.value=hle_x", "handover.enabled=false"), "-operator"
        )
        assert deployment["spec"]["strategy"]["type"] == "Recreate"

    @requires_helm
    def test_legacy_ignores_handover(self):
        deployment = _deployment(
            _render("apiKey.value=hle_old", "handover.enabled=true"), "-operator"
        )
        assert "strategy" not in deployment["spec"]


class TestCredentialMigration:
    @requires_helm
    def test_credential_value_creates_named_secret(self):
        docs = _render("credential.value=hle_new")
        assert "test-release-hle-operator-credential" in _kind_names(docs, "Secret")

        container = _container(_deployment(docs, "-operator"), "operator")
        assert _secret_ref(container, "HLE_AGENT_TOKEN") == {
            "name": "test-release-hle-operator-credential",
            "key": "credential",
        }

    @requires_helm
    def test_agent_token_maps_onto_the_credential(self):
        docs = _render("agent.token.value=hlea_old")
        assert "test-release-hle-operator-agent-token" in _kind_names(docs, "Secret")

        container = _container(_deployment(docs, "-operator"), "operator")
        assert _secret_ref(container, "HLE_AGENT_TOKEN") == {
            "name": "test-release-hle-operator-agent-token",
            "key": "agent-token",
        }

    @requires_helm
    def test_api_key_maps_onto_the_legacy_credential(self):
        docs = _render("apiKey.value=hle_old")
        assert "test-release-hle-operator-api-key" in _kind_names(docs, "Secret")

        container = _container(_deployment(docs, "-operator"), "operator")
        assert _secret_ref(container, "HLE_API_KEY") == {
            "name": "test-release-hle-operator-api-key",
            "key": "api-key",
        }

    @requires_helm
    def test_api_key_existing_secret_is_referenced(self):
        container = _container(
            _deployment(_render("apiKey.existingSecret=legacy-key"), "-operator"), "operator"
        )
        assert _secret_ref(container, "HLE_API_KEY") == {
            "name": "legacy-key",
            "key": "api-key",
        }

    @requires_helm
    def test_credential_existing_secret_takes_precedence(self):
        container = _container(
            _deployment(
                _render("credential.existingSecret=my-cred", "apiKey.value=hle_old"),
                "-operator",
            ),
            "operator",
        )
        assert _secret_ref(container, "HLE_AGENT_TOKEN") == {
            "name": "my-cred",
            "key": "credential",
        }


class TestLegacyMode:
    @requires_helm
    def test_replica_count_is_honoured(self):
        deployment = _deployment(_render("apiKey.value=hle_old", "replicaCount=3"), "-operator")
        assert deployment["spec"]["replicas"] == 3

    @requires_helm
    def test_legacy_env_has_no_agent_settings(self):
        deployment = _deployment(_render("apiKey.value=hle_old"), "-operator")
        env = _env(_container(deployment, "operator"))
        assert env["HLE_OPERATOR_MODE"] == "legacy"
        assert "HLE_API_KEY" in env
        assert "HLE_AGENT_TOKEN" not in env
        assert "HLE_GITOPS" not in env
        assert "HLE_HANDOVER_GROUP" not in env

    @requires_helm
    def test_legacy_operator_rbac_is_kept(self):
        docs = _render("apiKey.value=hle_old")
        names = _kind_names(docs, "ClusterRole")
        assert "test-release-hle-operator" in names
        # The legacy operator reads Secrets by name for basic auth.
        role = next(d for d in docs if d.get("kind") == "ClusterRole")
        assert _rule(role, "secrets")["verbs"] == ["get"]


class TestSeparateLegacyAgent:
    @requires_helm
    def test_separate_agent_renders_only_in_legacy_mode(self):
        docs = _render("credential.value=hle_x", "agent.enabled=true")
        assert "test-release-hle-operator-agent" not in _kind_names(docs, "Deployment")

        docs = _render(
            "mode=legacy",
            "apiKey.value=hle_old",
            "agent.enabled=true",
            "agent.token.value=hlea_old",
        )
        assert "test-release-hle-operator-agent" in _kind_names(docs, "Deployment")


class TestAgentRbac:
    @requires_helm
    def test_cluster_wide_rbac_grants_discovery_and_gitops(self):
        docs = _render("credential.value=hle_x")
        role = next(d for d in docs if d.get("kind") == "ClusterRole")

        assert _rule(role, "services")["verbs"] == ["get", "list", "watch"]
        assert _rule(role, "hletunnels")["verbs"] == ["get", "list", "watch", "patch"]
        assert _rule(role, "hletunnels/status")["verbs"] == ["patch", "update"]
        # patch: the hle.world/public-url annotation is a metadata patch.
        assert _rule(role, "ingresses")["verbs"] == ["get", "list", "watch", "patch"]
        assert _rule(role, "ingresses/status")["verbs"] == ["patch", "update"]
        assert _rule(role, "customresourcedefinitions")["verbs"] == ["list", "watch"]

    @requires_helm
    def test_never_grants_secret_list_or_pod_exec(self):
        docs = _render("credential.value=hle_x", "gitops.clusterWideSecretGet=true")
        for rule in _rules(next(d for d in docs if d.get("kind") == "ClusterRole")):
            assert "list" not in rule["verbs"] or "secrets" not in rule["resources"]
            assert "watch" not in rule["verbs"] or "secrets" not in rule["resources"]
            assert "pods/exec" not in rule["resources"]
            assert "pods/log" not in rule["resources"]

    @requires_helm
    def test_secrets_absent_by_default(self):
        role = next(d for d in _render("credential.value=hle_x") if d.get("kind") == "ClusterRole")
        assert all("secrets" not in rule["resources"] for rule in _rules(role))

    @requires_helm
    def test_secret_resource_names_restrict_the_grant(self):
        docs = _render("credential.value=hle_x", "gitops.secretResourceNames={basic-a,basic-b}")
        role = next(d for d in docs if d.get("kind") == "ClusterRole")
        rule = _rule(role, "secrets")
        assert rule["verbs"] == ["get"]
        assert rule["resourceNames"] == ["basic-a", "basic-b"]

    @requires_helm
    def test_cluster_wide_secret_get_is_opt_in(self):
        role = next(
            d
            for d in _render("credential.value=hle_x", "gitops.clusterWideSecretGet=true")
            if d.get("kind") == "ClusterRole"
        )
        assert _rule(role, "secrets")["verbs"] == ["get"]


class TestScopedRbac:
    @requires_helm
    def test_scoped_uses_per_namespace_roles(self):
        docs = _render("credential.value=hle_x", "scope.namespaces={team-a,team-b}")

        roles = [d for d in docs if d.get("kind") == "Role"]
        bindings = [d for d in docs if d.get("kind") == "RoleBinding"]
        assert sorted(d["metadata"]["namespace"] for d in roles) == ["team-a", "team-b"]
        assert sorted(d["metadata"]["namespace"] for d in bindings) == ["team-a", "team-b"]

        # The cluster-scoped ClusterRole carries only CRD discovery, not Services.
        cluster_role = next(d for d in docs if d.get("kind") == "ClusterRole")
        resources = {r for rule in _rules(cluster_role) for r in rule["resources"]}
        assert "services" not in resources
        assert "customresourcedefinitions" in resources

        assert _rule(roles[0], "services")["verbs"] == ["get", "list", "watch"]

    @requires_helm
    def test_namespaces_env_is_passed_to_the_agent(self):
        env = _env(
            _container(
                _deployment(
                    _render("credential.value=hle_x", "scope.namespaces={team-a,team-b}"),
                    "-operator",
                ),
                "operator",
            )
        )
        assert env["HLE_NAMESPACES"] == "team-a,team-b"


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

    @requires_helm
    def test_firepuncher_env_defaults_off(self):
        agent = _container(_deployment(_render("credential.value=hle_x"), "-operator"), "operator")
        assert _env(agent)["HLE_FIREPUNCHER_ENABLED"] == "false"

    @requires_helm
    def test_firepuncher_can_be_opted_in(self):
        docs = _render("credential.value=hle_x", "agent.firepuncher.enabled=true")
        agent = _container(_deployment(docs, "-operator"), "operator")
        assert _env(agent)["HLE_FIREPUNCHER_ENABLED"] == "true"

    @requires_helm
    def test_exclusions_are_passed_to_the_agent(self):
        agent = _container(_deployment(_render("credential.value=hle_x"), "-operator"), "operator")
        env = _env(agent)
        assert env["HLE_DISCOVERY_EXCLUDE_NAMESPACES"] == (
            "kube-system,kube-public,kube-node-lease"
        )
        assert env["HLE_DISCOVERY_EXCLUDE_LABEL"] == "hle.world/expose=denied"

    @requires_helm
    def test_discovery_off_drops_the_grant_and_the_env(self):
        docs = _render(
            "credential.value=hle_x",
            "agent.discovery.enabled=false",
            "gitops.enabled=false",
        )
        deployment = _deployment(docs, "-operator")
        container = _container(deployment, "operator")
        assert "HLE_DISCOVERY_EXCLUDE_NAMESPACES" not in _env(container)
        # The process loads Kubernetes config and runs kopf even with gitops
        # off, so the token stays mounted; there is just nothing granted to it.
        assert deployment["spec"]["template"]["spec"]["automountServiceAccountToken"] is True
        names = _kind_names(docs, "ClusterRole") + _kind_names(docs, "Role")
        assert names == []

    @requires_helm
    def test_allow_raw_urls_is_off_by_default(self):
        agent = _container(_deployment(_render("credential.value=hle_x"), "-operator"), "operator")
        assert _env(agent)["HLE_ALLOW_RAW_URLS"] == "false"


class TestPodHardening:
    @requires_helm
    def test_agent_pod_is_non_root_with_read_only_root(self):
        deployment = _deployment(_render("credential.value=hle_x"), "-operator")
        pod = deployment["spec"]["template"]["spec"]
        assert pod["securityContext"]["runAsNonRoot"] is True
        # Numeric, or the kubelet cannot verify the image's `hleop` user.
        assert pod["securityContext"]["runAsUser"] == 1001
        security = _container(deployment, "operator")["securityContext"]
        assert security["readOnlyRootFilesystem"] is True
        assert security["allowPrivilegeEscalation"] is False
        assert security["capabilities"] == {"drop": ["ALL"]}

    @requires_helm
    def test_agent_pod_keeps_the_service_account_token_even_without_gitops(self):
        docs = _render("credential.value=hle_x", "gitops.enabled=false")
        pod = _deployment(docs, "-operator")["spec"]["template"]["spec"]
        assert pod["automountServiceAccountToken"] is True


class TestUpgradeSafety:
    """Old values files must render the same workload identity under the new chart."""

    @requires_helm
    def test_deployment_selector_is_unchanged(self):
        # Deployment selectors are immutable: changing one fails `helm upgrade`.
        for sets in (["apiKey.value=k"], ["agent.token.value=t"], ["credential.value=c"]):
            selector = _deployment(_render(*sets), "-operator")["spec"]["selector"]
            assert selector == {
                "matchLabels": {
                    "app.kubernetes.io/name": "hle-operator",
                    "app.kubernetes.io/instance": "test-release",
                }
            }

    @requires_helm
    def test_old_api_key_and_agent_token_secrets_keep_their_names(self):
        docs = _render("apiKey.value=k", "agent.token.value=t", "agent.enabled=true")
        # Agent token wins, so the agent-token Secret (its old name) is the one.
        container = _container(_deployment(docs, "-operator"), "operator")
        assert _secret_ref(container, "HLE_AGENT_TOKEN") == {
            "name": "test-release-hle-operator-agent-token",
            "key": "agent-token",
        }
        assert "test-release-hle-operator-agent-token" in _kind_names(docs, "Secret")

    @requires_helm
    def test_existing_secret_names_and_keys_carry_over(self):
        docs = _render(
            "agent.enabled=true",
            "agent.token.existingSecret=mytok",
            "agent.token.secretKey=t",
        )
        container = _container(_deployment(docs, "-operator"), "operator")
        assert _secret_ref(container, "HLE_AGENT_TOKEN") == {"name": "mytok", "key": "t"}
        assert _kind_names(docs, "Secret") == []

    @requires_helm
    def test_forced_legacy_keeps_each_credential_on_its_own_workload(self):
        docs = _render(
            "mode=legacy",
            "apiKey.value=full",
            "agent.enabled=true",
            "agent.token.value=tok",
        )
        operator = _container(_deployment(docs, "-operator"), "operator")
        assert _secret_ref(operator, "HLE_API_KEY") == {
            "name": "test-release-hle-operator-api-key",
            "key": "api-key",
        }
        agent = _container(_deployment(docs, "-operator-agent"), "agent")
        assert _secret_ref(agent, "HLE_AGENT_TOKEN") == {
            "name": "test-release-hle-operator-agent-token",
            "key": "agent-token",
        }
        secrets = {d["metadata"]["name"]: d["stringData"] for d in docs if d["kind"] == "Secret"}
        assert secrets == {
            "test-release-hle-operator-api-key": {"api-key": "full"},
            "test-release-hle-operator-agent-token": {"agent-token": "tok"},
        }

    @requires_helm
    def test_old_agent_only_values_do_not_grow_gitops(self):
        # operator.enabled=false + agent.enabled=true was a pure agent.
        docs = _render("operator.enabled=false", "agent.enabled=true", "agent.token.value=t")
        assert [d["metadata"]["name"] for d in docs if d["kind"] == "Deployment"] == [
            "test-release-hle-operator"
        ]
        env = _env(_container(_deployment(docs, "-operator"), "operator"))
        assert env["HLE_GITOPS"] == "false"
        role = next(d for d in docs if d.get("kind") == "ClusterRole")
        resources = {r for rule in _rules(role) for r in rule["resources"]}
        assert resources == {"services", "endpoints"}

    @requires_helm
    def test_old_agent_placement_values_carry_over(self):
        docs = _render(
            "agent.enabled=true",
            "agent.token.value=t",
            "agent.nodeSelector.disk=ssd",
        )
        pod = _deployment(docs, "-operator")["spec"]["template"]["spec"]
        assert pod["nodeSelector"] == {"disk": "ssd"}

    @requires_helm
    def test_legacy_default_render_is_the_old_workload(self):
        docs = _render("apiKey.value=k")
        assert [d["metadata"]["name"] for d in docs if d["kind"] == "Deployment"] == [
            "test-release-hle-operator"
        ]
        assert "securityContext" not in _deployment(docs, "-operator")["spec"]["template"]["spec"]


class TestHandoverGroup:
    @requires_helm
    def test_group_matches_the_server_rule_for_long_names(self):
        import re

        cmd = [
            HELM or "helm",
            "template",
            "a" * 53,
            str(CHART_DIR),
            "--namespace",
            "n" * 63,
            "--set",
            "credential.value=hle_x",
        ]
        out = subprocess.run(cmd, capture_output=True, text=True, check=True, timeout=60).stdout
        docs = [d for d in yaml.safe_load_all(out) if d]
        env = _env(_container(next(d for d in docs if d["kind"] == "Deployment"), "operator"))
        group = env["HLE_HANDOVER_GROUP"]
        assert group is not None
        assert len(group) <= 128
        assert re.fullmatch(r"[a-z0-9]([a-z0-9._/-]*[a-z0-9])?", group)


class TestRbacLeastPrivilege:
    @requires_helm
    def test_secret_get_needs_gitops(self):
        docs = _render(
            "credential.value=hle_x",
            "gitops.enabled=false",
            "gitops.clusterWideSecretGet=true",
            "scope.namespaces={team-a}",
        )
        for doc in docs:
            if doc.get("kind") in ("Role", "ClusterRole"):
                assert all("secrets" not in rule["resources"] for rule in _rules(doc))

    @requires_helm
    def test_scoped_roles_never_grant_secrets_by_default(self):
        docs = _render("credential.value=hle_x", "scope.namespaces={team-a}")
        for doc in docs:
            if doc.get("kind") in ("Role", "ClusterRole"):
                assert all("secrets" not in rule["resources"] for rule in _rules(doc))

    @requires_helm
    def test_status_subresource_grant_is_only_for_hle_resources(self):
        docs = _render("credential.value=hle_x")
        role = next(d for d in docs if d.get("kind") == "ClusterRole")
        status = [r for rule in _rules(role) for r in rule["resources"] if r.endswith("/status")]
        assert sorted(status) == ["hletunnels/status", "ingresses/status"]
