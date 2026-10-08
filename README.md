# HLE Operator

Kubernetes operator for [HLE (Home Lab Everywhere)](https://hle.world) tunnels. Expose your cluster services to the internet through HLE's relay network with declarative Kubernetes resources.

## Two ways to run HLE in a cluster

This chart ships both, and they are independent — install either, or both.

| | **Operator** | **Agent** |
|---|---|---|
| You declare tunnels in | `HLETunnel` CRDs and Ingress resources | the [dashboard](https://hle.world/dashboard) |
| Fits | GitOps, Flux, Argo — the cluster is the source of truth | clicking, trying things, homelabs |
| Finds services for you | no — you name them | yes, [discovery](https://hle.world/docs/discovery/) lists every Service |
| Enable with | `operator.enabled=true` (default) | `agent.enabled=true` |

```bash
# The agent, with cluster-wide read-only discovery
helm upgrade --install hle oci://ghcr.io/hle-world/charts/hle-operator \
  --set operator.enabled=false \
  --set agent.enabled=true \
  --set agent.token.value=hle_your_key_here
```

Get the token from **Dashboard → Connections → Agents → New** — it is shown once. The
pod dials out; nothing is published, and no Service or Ingress is created.

Prefer to hold the token yourself:

```bash
kubectl create secret generic hle-agent --from-literal=agent-token=hle_...
helm upgrade --install hle oci://ghcr.io/hle-world/charts/hle-operator \
  --set agent.enabled=true --set agent.token.existingSecret=hle-agent
```

The agent image defaults to the floating `headless` tag with
`pullPolicy: Always`, so every pod restart pulls the current build. For
reproducible rollouts pin a release tag instead:

```bash
helm upgrade --install hle oci://ghcr.io/hle-world/charts/hle-operator \
  --set agent.enabled=true --set agent.token.existingSecret=hle-agent \
  --set agent.image.tag=2609.6-headless
```

### What discovery can see

`agent.discovery.enabled=true` (the default) binds a ClusterRole granting
`get`, `list` and `watch` on **services** and **endpoints**, cluster-wide. That
is the entire grant. The agent never creates, patches or deletes anything.

Scope is cluster-wide but **opt-out**. `agent.discovery.excludeNamespaces`
(default `kube-system`, `kube-public`, `kube-node-lease`) is never reported, and
any namespace labelled `hle.world/expose=denied` is skipped too:

```bash
kubectl label namespace secrets hle.world/expose=denied
```

> The agent is handed these values as `HLE_DISCOVERY_EXCLUDE_NAMESPACES` and
> `HLE_DISCOVERY_EXCLUDE_LABEL`. hle-client does not read them yet — the
> client-side filter is landing in hle-client — so the values are wired up
> ahead of that change.

Turn discovery off with `--set agent.discovery.enabled=false` and the agent stops
mounting a ServiceAccount token altogether — you then type service URLs into
the dashboard yourself.

### Cluster-agent defaults

A few defaults differ from a laptop agent, because a cluster is a shared
network:

- **Firepuncher is off** (`agent.firepuncher.enabled=false`). `hle fp --agent`
  turns the agent into a dialer for other machines' services; a cluster agent
  should advertise its declared tunnels, not double as a jump host into the
  cluster network. Opt in with `--set agent.firepuncher.enabled=true`.
  The value is passed as `HLE_FIREPUNCHER_ENABLED`, which hle-client does not
  read yet.
- **`HLE_INSTALL_METHOD=kubernetes`** is set on both containers so the relay
  can label the install. hle-client does not read it yet.
- **Readiness** runs `hle agent status`, which fails when no enrollment token
  is configured. It does **not** yet prove the relay accepted the agent: the
  agent keeps no local connection state and hle-client exposes no
  welcome-aware signal. A welcome-gated probe is a follow-up hle-client change.
- The operator's `secrets: get` grant stays cluster-wide: the basic-auth and
  per-tunnel API-key Secrets it reads are referenced by name from CRs and can
  live in any namespace, so there is no safe static scoping.

## Features

- **HLETunnel CRD** — Declarative tunnel management with full access control
- **Ingress support** — Use `ingressClassName: hle` on standard Ingress resources
- **Access control** — SSO email allowlists (per-provider), PIN protection, Basic Auth
- **Sync policies** — `strict` (operator is source of truth) or `initial` (dashboard stays editable)
- **Smart updates** — Access control changes don't restart tunnels
- **Per-tunnel API keys** — Override the global key for multi-tenant setups

## Quick Start

### 1. Install the operator

The chart is published as an OCI artifact to GitHub Container Registry on every
release. Installing without `--version` always pulls the newest chart, whose
default image tag tracks that same release:

```bash
# Create the API key Secret
kubectl create secret generic hle-api-key \
  --from-literal=api-key=hle_your_key_here

# Install from the published OCI chart (newest release)
helm upgrade --install hle-operator \
  oci://ghcr.io/hle-world/charts/hle-operator
```

Releases are CalVer (`v2609.4`). The chart version and `appVersion` match the
release, the default `image.tag` is empty so it resolves to `Chart.appVersion`,
and `image.pullPolicy` is `IfNotPresent` — release image tags are immutable, so
a restart reuses the cached image instead of re-pulling. Pin a specific chart
with:

```bash
helm upgrade --install hle-operator \
  oci://ghcr.io/hle-world/charts/hle-operator --version 2609.4
```

The image is also published with a floating `latest` tag on each release; the
chart does not use it, so upgrades are driven by the chart version instead.

#### From source (contributors)

```bash
git clone https://github.com/hle-world/hle-operator
cd hle-operator
helm upgrade --install hle-operator ./chart/hle-operator
```

### 2. Expose a service

**Using HLETunnel CRD:**

```yaml
apiVersion: hle.world/v1alpha1
kind: HLETunnel
metadata:
  name: grafana
spec:
  serviceRef:
    name: grafana
    namespace: monitoring
    port: 3000
  label: grafana
  authMode: sso
  syncPolicy: strict
  accessControl:
    allowedUsers:
      - email: me@gmail.com
        provider: google
      - email: dev@company.com
        provider: github
    pin: "1234"
```

**Using Ingress:**

```yaml
apiVersion: networking.k8s.io/v1
kind: Ingress
metadata:
  name: grafana
  annotations:
    hle.world/label: grafana
    hle.world/auth-mode: sso
    hle.world/allowed-users: "me@gmail.com:google,dev@company.com:github"
    hle.world/pin: "1234"
spec:
  ingressClassName: hle
  rules:
  - http:
      paths:
      - path: /
        pathType: Prefix
        backend:
          service:
            name: grafana
            port:
              number: 3000
```

### 3. Check status

```bash
kubectl get hlt
# NAME      PHASE       PUBLIC URL                        LABEL     SYNC     AGE
# grafana   Connected   https://grafana-x7k.hle.world     grafana   strict   5m
```

## CRD Reference

### spec.serviceRef

| Field | Type | Required | Description |
|---|---|---|---|
| `name` | string | yes | Kubernetes Service name |
| `namespace` | string | no | Service namespace (defaults to HLETunnel namespace) |
| `port` | integer | yes | Service port |
| `protocol` | string | no | `http` (default) or `https` |

### spec.accessControl

| Field | Type | Description |
|---|---|---|
| `allowedUsers` | array | List of `{email, provider}` objects. Provider: `google`, `github`, `hle`, `any` |
| `pin` | string | 4-8 digit PIN code |
| `basicAuth.secretRef.name` | string | Secret with `username` and `password` keys |

### spec.zone / spec.apex

Publish under one of your custom zones instead of the base domain:

| Field | Type | Description |
|---|---|---|
| `zone` | string | Custom zone, e.g. `t00t.us` → `https://<label>.t00t.us` (in the zone's `clean` subdomain mode). The zone must be active and your account a member of it. |
| `apex` | boolean | Serve at the bare zone root (`https://t00t.us`). Requires `zone`. |

On an Ingress, use the annotations `hle.world/zone: t00t.us` and `hle.world/apex: "true"`.

### spec.syncPolicy

- **`strict`** (default) — Operator continuously reconciles access control. Dashboard edits are overwritten every 60 seconds.
- **`initial`** — Operator applies settings only on tunnel creation. Dashboard remains fully editable after that.

### spec.apiKeyRef

Override the global API key for this specific tunnel:

```yaml
spec:
  apiKeyRef:
    name: my-other-api-key  # Secret name
    key: api-key             # Key within the Secret
```

## Architecture

```
┌─────────────────────────────────────────────────────┐
│                  Kubernetes Cluster                  │
│                                                      │
│  ┌──────────────┐    ┌─────────────────────────┐    │
│  │ HLE Operator │───→│ Service (grafana:3000)   │    │
│  │   (kopf)     │    │ Service (argocd:8080)    │    │
│  └──────┬───────┘    │ Service (longhorn:8000)  │    │
│         │            └─────────────────────────┘    │
└─────────┼───────────────────────────────────────────┘
          │ WebSocket tunnel (per service)
          ▼
┌─────────────────────┐
│   hle.world relay   │ ← Assigns subdomains, handles SSO
│   (VPS Amsterdam)   │
└─────────────────────┘
          ▲
          │ HTTPS
          │
    End users access:
    grafana-x7k.hle.world
    argocd-x7k.hle.world
```

## Development

```bash
uv venv && uv pip install -e ".[dev]"
uv run kopf run src/hle_operator/handlers.py --verbose
uv run ruff check src/
uv run pytest tests/ -v
```

## License

MIT
