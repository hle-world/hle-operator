"""Operator mode resolution and agent config defaults."""

from __future__ import annotations

import pytest

from hle_operator.config import (
    OperatorConfig,
    OperatorMode,
    kopf_scope,
    load_operator_config,
    namespace_in_scope,
    resolve_mode,
)


class TestResolveMode:
    def test_explicit_agent_wins(self):
        assert resolve_mode({"HLE_OPERATOR_MODE": "agent", "HLE_API_KEY": "k"}) is (
            OperatorMode.AGENT
        )

    def test_explicit_legacy_wins(self):
        assert resolve_mode({"HLE_OPERATOR_MODE": "legacy", "HLE_AGENT_TOKEN": "t"}) is (
            OperatorMode.LEGACY
        )

    def test_api_key_only_is_legacy(self):
        assert resolve_mode({"HLE_API_KEY": "k"}) is OperatorMode.LEGACY

    def test_agent_credential_is_agent(self):
        assert resolve_mode({"HLE_AGENT_TOKEN": "t"}) is OperatorMode.AGENT
        assert resolve_mode({"HLE_CREDENTIAL": "c"}) is OperatorMode.AGENT

    def test_agent_credential_beats_api_key(self):
        assert resolve_mode({"HLE_API_KEY": "k", "HLE_CREDENTIAL": "c"}) is OperatorMode.AGENT

    def test_nothing_is_agent_default(self):
        assert resolve_mode({}) is OperatorMode.AGENT


class TestLoadOperatorConfig:
    def test_agent_defaults(self):
        cfg = load_operator_config({"HLE_AGENT_TOKEN": "t"})
        assert cfg.mode is OperatorMode.AGENT
        assert cfg.credential == "t"
        assert cfg.gitops is False
        assert cfg.relay_host == "hle.world"
        assert cfg.relay_port == 443
        assert cfg.namespaces == []

    def test_credential_alias(self):
        cfg = load_operator_config({"HLE_CREDENTIAL": "c"})
        assert cfg.credential == "c"

    def test_gitops_opt_in(self):
        cfg = load_operator_config({"HLE_AGENT_TOKEN": "t", "HLE_GITOPS": "true"})
        assert cfg.gitops is True

    def test_legacy_forces_gitops_and_reads_api_key(self):
        cfg = load_operator_config({"HLE_API_KEY": "full"})
        assert cfg.mode is OperatorMode.LEGACY
        assert cfg.credential == "full"
        assert cfg.gitops is True

    def test_relay_and_namespaces(self):
        cfg = load_operator_config(
            {
                "HLE_AGENT_TOKEN": "t",
                "HLE_RELAY_HOST": "relay.internal",
                "HLE_RELAY_PORT": "8443",
                "HLE_NAMESPACES": "apps, monitoring",
            }
        )
        assert cfg.relay_host == "relay.internal"
        assert cfg.relay_port == 8443
        assert cfg.namespaces == ["apps", "monitoring"]

    def test_invalid_relay_port_falls_back(self):
        cfg = load_operator_config({"HLE_AGENT_TOKEN": "t", "HLE_RELAY_PORT": "not-a-port"})
        assert cfg.relay_port == 443


class TestNamespaceScopeConfig:
    def test_defaults_are_empty(self):
        cfg = load_operator_config({"HLE_AGENT_TOKEN": "t"})
        assert cfg.namespace_patterns == []
        assert cfg.exclude_namespaces == []

    def test_globs_are_parsed_and_stripped(self):
        cfg = load_operator_config(
            {
                "HLE_AGENT_TOKEN": "t",
                "HLE_NAMESPACE_PATTERNS": "e2e-*, apps-* ",
                "HLE_EXCLUDE_NAMESPACES": "kube-system, e2e-skip",
            }
        )
        assert cfg.namespace_patterns == ["e2e-*", "apps-*"]
        assert cfg.exclude_namespaces == ["kube-system", "e2e-skip"]

    @pytest.mark.parametrize("extra", ["HLE_NAMESPACE_PATTERNS", "HLE_EXCLUDE_NAMESPACES"])
    def test_literals_and_globs_conflict_exits(self, extra: str):
        env = {"HLE_AGENT_TOKEN": "t", "HLE_NAMESPACES": "apps", extra: "e2e-*"}
        with pytest.raises(SystemExit) as excinfo:
            load_operator_config(env)
        assert excinfo.value.code != 0


class TestKopfScope:
    def test_literals_keep_per_namespace_watch(self):
        cfg = OperatorConfig(mode=OperatorMode.AGENT, namespaces=["a", "b"])
        assert kopf_scope(cfg) == (False, ["a", "b"])

    def test_patterns_and_excludes_watch_clusterwide(self):
        cfg = OperatorConfig(
            mode=OperatorMode.AGENT,
            namespace_patterns=["e2e-*"],
            exclude_namespaces=["kube-system"],
        )
        assert kopf_scope(cfg) == (False, ["e2e-*", "!kube-system"])

    def test_only_excludes_use_a_star_pattern(self):
        cfg = OperatorConfig(mode=OperatorMode.AGENT, exclude_namespaces=["e2e-*"])
        assert kopf_scope(cfg) == (False, ["*", "!e2e-*"])

    def test_nothing_set_is_clusterwide(self):
        cfg = OperatorConfig(mode=OperatorMode.AGENT)
        assert kopf_scope(cfg) == (True, [])


class TestNamespaceInScope:
    def test_exclude_glob_drops_matching_namespace(self):
        cfg = OperatorConfig(mode=OperatorMode.AGENT, exclude_namespaces=["e2e-*"])
        assert namespace_in_scope(cfg, "e2e-x") is False
        assert namespace_in_scope(cfg, "default") is True

    def test_patterns_only_keep_matches(self):
        cfg = OperatorConfig(mode=OperatorMode.AGENT, namespace_patterns=["e2e-*"])
        assert namespace_in_scope(cfg, "e2e-a") is True
        assert namespace_in_scope(cfg, "default") is False

    def test_exclude_wins_over_pattern(self):
        cfg = OperatorConfig(
            mode=OperatorMode.AGENT,
            namespace_patterns=["e2e-*"],
            exclude_namespaces=["e2e-skip"],
        )
        assert namespace_in_scope(cfg, "e2e-a") is True
        assert namespace_in_scope(cfg, "e2e-skip") is False

    def test_literals_are_exact(self):
        cfg = OperatorConfig(mode=OperatorMode.AGENT, namespaces=["apps"])
        assert namespace_in_scope(cfg, "apps") is True
        assert namespace_in_scope(cfg, "apps-2") is False

    def test_no_scope_keeps_everything(self):
        cfg = OperatorConfig(mode=OperatorMode.AGENT)
        assert namespace_in_scope(cfg, "anything") is True
