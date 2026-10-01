# hle-operator

Kubernetes operator and agent for [HLE](https://hle.world). Installs either the
CRD/Ingress reconciler (`operator.enabled`), the dashboard-driven agent
(`agent.enabled`), or both. See the [repository README](../../README.md) for
the full walkthrough.

```bash
helm upgrade --install hle-operator oci://ghcr.io/hle-world/charts/hle-operator \
  --set apiKey.value=hle_your_key_here
```

## Values

| Key | Default | Description |
|---|---|---|
| `operator.enabled` | `true` | Install the CRD/Ingress reconciler. |
| `agent.enabled` | `false` | Install the agent (`hle agent run`). |
| `apiKey.value` / `apiKey.existingSecret` | `""` | Operator API key, inline or from an existing Secret. |
| `agent.token.value` / `agent.token.existingSecret` | `""` | Agent enrollment token (`hlea_…`), inline or from an existing Secret. |
| `agent.image.tag` | `headless` | Floating agent image tag; pin a release tag for reproducible rollouts. |
| `agent.discovery.enabled` | `true` | Cluster-wide read-only Services/Endpoints discovery; off means no ServiceAccount token is mounted. |
| `agent.discovery.excludeNamespaces` | `[kube-system, kube-public, kube-node-lease]` | Namespaces the agent never reports. |
| `agent.discovery.excludeLabel` | `hle.world/expose=denied` | Namespaces carrying this label are skipped. |
| `agent.firepuncher.enabled` | `false` | Allow `hle fp --agent` through this agent. Off for cluster agents. |
| `agent.env` | `[]` | Extra environment for the agent container. |

The agent is a single replica with a `Recreate` strategy: one enrollment is one
machine, so two pods on the same token would fight over the same endpoints.

## Notes

- `HLE_INSTALL_METHOD=kubernetes` is set on both containers. hle-client does
  not read it yet.
- The discovery opt-outs are passed as `HLE_DISCOVERY_EXCLUDE_NAMESPACES` and
  `HLE_DISCOVERY_EXCLUDE_LABEL`; the client-side filter lands in hle-client.
- Firepuncher is passed as `HLE_FIREPUNCHER_ENABLED`; hle-client does not read
  it yet.
- The agent readiness probe runs `hle agent status`, which only checks that an
  enrollment credential is configured — a welcome-gated probe needs an
  hle-client change.
