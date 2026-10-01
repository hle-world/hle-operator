"""``python -m hle_operator`` — the operator and its agent in one event loop.

Agent mode (the default) runs ``AgentClient`` and kopf together: kopf watches
CRs and Ingresses and declares them; the agent runs every tunnel, cluster-owned
ones included. Legacy mode runs the original apiKey kopf operator alone,
exactly as ``kopf run handlers.py --liveness=... --verbose`` used to.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import signal
import sys

import kopf

from hle_operator.config import (
    OperatorConfig,
    OperatorMode,
    load_config,
    load_operator_config,
)

logger = logging.getLogger(__name__)

LIVENESS_ENDPOINT = "http://0.0.0.0:8080/healthz"
# How long a SIGTERM waits for the agent to stop its tunnels before cancelling.
STOP_TIMEOUT = 20.0


def main() -> int:
    cfg = load_operator_config()
    try:
        if cfg.mode is OperatorMode.LEGACY:
            # The same logging `kopf run --verbose` set up before.
            kopf.configure(verbose=True)
            return asyncio.run(_run_legacy(cfg))
        logging.basicConfig(
            level=logging.INFO,
            format="%(asctime)s %(levelname)s %(name)s %(message)s",
        )
        logger.info("hle-operator starting in agent mode (gitops=%s)", cfg.gitops)
        return asyncio.run(_run_agent(cfg))
    except KeyboardInterrupt:
        return 0


def _install_signal_handlers(stop: asyncio.Event) -> None:
    """Set *stop* on SIGTERM/SIGINT.

    kopf replaces these with its own once it starts, which ends
    ``kopf.operator()`` and so reaches the same shutdown path; these only
    cover a signal that lands before kopf is up.
    """
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGTERM, signal.SIGINT):
        with contextlib.suppress(NotImplementedError):
            loop.add_signal_handler(sig, stop.set)


async def _run_legacy(cfg: OperatorConfig) -> int:
    """Today's operator, unchanged.

    Importing the module registers the legacy startup/create/update/delete/
    timer handlers into kopf's default registry; its startup handler loads the
    kube config and API key and pins the legacy finalizer and storages. Agent
    code is never imported on this path.
    """
    import hle_operator.handlers  # noqa: F401

    await kopf.operator(
        clusterwide=not cfg.namespaces,
        namespaces=cfg.namespaces,
        liveness_endpoint=LIVENESS_ENDPOINT,
    )
    return 0


async def _run_agent(cfg: OperatorConfig) -> int:
    if not cfg.credential:
        logger.error("Agent mode requires an enrollment credential; exiting")
        return 1

    from hle_client.agent import AgentClient

    from hle_operator.registry import DeclarationRegistry, KubernetesLister
    from hle_operator.status import StatusPublisher

    load_config()

    # The client and the registry need each other: the client's callbacks land
    # on the registry, and the registry sends through the client. The holder
    # breaks the cycle without a partially-built object.
    holder: dict[str, DeclarationRegistry] = {}
    client = AgentClient(
        cfg.credential,
        cfg.relay_host,
        cfg.relay_port,
        declares_endpoints=cfg.gitops,
        on_declared_ack=lambda ack: holder["registry"].record_ack(ack),
        spec_resolver=lambda spec: holder["registry"].resolve_spec(spec),
    )
    # Listed exactly as kopf watches: the same namespaces or the whole cluster.
    registry = DeclarationRegistry(client, lister=KubernetesLister(cfg.namespaces))
    holder["registry"] = registry

    stop = asyncio.Event()
    _install_signal_handlers(stop)

    # A registry of its own, so agent mode runs only the handlers registered
    # below. A bare registry has no login handler (kopf's default one adds it
    # implicitly), so add the same one: the kubernetes client's config.
    kopf_registry = kopf.OperatorRegistry()
    kopf.on.login(registry=kopf_registry)(kopf.login_via_client)
    aux: list[asyncio.Task[None]] = [asyncio.create_task(StatusPublisher(client, registry).run())]
    if cfg.gitops:
        from hle_operator import agent_handlers

        agent_handlers.register(registry, kopf_registry)
        aux.append(asyncio.create_task(registry.run_initial_sync()))
        aux.append(asyncio.create_task(registry.run_maintenance()))
        aux.append(asyncio.create_task(registry.run_secret_poll()))
    else:
        # No CR/Ingress handlers, but still one explicit empty frame (queued
        # until the welcome) so any cluster rows left on the server retire
        # instead of running ownerless.
        await client.send_declared_endpoints([], registry.next_revision())

    # Only on.event handlers are registered, so kopf adds no finalizer and no
    # progress annotations; the legacy finalizer is stripped on sight. One
    # replica, so no peering.
    client_task = asyncio.create_task(client.run(), name="agent")
    kopf_task = asyncio.create_task(
        kopf.operator(
            standalone=True,
            registry=kopf_registry,
            clusterwide=not cfg.namespaces,
            namespaces=cfg.namespaces,
            liveness_endpoint=LIVENESS_ENDPOINT,
            settings=kopf.OperatorSettings(),
            stop_flag=stop,
        ),
        name="kopf",
    )
    stop_task = asyncio.create_task(stop.wait(), name="stop")
    await asyncio.wait({client_task, kopf_task, stop_task}, return_when=asyncio.FIRST_COMPLETED)

    # Whichever half ended, end the other: the agent must not keep serving
    # declarations nobody maintains, and kopf must not declare into a dead
    # agent. kopf's own SIGTERM handling ends kopf_task, so a signal arrives
    # here as "kopf finished".
    stop.set()
    registry.close()
    with contextlib.suppress(Exception):
        await asyncio.wait_for(client.stop(), timeout=STOP_TIMEOUT)
    for task in (client_task, kopf_task, stop_task, *aux):
        task.cancel()
    results = await asyncio.gather(client_task, kopf_task, stop_task, *aux, return_exceptions=True)

    if client.fatal_error:
        logger.error("Agent stopped for good: %s", client.fatal_error)
        return 1
    agent_result, kopf_result = results[0], results[1]
    if isinstance(agent_result, Exception):
        logger.error("Agent crashed: %r", agent_result)
        return 1
    if isinstance(kopf_result, Exception):
        logger.error("kopf crashed: %r", kopf_result)
        return 1
    return int(client.exit_code)


if __name__ == "__main__":
    sys.exit(main())
