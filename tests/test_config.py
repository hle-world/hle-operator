"""Operator mode resolution and agent config defaults."""

from __future__ import annotations

from hle_operator.config import (
    OperatorMode,
    load_operator_config,
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
