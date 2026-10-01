"""Entrypoint-level checks: exit codes, shutdown, gitops off, legacy isolation."""

from __future__ import annotations

import asyncio
import subprocess
import sys
from typing import Any

import pytest

from hle_operator import __main__ as entrypoint
from hle_operator.config import OperatorConfig, OperatorMode


def test_agent_mode_without_credential_exits_1(monkeypatch):
    monkeypatch.setenv("HLE_OPERATOR_MODE", "agent")
    monkeypatch.delenv("HLE_AGENT_TOKEN", raising=False)
    monkeypatch.delenv("HLE_CREDENTIAL", raising=False)
    monkeypatch.delenv("HLE_API_KEY", raising=False)
    assert entrypoint.main() == 1


def test_python_dash_m_runs_main():
    env = {"PATH": "/usr/bin:/bin", "HLE_OPERATOR_MODE": "agent"}
    result = subprocess.run(
        [sys.executable, "-m", "hle_operator"],
        capture_output=True,
        text=True,
        check=False,
        timeout=60,
        env=env,
    )
    assert result.returncode == 1, result.stderr
    assert "requires an enrollment credential" in result.stderr


def test_legacy_module_does_not_import_agent_stack():
    code = (
        "import sys; import hle_operator.handlers; "
        "assert 'hle_operator.registry' not in sys.modules, 'registry imported'; "
        "assert 'hle_operator.agent_handlers' not in sys.modules, 'agent handlers imported'; "
        "assert 'hle_client.agent' not in sys.modules, 'agent client imported'; "
        "print('ok')"
    )
    result = subprocess.run(
        [sys.executable, "-c", code], capture_output=True, text=True, check=False, timeout=60
    )
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "ok"


# -- agent mode wiring, with AgentClient and kopf replaced ---------------------

try:
    import hle_client.agent  # noqa: F401
    from hle_common.agent_protocol import DeclaredAccess  # noqa: F401

    HAVE_P4_CLIENT = True
except ImportError:  # pragma: no cover
    HAVE_P4_CLIENT = False

needs_p4 = pytest.mark.skipif(not HAVE_P4_CLIENT, reason="hle-client P3+P4 required")


@needs_p4
def test_real_agent_client_has_the_api_the_entrypoint_uses():
    # hle-client ships no py.typed, so mypy sees AgentClient as Any; pin the
    # names here instead.
    import inspect

    from hle_client.agent import AgentClient

    params = inspect.signature(AgentClient.__init__).parameters
    for name in ("declares_endpoints", "on_declared_ack", "spec_resolver"):
        assert name in params, name
    for attr in ("run", "stop", "send_declared_endpoints", "endpoint_statuses"):
        assert callable(getattr(AgentClient, attr)), attr
    for prop in ("fatal_error", "exit_code"):
        assert isinstance(inspect.getattr_static(AgentClient, prop), property), prop


class FakeAgent:
    """Stands in for AgentClient: run() blocks until stopped or told to fail."""

    instances: list[FakeAgent] = []

    def __init__(self, token: str, host: str, port: int, **kwargs: Any) -> None:
        self.kwargs = kwargs
        self.sent: list[tuple[list[Any], int]] = []
        self.stopped = False
        self.fatal_error: str | None = None
        self.exit_code = 0
        self.end = asyncio.Event()
        self.raise_in_run: BaseException | None = None
        FakeAgent.instances.append(self)

    async def run(self) -> None:
        await self.end.wait()
        if self.raise_in_run is not None:
            raise self.raise_in_run

    async def stop(self) -> None:
        self.stopped = True

    async def send_declared_endpoints(self, endpoints: list[Any], revision: int) -> bool:
        self.sent.append((list(endpoints), revision))
        return False

    def endpoint_statuses(self) -> None:
        return None


class FakeKopf:
    def __init__(self) -> None:
        self.kwargs: dict[str, Any] = {}
        self.crash: BaseException | None = None
        self.started = asyncio.Event()

    async def operator(self, **kwargs: Any) -> None:
        self.kwargs = kwargs
        self.started.set()
        if self.crash is not None:
            raise self.crash
        await kwargs["stop_flag"].wait()


