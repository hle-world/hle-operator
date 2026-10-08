# hle-operator

Kubernetes agent and operator for [HLE](https://hle.world). One Deployment is
both the cluster **agent** (the default) and, when `gitops.enabled=true`, the
CRD/Ingress reconciler. A plain install with a tunnel-scoped `hle_` credential
gives you a cluster you control from the hle.world dashboard and the `hle` CLI:
expose Services, set access rules, read status — nothing is written to the
cluster. See the [repository README](../../README.md) for the full walkthrough.

```bash
helm upgrade --install hle-operator oci://ghcr.io/hle-world/charts/hle-operator \
  --set credential.value=hle_your_key_here
```

Get the credential from **Dashboard → Connections → Agents → New**. It is shown once.
It is a tunnel-scoped `hle_` key (a legacy `hlea_` token also works); the
full-account key the old operator used is no longer needed.

## Modes

`mode` defaults to `""` (auto): the chart picks **agent** unless only
`apiKey.*` is set — an older install that holds a full-account key and no agent
credential. `agent` and `legacy` force the choice.

- **agent** — `python -m hle_operator` runs the agent and (optionally) kopf in
  one event loop. `replicas` is pinned to 1.
- **legacy** — the original apiKey operator, unchanged and **deprecated**.
  `replicaCount` is honoured.

## Values

| Key | Default | Description |
|---|---|---|
| `mode` | `""` (auto) | `agent` or `legacy`; empty auto-detects. |
| `credential.value` / `credential.existingSecret` | `""` | The single HLE credential, inline or from an existing Secret. Agent mode: tunnel-scoped `hle_` key. Legacy mode: full-account API key. |
| `gitops.enabled` | `true` | Run the CRD/Ingress reconciler on top of the agent. Declared tunnels show read-only in the dashboard. |
| `gitops.secretResourceNames` | `[]` | Secret names the agent may `get` for visitor basic auth. get-only. |
| `gitops.clusterWideSecretGet` | `false` | Cluster-wide Secret `get` for basic-auth Secrets whose names are unknown ahead of time. Widest grant; prefer `secretResourceNames`. |
| `scope.namespaces` | `[]` | Namespaces the agent may see/expose. Empty is cluster-wide; set uses per-namespace Roles. |
| `handover.enabled` | `true` | Zero-drop `RollingUpdate` (maxSurge=1, maxUnavailable=0): the relay hands tunnels over to the new pod (needs relay and hle-client 2610.1+). `false` uses `Recreate`. |
| `allowRawUrls` | `false` | Allow raw URLs as targets. Services only by default; the kube API, metadata, node IPs and loopback are always refused. |
| `relay.host` / `relay.port` | `""` | Override the HLE relay (self-hosted). |
| `operator.enabled` | `true` | Installs the pod (agent mode: also when `agent.enabled`). `false` turns GitOps off too, so an old agent-only values file stays a pure agent. |
| `replicaCount` | `1` | Honoured in legacy mode; agent mode pins 1. |
| `apiKey.*` | `""` | **Deprecated**, mapped onto `credential` for upgrades. |
| `agent.token.*` | `""` | **Deprecated**, mapped onto `credential` for upgrades. |
| `agent.enabled` | `false` | **Deprecated** separate headless agent; renders only in legacy mode. |
| `agent.discovery.enabled` | `true` | Read-only Services/Endpoints discovery. Off drops the grant and the env. |
| `agent.discovery.excludeNamespaces` | `[kube-system, kube-public, kube-node-lease]` | Namespaces the agent never reports. |
| `agent.discovery.excludeLabel` | `hle.world/expose=denied` | Namespaces carrying this label are skipped. |
| `agent.firepuncher.enabled` | `false` | Allow `hle fp --agent` through this agent. Off for cluster agents. |
| `agent.env` | `[]` | Extra environment for the agent container. |

## RBAC

Agent mode grants `get/list/watch` on Services and Endpoints, and — when
`gitops.enabled` — on HLETunnels and Ingresses (`patch` on both, for finalizer
cleanup and the `hle.world/public-url` annotation), plus `patch`/`update` on their
`status` subresources and `create` on Events. Secrets are `get`-only, by name,
and only when `gitops.secretResourceNames` is set or `clusterWideSecretGet` is
opted into. The chart never grants Secrets `list`/`watch`, `pods/exec`,
`pods/log`, or any write beyond CR/Ingress status and the Ingress annotation.

The agent pod runs as uid 1001, non-root, with a read-only root filesystem
(`/data` and `/tmp` are emptyDirs) and keeps its ServiceAccount token mounted
in every agent-mode run, since the process always talks to the Kubernetes API.

With `scope.namespaces` set, the namespaced grants become per-namespace Roles;
a small ClusterRole still covers CRD discovery (and the opt-in cluster-wide
Secret get).

## Migration and rollback

### From the legacy apiKey operator

1. Enroll the cluster (**Dashboard → Connections → Agents → New**) and copy the
   tunnel-scoped `hle_` key.
2. ```bash
   helm upgrade hle-operator oci://ghcr.io/hle-world/charts/hle-operator \
     --reuse-values --set credential.value=hle_your_key_here
   ```
   Auto-detect flips the release to agent mode and replaces the pod.
   The old operator stops its standalone tunnels and
   the new pod declares the same labels — those were plain tunnels before, not
   agent endpoints, so there is no declaration conflict.
3. If you ran the separate headless agent (`agent.enabled=true`), pass the
   **same** credential it was enrolled with and set `agent.enabled=false`. Its
   dashboard endpoints carry over because it is the same enrollment. A
   different credential leaves those endpoints with the old, now-offline agent.

### Rollback

```bash
helm rollback hle-operator
```

Legacy mode resumes. Cluster-declared rows remain on the server, but no agent
connects with that enrollment, so nothing runs twice. Re-upgrading re-declares
them.

### Zero-downtime upgrades

`helm upgrade` is zero-drop by default: the new pod connects alongside the old
one, the relay hands the tunnels over, then the old pod exits. This needs relay
and hle-client 2610.1 or newer. Against an older self-hosted relay, fall back to
`Recreate` (a few seconds of dropped tunnels per upgrade):

```bash
helm upgrade hle-operator oci://ghcr.io/hle-world/charts/hle-operator \
  --reuse-values --set handover.enabled=false
```

## Notes

- `HLE_INSTALL_METHOD=kubernetes`, `HLE_HANDOVER_GROUP=<ns>/<release>`,
  `HLE_HOME=/data` and the P0 discovery opt-outs are set on the agent
  container. The readiness probe runs `hle agent status --ready`, which passes
  only once a welcomed control connection has recorded its pid.
- The discovery opt-outs are passed as `HLE_DISCOVERY_EXCLUDE_NAMESPACES` and
  `HLE_DISCOVERY_EXCLUDE_LABEL`.
- Firepuncher is passed as `HLE_FIREPUNCHER_ENABLED`; off for cluster agents.
- `HLE_NODE_IP` (downward API `status.hostIP`) and `HLE_ALLOW_RAW_URLS` back the
  agent-side target guard.
