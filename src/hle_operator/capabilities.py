"""Extra hello capabilities the operator advertises on top of hle-client's own.

``AgentClient`` builds its hello inline and has no hook for an embedder to add
a capability, and hle-client 2611.4 does not advertise ``declared-override``
itself. The operator understands ``DeclaredAckEntry.overridden``, so it must say
so, or the relay never sets the field. This swaps the hello class the client
uses for one that appends the capability; it is a no-op once the client
advertises it natively.
"""

from __future__ import annotations

from dataclasses import dataclass

from hle_common.agent_protocol import DECLARED_OVERRIDE_CAPABILITY, AgentHello

EXTRA_CAPABILITIES = (DECLARED_OVERRIDE_CAPABILITY,)


@dataclass(kw_only=True)
class OperatorHello(AgentHello):
    """AgentHello that also advertises the operator's extra capabilities."""

    def __post_init__(self) -> None:
        parent = getattr(super(), "__post_init__", None)
        if callable(parent):
            parent()
        for cap in EXTRA_CAPABILITIES:
            if cap not in self.capabilities:
                self.capabilities.append(cap)


def advertise_extra_capabilities() -> None:
    """Make every hello the agent client sends carry :data:`EXTRA_CAPABILITIES`."""
    import hle_client.agent as agent_module

    agent_module.AgentHello = OperatorHello  # type: ignore[attr-defined]