@pytest.fixture
def agent_env(monkeypatch):
    FakeAgent.instances.clear()
    fake_kopf = FakeKopf()
    monkeypatch.setattr("hle_client.agent.AgentClient", FakeAgent)
    monkeypatch.setattr(entrypoint.kopf, "operator", fake_kopf.operator)
    monkeypatch.setattr(entrypoint, "load_config", lambda: None)
    monkeypatch.setattr(entrypoint, "_install_signal_handlers", lambda stop: None)
    # Keep the background loops from touching an API server.
    monkeypatch.setattr(
        "hle_operator.registry.DeclarationRegistry.run_initial_sync", _never, raising=True
    )
    monkeypatch.setattr("hle_operator.registry.DeclarationRegistry.run_maintenance", _never)
    monkeypatch.setattr("hle_operator.registry.DeclarationRegistry.run_secret_poll", _never)
    return fake_kopf


async def _never(*_: Any, **__: Any) -> None:
    await asyncio.Event().wait()


def cfg(*, gitops: bool) -> OperatorConfig:
    return OperatorConfig(mode=OperatorMode.AGENT, credential="hle_x", gitops=gitops)


@needs_p4
async def test_gitops_off_registers_no_watches_and_sends_one_empty_frame(agent_env):
    task = asyncio.create_task(entrypoint._run_agent(cfg(gitops=False)))
    await agent_env.started.wait()
    [agent] = FakeAgent.instances
    assert agent.sent == [([], agent.sent[0][1])]
    assert agent.kwargs["declares_endpoints"] is False
    kopf_registry = agent_env.kwargs["registry"]
    assert not kopf_registry._watching.get_all_handlers()
    assert not kopf_registry._changing.get_all_handlers()
    assert agent_env.kwargs["standalone"] is True
    agent_env.kwargs["stop_flag"].set()
    assert await task == 0
    assert agent.stopped


@needs_p4
async def test_gitops_on_registers_cr_and_ingress_watches(agent_env):
    task = asyncio.create_task(entrypoint._run_agent(cfg(gitops=True)))
    await agent_env.started.wait()
    [agent] = FakeAgent.instances
    assert agent.sent == []  # nothing before the initial sync
    assert agent.kwargs["declares_endpoints"] is True
    kopf_registry = agent_env.kwargs["registry"]
    plurals = {s.any_name for s in kopf_registry._watching.get_all_selectors()}
    assert plurals == {"hletunnels", "ingresses"}
    agent_env.kwargs["stop_flag"].set()
    assert await task == 0


@needs_p4
async def test_fatal_agent_error_stops_kopf_and_exits_1(agent_env):
    task = asyncio.create_task(entrypoint._run_agent(cfg(gitops=True)))
    await agent_env.started.wait()
    [agent] = FakeAgent.instances
    agent.fatal_error = "token revoked"
    agent.end.set()
    assert await asyncio.wait_for(task, 5) == 1
    assert agent_env.kwargs["stop_flag"].is_set()


@needs_p4
async def test_kopf_crash_stops_agent_and_exits_1(agent_env):
    agent_env.crash = RuntimeError("watcher died")
    result = await asyncio.wait_for(entrypoint._run_agent(cfg(gitops=True)), 5)
    assert result == 1
    [agent] = FakeAgent.instances
    assert agent.stopped


@needs_p4
async def test_agent_crash_exits_1(agent_env):
    task = asyncio.create_task(entrypoint._run_agent(cfg(gitops=True)))
    await agent_env.started.wait()
    [agent] = FakeAgent.instances
    agent.raise_in_run = RuntimeError("boom")
    agent.end.set()
    assert await asyncio.wait_for(task, 5) == 1


@needs_p4
async def test_kopf_namespaces_match_lister(agent_env):
    config = OperatorConfig(
        mode=OperatorMode.AGENT, credential="hle_x", gitops=True, namespaces=["a", "b"]
    )
    task = asyncio.create_task(entrypoint._run_agent(config))
    await agent_env.started.wait()
    assert agent_env.kwargs["namespaces"] == ["a", "b"]
    assert agent_env.kwargs["clusterwide"] is False
    agent_env.kwargs["stop_flag"].set()
    await task
